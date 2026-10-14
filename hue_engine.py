"""Fast, self-healing Philips Hue Bluetooth engine for Windows.

Shared by the GUI (hue_controller.py) and the command line (hue_cli.py).

Why it is fast
  * The lamps are paired with Windows, so we connect straight to their
    address instead of scanning first. The lamps only advertise about once
    per second (less while connected), which is why scan-first scripts need
    10+ seconds and still miss a lamp now and then.
  * Windows' cached GATT table is used: a connection takes ~0.3-2.5 s.
  * Connections stay open. A command is then one ~100 ms GATT write to the
    combined light-state characteristic (power, brightness, colour, effect
    and fade time in a single packet), and one subscription on that same
    characteristic reports every change - including ones made by the phone.

Why it is robust
  * Every lamp has its own supervisor task that reconnects with a growing
    pause (0.25 s .. 30 s) until it succeeds; a new command skips the pause.
  * Commands are desired state, not fire-and-forget: they wait in a
    latest-wins queue and are written as soon as the link is back - also
    when the lamp was never connected yet. ``HueManager.pending_ttl`` limits
    how long they wait (0 = until the lamp has them).
  * Every Bluetooth call has a hard deadline (``guarded``) that holds even
    when a Windows call ignores cancellation, so nothing can hang forever.
  * Bluetooth radio on/off and adapter removal are watched and reported.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import logging.handlers
import os
import random
import socket
import struct
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

__version__ = "1.2.0"
PROJECT_URL = "https://github.com/jargyl/huetooth"

log = logging.getLogger("hue")

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

LIGHT_SERVICE = "932c32bd-0000-47a2-835a-a8d455b859dd"
CONTROL_CHAR = "932c32bd-0007-47a2-835a-a8d455b859dd"  # combined light state
HUE_ADVERT_UUID = "0000fe0f-0000-1000-8000-00805f9b34fb"

TLV_POWER = 0x01
TLV_BRIGHTNESS = 0x02
TLV_MIREDS = 0x03
TLV_XY = 0x04
TLV_TRANSITION = 0x05  # uint16 in steps of 100 ms; 0 = instant, lamp default ~4
TLV_EFFECT = 0x06
TLV_EFFECT_SPEED = 0x08

BRIGHTNESS_MIN, BRIGHTNESS_MAX = 1, 254
MIREDS_MIN, MIREDS_MAX = 153, 500

# (id, name, Dutch alias kept for older command lines)
EFFECTS: tuple[tuple[int, str, str], ...] = (
    (1, "Candle", "kaars"),
    (2, "Fireplace", "haardvuur"),
    (3, "Prism", "prisma"),
    (10, "Sparkle", "fonkeling"),
    (11, "Opal", "opaal"),
    (12, "Glisten", "glinstering"),
    (14, "Underwater", "onderwater"),
    (15, "Cosmos", "kosmos"),
    (16, "Sunbeam", "zonnestraal"),
    (17, "Enchant", "betovering"),
)
EFFECT_NAMES = {eid: name for eid, name, _ in EFFECTS}

COMMAND_FIELDS = ("power", "brightness", "xy", "mireds", "effect", "effect_speed", "transition")


def _clamp(value, low, high):
    return max(low, min(high, value))


@dataclass(frozen=True)
class LampState:
    """What the lamp reported last. ``None`` means unknown."""

    power: bool | None = None
    brightness: int | None = None
    xy: tuple[float, float] | None = None
    mireds: int | None = None
    effect: int = 0
    effect_speed: int | None = None

    @property
    def mode(self) -> str | None:
        return "ct" if self.mireds is not None else "xy" if self.xy is not None else None


def normalise_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate and clamp command fields; ``None`` values are dropped."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key == "power":
            out[key] = bool(value)
        elif key == "brightness":
            out[key] = _clamp(int(round(float(value))), BRIGHTNESS_MIN, BRIGHTNESS_MAX)
        elif key == "xy":
            x, y = value
            out[key] = (_clamp(float(x), 0.0, 1.0), _clamp(float(y), 0.0, 1.0))
        elif key == "mireds":
            out[key] = _clamp(int(round(float(value))), MIREDS_MIN, MIREDS_MAX)
        elif key == "effect":
            out[key] = _clamp(int(value), 0, 255)
        elif key == "effect_speed":
            out[key] = _clamp(int(value), 1, 254)
        elif key == "transition":
            out[key] = _clamp(int(round(float(value))), 0, 600)
        else:
            raise ValueError(f"unknown field: {key}")
    if "xy" in out and "mireds" in out:
        raise ValueError("choose a color (xy) or a white (mireds), not both")
    return out


def encode_command(fields: dict[str, Any]) -> bytes:
    """Build the TLV packet for the combined characteristic."""
    f = normalise_fields(fields)
    out = bytearray()
    if "power" in f:
        out += bytes((TLV_POWER, 1, 1 if f["power"] else 0))
    if "brightness" in f:
        out += bytes((TLV_BRIGHTNESS, 1, f["brightness"]))
    colour = "mireds" in f or "xy" in f
    if "mireds" in f:
        out += bytes((TLV_MIREDS, 2)) + struct.pack("<H", f["mireds"])
    elif "xy" in f:
        x, y = f["xy"]
        out += bytes((TLV_XY, 4)) + struct.pack("<HH", int(round(x * 0xFFFF)), int(round(y * 0xFFFF)))
    if "effect" in f and not (colour and f["effect"] == 0):
        # Measured on the lamps: "effect off" in the same packet makes them
        # ignore the colour, while a colour on its own already ends any effect.
        out += bytes((TLV_EFFECT, 1, f["effect"]))
    if "effect_speed" in f:
        out += bytes((TLV_EFFECT_SPEED, 1, f["effect_speed"]))
    if "transition" in f:
        out += bytes((TLV_TRANSITION, 2)) + struct.pack("<H", f["transition"])
    return bytes(out)


def decode_report(data: bytes) -> LampState:
    """Parse a state report (same TLV format) from the combined characteristic."""
    values: dict[str, Any] = {}
    i = 0
    while i + 2 <= len(data):
        kind, size = data[i], data[i + 1]
        chunk = bytes(data[i + 2:i + 2 + size])
        if len(chunk) < size:
            break
        if kind == TLV_POWER and size >= 1:
            values["power"] = bool(chunk[0])
        elif kind == TLV_BRIGHTNESS and size >= 1:
            values["brightness"] = chunk[0]
        elif kind == TLV_MIREDS and size >= 2:
            mireds = int.from_bytes(chunk[:2], "little")
            if mireds != 0xFFFF:
                values["mireds"] = mireds
        elif kind == TLV_XY and size >= 4:
            x, y = struct.unpack_from("<HH", chunk)
            if (x, y) != (0xFFFF, 0xFFFF):
                values["xy"] = (x / 0xFFFF, y / 0xFFFF)
        elif kind == TLV_EFFECT and size >= 1:
            values["effect"] = chunk[0]
        elif kind == TLV_EFFECT_SPEED and size >= 1:
            values["effect_speed"] = chunk[0]
        i += 2 + size
    return LampState(**values)


def state_fields(state: LampState) -> dict[str, Any]:
    """Command fields that would reproduce *state* (used for scenes)."""
    fields: dict[str, Any] = {"power": state.power, "brightness": state.brightness}
    if state.mireds is not None:
        fields["mireds"] = state.mireds
    elif state.xy is not None:
        fields["xy"] = state.xy
    if state.effect:
        fields["effect"] = state.effect
        fields["effect_speed"] = state.effect_speed
    return {k: v for k, v in fields.items() if v is not None}


# ---------------------------------------------------------------------------
# Errors and deadlines
# ---------------------------------------------------------------------------


class LinkLost(Exception):
    """The lamp or Windows closed the connection."""


class LampBusy(Exception):
    """Windows gives the lamp's light service to one program at a time, and
    another program on this PC has it (for example a second copy of Huetooth)."""


def describe_error(exc: BaseException) -> tuple[str, str]:
    """Map an exception to (kind, Dutch explanation for the user)."""
    from bleak.exc import (
        BleakBluetoothNotAvailableError,
        BleakCharacteristicNotFoundError,
        BleakDeviceNotFoundError,
        BleakError,
        BleakGATTProtocolError,
    )

    if isinstance(exc, LinkLost):
        return "lost", "Connection lost"
    if isinstance(exc, LampBusy):
        return "busy", "Another program on this PC is using the light (for example a second copy of Huetooth)"
    if isinstance(exc, TimeoutError):
        return "timeout", "No answer (light off, too far away or busy)"
    if isinstance(exc, BleakBluetoothNotAvailableError):
        return "bt_off", "Bluetooth is not available"
    if isinstance(exc, BleakDeviceNotFoundError):
        return "not_paired", "Windows does not know this light (any more) - pair it again"
    if isinstance(exc, BleakCharacteristicNotFoundError):
        return "gatt", "Outdated light data in Windows - the next attempt reads it again"
    if isinstance(exc, BleakGATTProtocolError):
        code = exc.args[0] if exc.args else None
        if code in (0x05, 0x08, 0x0C, 0x0F):
            return "auth", "Access denied - the pairing with Windows is invalid; pair the light again"
        if code in (0x01, 0x0A, 0x12):
            return "gatt", "Outdated light data in Windows - the next attempt reads it again"
        return "rejected", f"The light refused the command ({exc.args[-1] if exc.args else code})"
    if isinstance(exc, BleakError):
        text = str(exc)
        if "Unreachable" in text:
            return "unreachable", "Light unreachable (off, too far away or interference)"
        if "Access Denied" in text:
            return "auth", "Access denied - the pairing with Windows is invalid; pair the light again"
        if "Not connected" in text:
            return "lost", "Connection lost"
        return "error", text or type(exc).__name__
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == -2147023673:   # 0x800704C7 ERROR_CANCELLED
            return "cancelled", "Connection broke off while being set up (Windows/adapter busy)"
        return "os", f"Windows error: {exc}"
    return "error", f"{type(exc).__name__}: {exc}"


def describe_duration(seconds: float) -> str:
    """60 -> '1 min', 3600 -> '1 h', 45 -> '45 s' (for messages)."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)} h"
    if seconds >= 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds:.0f} s"


async def guarded(awaitable, timeout: float, cleanup: Callable[[Any], Any] | None = None):
    """Await with a hard deadline.

    Unlike ``asyncio.wait_for`` this never waits for the cancelled operation
    to acknowledge the cancellation, so a stuck WinRT call cannot block us.
    If an abandoned operation still succeeds later, its result is passed to
    *cleanup* (used to close links that connect too late).
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        _abandon(task, cleanup)
        raise
    if done:
        return task.result()
    _abandon(task, cleanup)
    raise TimeoutError(f"no answer within {timeout:.1f} s")


def _abandon(task: asyncio.Future, cleanup: Callable[[Any], Any] | None) -> None:
    task.cancel()

    def reap(t: asyncio.Future) -> None:
        if t.cancelled():
            return
        if t.exception() is not None or cleanup is None:
            return
        try:
            result = cleanup(t.result())
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
        except Exception:  # pragma: no cover - best effort
            log.debug("cleanup of abandoned operation failed", exc_info=True)

    task.add_done_callback(reap)


async def close_quietly(link, timeout: float = 4.0) -> None:
    try:
        await guarded(link.close(), timeout)
    except Exception as exc:
        log.debug("closing failed: %s", exc)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    """A lamp heard during a scan."""

    address: str
    name: str | None
    rssi: int | None


class Link(Protocol):
    name: str | None

    async def write(self, payload: bytes) -> None: ...

    async def read(self) -> bytes: ...

    async def close(self) -> None: ...


class BleakLink:
    """An open connection to one lamp (Windows / bleak)."""

    def __init__(self, client, on_report: Callable[[bytes], None]):
        self._client = client
        self._on_report = on_report
        self._char = None

    @property
    def name(self) -> str | None:
        try:
            return self._client.name
        except Exception:
            return None

    def _notified(self, _sender, data: bytearray) -> None:
        self._on_report(bytes(data))

    async def _setup(self, fresh_gatt: bool) -> None:
        from bleak.exc import BleakCharacteristicNotFoundError

        self._char = self._client.services.get_characteristic(CONTROL_CHAR)
        if self._char is None:
            # bleak leaves out services Windows refuses ("access denied"), which is
            # what happens while another program uses the lamp. With a cached GATT
            # table it may also just be stale, so first retry with a fresh read.
            if fresh_gatt and self._client.services.get_service(LIGHT_SERVICE) is None:
                raise LampBusy("light service not accessible")
            raise BleakCharacteristicNotFoundError(CONTROL_CHAR)
        await self._client.start_notify(self._char, self._notified)

    async def write(self, payload: bytes) -> None:
        await self._client.write_gatt_char(self._char, payload, response=True)

    async def read(self) -> bytes:
        return bytes(await self._client.read_gatt_char(self._char))

    async def close(self) -> None:
        await self._client.disconnect()


class BleakTransport:
    """Real Bluetooth via bleak's WinRT backend."""

    def __init__(self) -> None:
        self._radio = None
        self._radio_token = None
        self._radio_callbacks: list[Callable[[], None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scan_lock: asyncio.Lock | None = None

    def on_radio_change(self, callback: Callable[[], None]) -> None:
        self._radio_callbacks.append(callback)

    def _radio_event(self, _sender, _args) -> None:  # WinRT thread
        loop = self._loop
        if loop is not None and not loop.is_closed():
            for callback in self._radio_callbacks:
                loop.call_soon_threadsafe(callback)

    def _unsubscribe_radio(self) -> None:
        if self._radio is not None and self._radio_token is not None:
            try:
                self._radio.remove_state_changed(self._radio_token)
            except Exception:
                pass
        self._radio = None
        self._radio_token = None

    async def radio_state(self) -> str:
        """'on', 'off', 'no_adapter' or 'unknown'. Never raises."""
        self._loop = asyncio.get_running_loop()
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
            from winrt.windows.devices.radios import RadioState

            adapter = await guarded(BluetoothAdapter.get_default_async(), 5)
            if adapter is None:
                self._unsubscribe_radio()
                return "no_adapter"
            radio = await guarded(adapter.get_radio_async(), 5)
            if radio is None:
                return "unknown"
            if self._radio is None:
                self._radio = radio
                self._radio_token = radio.add_state_changed(self._radio_event)
            return "on" if radio.state == RadioState.ON else "off"
        except Exception as exc:
            log.debug("radio state unknown: %s", exc)
            return "unknown"

    async def open(self, address: str, *, timeout: float, fresh_gatt: bool,
                   on_report: Callable[[bytes], None], on_lost: Callable[[], None]) -> BleakLink:
        from bleak import BleakClient
        from bleak.backends.device import BLEDevice

        # A BLEDevice (instead of a plain address) makes bleak skip its own scan
        # and ask Windows for the paired device directly.
        client = BleakClient(
            BLEDevice(address, None, None),
            disconnected_callback=lambda _client: on_lost(),
            services=[LIGHT_SERVICE],
            timeout=timeout,
            winrt={"use_cached_services": not fresh_gatt},
        )
        link = BleakLink(client, on_report)
        try:
            await client.connect()
            await link._setup(fresh_gatt)
        except BaseException:
            await close_quietly(link)
            raise
        return link

    async def scan(self, timeout: float, want: set[str] | None = None) -> dict[str, Seen]:
        """Listen for Hue adverts; returns early once every wanted address was heard."""
        from bleak import BleakScanner

        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        found: dict[str, Seen] = {}
        complete = asyncio.Event()

        def heard(device, adv) -> None:
            address = device.address.upper()
            if HUE_ADVERT_UUID not in adv.service_uuids and not (want and address in want):
                return
            found[address] = Seen(address, device.name or adv.local_name, adv.rssi)
            if want and want <= found.keys():
                complete.set()

        async with self._scan_lock:
            async with BleakScanner(heard):
                try:
                    await asyncio.wait_for(complete.wait(), timeout)
                except TimeoutError:
                    pass
        return found

    async def pair(self, address: str, timeout: float = 30.0) -> str | None:
        """Pair (Windows 'Just Works') and connect once. Returns the lamp's name."""
        from bleak import BleakClient
        from bleak.backends.device import BLEDevice

        client = BleakClient(BLEDevice(address, None, None), pair=True,
                             services=[LIGHT_SERVICE], timeout=timeout)
        link = BleakLink(client, lambda data: None)
        try:
            await guarded(client.connect(), timeout + 5, cleanup=lambda _r: close_quietly(link))
            return link.name
        finally:
            await close_quietly(link)

    async def aclose(self) -> None:
        self._unsubscribe_radio()


# ---------------------------------------------------------------------------
# Lamp supervisor
# ---------------------------------------------------------------------------


class LampStatus(str, Enum):
    STARTING = "starting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    WAITING = "waiting"          # an attempt failed; next one is scheduled
    RELEASED = "released"        # disconnected on purpose
    BT_OFF = "bt_off"
    NO_ADAPTER = "no_adapter"
    NOT_PAIRED = "not_paired"
    STOPPED = "stopped"


@dataclass(frozen=True)
class LampInfo:
    """Immutable snapshot of one lamp, handed to the UI/CLI."""

    id: str
    name: str
    address: str
    status: LampStatus
    detail: str = ""
    attempt: int = 0
    retry_at: float | None = None        # time.monotonic() of the next attempt
    state: LampState = LampState()
    pending: bool = False                 # a command is waiting to be written
    latency_ms: float | None = None       # duration of the last successful write
    connected_since: float | None = None
    visible: bool | None = None           # heard advertising during the last check
    rssi: int | None = None
    reported_name: str | None = None      # the lamp's own Bluetooth name
    error_kind: str = ""                  # kind of the last connect failure (see describe_error)
    pending_state: LampState | None = None  # what the lamp will look like once the queue is written
    dropped: int = 0                      # commands given up on (expired or refused by the lamp)

    @property
    def connected(self) -> bool:
        return self.status is LampStatus.CONNECTED

    @property
    def target(self) -> LampState:
        """The state the user asked for: queued values, else what the lamp reports."""
        return self.pending_state if self.pending_state is not None else self.state


@dataclass(frozen=True)
class Timing:
    """All delays in seconds. Tests shrink these."""

    connect_timeouts: tuple[float, ...] = (10.0, 12.0, 15.0)
    connect_grace: float = 3.0               # extra hard deadline on top of bleak's own
    backoff: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0)
    write_timeout: float = 4.0
    read_timeout: float = 4.0
    close_timeout: float = 4.0
    keepalive: float = 45.0                   # idle read to catch silently dead links
    pending_ttl: float = 60.0                 # default; HueManager(pending_ttl=0) keeps them until written
    not_paired_retry: float = 15.0
    radio_poll: float = 3.0
    flap_window: float = 5.0                  # a link dying sooner counts as a failure
    min_retry_gap: float = 0.2
    # Measured: reconnecting within ~20 s after the links were closed makes
    # Windows cancel the link setup ("operation canceled") again and again until
    # the old links are fully gone. The lamp is reachable then, so retry quickly
    # instead of backing off (connects ~0.3 s after Windows is ready).
    cancelled_retry: float = 0.3
    cancelled_fast_attempts: int = 40
    pending_retry_cap: float = 2.0            # max pause between attempts while a command waits


class Lamp:
    """Keeps one lamp connected and writes the desired state to it.

    All methods run on the engine's event loop.
    """

    def __init__(self, manager: "HueManager", lamp_id: str, name: str, address: str):
        self.id = lamp_id
        self.name = name
        self.address = address.upper()
        self._mgr = manager
        self._t = manager.timing
        self._want = False
        self._stopping = False
        self._wake = asyncio.Event()
        self._pending: dict[str, tuple[Any, int, float]] = {}   # field -> (value, seq, time)
        self._waiters: list[tuple[int, asyncio.Future]] = []
        self._actions: list[str] = []
        self._link = None
        self._gen = 0
        self._lost = False
        self._failures = 0
        self._error_kind = ""
        self._fresh_gatt = False
        self._task: asyncio.Task | None = None
        self._status = LampStatus.STARTING
        self._detail = ""
        self._attempt = 0
        self._retry_at: float | None = None
        self._state = LampState()
        self._latency_ms: float | None = None
        self._connected_since: float | None = None
        self._visible: bool | None = None
        self._rssi: int | None = None
        self._reported_name: str | None = None
        self._dropped = 0
        self._last_info: LampInfo | None = None

    # -- public -------------------------------------------------------------

    def info(self) -> LampInfo:
        return LampInfo(
            id=self.id, name=self.name, address=self.address, status=self._status,
            detail=self._detail, attempt=self._attempt, retry_at=self._retry_at,
            state=self._state, pending=bool(self._pending), latency_ms=self._latency_ms,
            connected_since=self._connected_since, visible=self._visible, rssi=self._rssi,
            reported_name=self._reported_name, error_kind=self._error_kind,
            pending_state=self._pending_state(), dropped=self._dropped,
        )

    def _pending_state(self) -> LampState | None:
        """The last reported state with the queued fields laid over it."""
        if not self._pending:
            return None
        s = self._state
        values = {"power": s.power, "brightness": s.brightness, "xy": s.xy, "mireds": s.mireds,
                  "effect": s.effect, "effect_speed": s.effect_speed}
        for key, (value, _seq, _time) in self._pending.items():
            if key == "xy":
                values["xy"], values["mireds"] = value, None
            elif key == "mireds":
                values["mireds"], values["xy"] = value, None
            elif key in values:
                values[key] = value
        if ("xy" in self._pending or "mireds" in self._pending) and "effect" not in self._pending:
            values["effect"] = 0          # a plain colour ends any effect on the lamp
        return LampState(**values)

    @property
    def wanted(self) -> bool:
        return self._want

    def start(self, want: bool) -> None:
        self._want = want
        self._task = asyncio.create_task(self._supervise(), name=f"lamp-{self.id}")

    def set_wanted(self, want: bool) -> None:
        if want != self._want:
            self._want = want
            if want:
                self._failures = 0
            self._poke()

    def submit(self, fields: dict[str, Any], seq: int) -> asyncio.Future:
        """Queue desired-state fields (already normalised). Latest value wins."""
        future = asyncio.get_running_loop().create_future()
        now = time.monotonic()
        if "xy" in fields:
            self._pending.pop("mireds", None)
        if "mireds" in fields:
            self._pending.pop("xy", None)
        self._pending.pop("transition", None)   # a fade time belongs to one command
        for key, value in fields.items():
            self._pending[key] = (value, seq, now)
        self._waiters.append((seq, future))
        self._settle_waiters()
        if not self._want:
            self._want = True
            self._failures = 0
        self._poke()
        self._emit()
        return future

    def identify(self) -> None:
        if "identify" not in self._actions:
            self._actions.append("identify")
        self.set_wanted(True)
        self._poke()

    def reconnect(self, fresh: bool = True) -> None:
        """Drop the current link (if any) and connect again right away."""
        self._fresh_gatt = self._fresh_gatt or fresh
        self._failures = 0
        self._want = True
        if self._link is not None:
            self._lost = True
        self._poke()

    async def stop(self) -> None:
        self._stopping = True
        self._poke()
        task = self._task
        if task is not None and not task.done():
            if self._status in (LampStatus.CONNECTING, LampStatus.WAITING, LampStatus.BT_OFF,
                                LampStatus.NO_ADAPTER, LampStatus.NOT_PAIRED, LampStatus.STARTING):
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=self._t.close_timeout + 1)
            if not done:
                task.cancel()
                await asyncio.wait({task}, timeout=1.0)
        for _seq, future in self._waiters:
            if not future.done():
                future.set_result(False)
        self._waiters.clear()

    # -- supervisor ----------------------------------------------------------

    def _poke(self) -> None:
        self._wake.set()

    async def _supervise(self) -> None:
        try:
            while not self._stopping:
                try:
                    await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("%s: unexpected error, retrying in 1 s", self.name)
                    await asyncio.sleep(1.0)
        finally:
            link, self._link = self._link, None
            if link is not None:
                await close_quietly(link, self._t.close_timeout)
            self._connected_since = None
            self._set_status(LampStatus.STOPPED, "Stopped")

    async def _cycle(self) -> None:
        self._wake.clear()
        self._expire_pending()
        if self._stopping:
            return
        if not self._want:
            self._failures = 0
            self._attempt = 0
            self._set_status(LampStatus.RELEASED, "Released - connects again on the next command")
            await self._sleep(3600)
            return
        radio = self._mgr.radio
        if radio in ("off", "no_adapter"):
            if radio == "off":
                self._set_status(LampStatus.BT_OFF, "Bluetooth is off - turn it on in Windows")
            else:
                self._set_status(LampStatus.NO_ADAPTER, "No Bluetooth adapter - is it plugged in?")
            await self._sleep(self._t.radio_poll)
            await self._mgr.refresh_radio()
            return
        await self._connect_and_run()

    async def _connect_and_run(self) -> None:
        t = self._t
        self._attempt = self._failures + 1
        self._set_status(LampStatus.CONNECTING,
                         "Connecting..." if self._attempt == 1 else f"Connecting... (attempt {self._attempt})")
        self._gen += 1
        gen = self._gen
        self._lost = False
        timeout = t.connect_timeouts[min(self._failures, len(t.connect_timeouts) - 1)]
        started = time.monotonic()
        link = None
        try:
            link = await guarded(
                self._mgr.transport.open(
                    self.address, timeout=timeout, fresh_gatt=self._fresh_gatt,
                    on_report=lambda data: self._on_report(gen, data),
                    on_lost=lambda: self._on_lost(gen)),
                timeout + t.connect_grace,
                cleanup=lambda late: close_quietly(late, t.close_timeout),
            )
            report = await guarded(link.read(), t.read_timeout)
        except asyncio.CancelledError:
            if link is not None:
                await close_quietly(link, t.close_timeout)
            raise
        except Exception as exc:
            if link is not None:
                await close_quietly(link, t.close_timeout)
            await self._connect_failed(exc, time.monotonic() - started)
            return

        elapsed = time.monotonic() - started
        self._fresh_gatt = False
        self._error_kind = ""
        self._visible = True
        self._reported_name = getattr(link, "name", None) or self._reported_name
        self._apply_report(report)
        self._link = link
        self._connected_since = time.monotonic()
        log.info("%s: connected in %.1f s%s", self.name, elapsed,
                 f" (after {self._failures} failed attempts)" if self._failures else "")
        failures_before = self._failures
        self._failures = 0
        try:
            await self._session(link, gen)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind, text = describe_error(exc)
            if kind in ("gatt", "auth"):
                self._fresh_gatt = True
            lived = time.monotonic() - (self._connected_since or time.monotonic())
            if lived < t.flap_window:
                # Dying right after connecting: back off instead of hammering the lamp.
                self._failures = failures_before + 1
            if not self._stopping:
                log.warning("%s: connection lost (%s) - reconnecting right away", self.name, text)
        finally:
            self._link = None
            self._connected_since = None
            await close_quietly(link, t.close_timeout)

    async def _connect_failed(self, exc: BaseException, elapsed: float) -> None:
        t = self._t
        self._failures += 1
        kind, text = describe_error(exc)
        if kind in ("gatt", "auth"):
            self._fresh_gatt = True
        radio = await self._mgr.refresh_radio()
        if radio in ("off", "no_adapter") or self._stopping:
            return
        delay = t.backoff[min(self._failures - 1, len(t.backoff) - 1)]
        status = LampStatus.WAITING
        repeat = kind == self._error_kind
        self._error_kind = kind
        if kind == "cancelled" and self._failures <= t.cancelled_fast_attempts:
            delay = t.cancelled_retry
        if self._pending:
            # Someone is waiting for this lamp: keep trying almost continuously so a
            # lamp that is switched back on gets its colour within a few seconds.
            delay = min(delay, t.pending_retry_cap)
        if kind == "not_paired":
            status = LampStatus.NOT_PAIRED
            delay = max(delay, t.not_paired_retry)
        if repeat and self._failures % 10:
            level = logging.DEBUG          # one line per streak (+ every 10th) is enough for the activity log
        else:
            level = logging.INFO if self._failures <= 2 or kind == "cancelled" else logging.WARNING
        log.log(level, "%s: connecting failed (attempt %d, %.1f s): %s", self.name, self._failures, elapsed, text)
        log.debug("%s: details of the failed attempt", self.name, exc_info=exc)
        if kind in ("timeout", "unreachable") and (self._failures in (2, 5) or self._failures % 10 == 0):
            self._mgr.check_visibility()
        self._retry_at = time.monotonic() + delay
        self._set_status(status, text)
        # Short uninterruptible gap so Windows can tidy up, then a pause that a
        # new command (poke) ends early.
        gap = min(delay, t.min_retry_gap if kind != "not_paired" else 2.0)
        await asyncio.sleep(gap)
        await self._sleep(delay - gap)
        self._retry_at = None

    async def _session(self, link, gen: int) -> None:
        t = self._t
        self._set_status(LampStatus.CONNECTED, "Connected")
        last_io = time.monotonic()
        while True:
            self._wake.clear()
            if self._lost:
                raise LinkLost()
            if self._stopping or not self._want:
                return
            self._expire_pending()
            if self._actions:
                action = self._actions.pop(0)
                if action == "identify":
                    await self._identify(link)
                last_io = time.monotonic()
                continue
            if self._pending:
                await self._flush(link)
                last_io = time.monotonic()
                continue
            idle = time.monotonic() - last_io
            if idle >= t.keepalive:
                self._apply_report(await guarded(link.read(), t.read_timeout))
                last_io = time.monotonic()
                continue
            await self._sleep(t.keepalive - idle)

    async def _flush(self, link) -> None:
        snapshot = dict(self._pending)
        fields = {key: entry[0] for key, entry in snapshot.items()}
        if set(fields) <= {"transition"}:
            self._finish(snapshot, ok=True)
            return
        payload = encode_command(fields)
        started = time.monotonic()
        try:
            await guarded(link.write(payload), self._t.write_timeout)
        except Exception as exc:
            kind, text = describe_error(exc)
            if kind == "rejected":
                # The lamp understood and said no; resending the same bytes won't help.
                log.error("%s: command %s refused: %s", self.name, payload.hex(), text)
                self._finish(snapshot, ok=False)
                return
            raise
        self._latency_ms = (time.monotonic() - started) * 1000
        log.debug("%s: wrote %s in %.0f ms", self.name, payload.hex(), self._latency_ms)
        self._finish(snapshot, ok=True)

    async def _identify(self, link) -> None:
        t = self._t
        before = self._state
        steps = [({"power": True, "brightness": 254, "transition": 0}, 0.35),
                 ({"brightness": 15, "transition": 0}, 0.35)] * 2
        for fields, pause in steps:
            await guarded(link.write(encode_command(fields)), t.write_timeout)
            await asyncio.sleep(pause)
        restore = {"power": True if before.power is None else before.power, "transition": 2}
        if before.brightness:
            restore["brightness"] = before.brightness
        await guarded(link.write(encode_command(restore)), t.write_timeout)

    # -- pending bookkeeping -------------------------------------------------

    def _finish(self, snapshot: dict[str, tuple[Any, int, float]], ok: bool) -> None:
        failed_upto = -1
        for key, entry in snapshot.items():
            current = self._pending.get(key)
            if current is not None and current[1] == entry[1]:
                del self._pending[key]
                if not ok:
                    failed_upto = max(failed_upto, entry[1])
        if not ok:
            self._dropped += 1
        self._settle_waiters(failed_upto)
        self._emit()

    def _settle_waiters(self, failed_upto: int = -1) -> None:
        lowest = min((entry[1] for entry in self._pending.values()), default=None)
        keep = []
        for seq, future in self._waiters:
            if future.done():
                continue
            if seq <= failed_upto:
                future.set_result(False)
            elif lowest is None or seq < lowest:
                future.set_result(True)
            else:
                keep.append((seq, future))
        self._waiters = keep

    def _next_expiry(self) -> float | None:
        ttl = self._mgr.pending_ttl
        if not self._pending or ttl <= 0:
            return None
        return min(entry[2] for entry in self._pending.values()) + ttl

    def _expire_pending(self) -> None:
        ttl = self._mgr.pending_ttl
        if not self._pending or ttl <= 0:      # 0 = keep until the lamp has it
            return
        cutoff = time.monotonic() - ttl
        expired = [key for key, entry in self._pending.items() if entry[2] <= cutoff]
        if not expired:
            return
        failed_upto = max(self._pending[key][1] for key in expired)
        for key in expired:
            del self._pending[key]
        self._dropped += 1
        log.warning("%s: command dropped - the light was unreachable for %s", self.name, describe_duration(ttl))
        self._settle_waiters(failed_upto)
        self._emit()

    async def _sleep(self, delay: float) -> None:
        """Sleep up to *delay* s. A poke ends it early; stale commands expire meanwhile."""
        end = time.monotonic() + max(0.0, delay)
        while not self._wake.is_set():
            self._expire_pending()
            now = time.monotonic()
            if now >= end:
                break
            step = end - now
            expiry = self._next_expiry()
            if expiry is not None:
                step = min(step, max(0.05, expiry - now))
            try:
                await asyncio.wait_for(self._wake.wait(), step)
            except TimeoutError:
                pass
        self._wake.clear()

    # -- callbacks from the transport -----------------------------------------

    def _on_report(self, gen: int, data: bytes) -> None:
        if gen == self._gen:
            self._apply_report(data)

    def _on_lost(self, gen: int) -> None:
        # Only a live session counts: closing a half-open link after a failed
        # attempt also fires this callback and must not cut the back-off short.
        if gen == self._gen and self._link is not None and not self._lost:
            self._lost = True
            self._poke()

    def _apply_report(self, data: bytes) -> None:
        try:
            state = decode_report(data)
        except Exception:
            log.debug("%s: unreadable report %r", self.name, data)
            return
        if state.power is None:
            return
        if state != self._state:
            self._state = state
            self._emit()

    def _set_status(self, status: LampStatus, detail: str = "") -> None:
        if status is not self._status:
            log.debug("%s: %s -> %s (%s)", self.name, self._status.value, status.value, detail)
        self._status = status
        self._detail = detail
        if status is not LampStatus.WAITING and status is not LampStatus.NOT_PAIRED:
            self._retry_at = None
        if status is LampStatus.CONNECTED:
            self._attempt = 0
        self._emit()

    def _set_visibility(self, seen: Seen | None) -> None:
        self._visible = seen is not None
        self._rssi = seen.rssi if seen else None
        self._emit()

    def _emit(self) -> None:
        info = self.info()
        if info != self._last_info:
            self._last_info = info
            self._mgr._lamp_changed(info)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class HueManager:
    """Owns all lamps. Must be used from a single asyncio event loop."""

    def __init__(self, lamps: Iterable[tuple[str, str, str]], *, transport=None,
                 timing: Timing = Timing(), keep_connected: bool = True,
                 idle_release: float = 0.0, pending_ttl: float | None = None,
                 on_change: Callable[[LampInfo], None] | None = None):
        self.transport = transport if transport is not None else BleakTransport()
        self.timing = timing
        self.keep_connected = keep_connected
        self.idle_release = idle_release          # seconds, 0 = never
        # How long a command waits for an unreachable lamp; 0 = until it is written.
        self.pending_ttl = timing.pending_ttl if pending_ttl is None else pending_ttl
        self.on_change = on_change
        self.radio = "unknown"
        self.lamps: dict[str, Lamp] = {}
        self._lamp_specs = list(lamps)
        self._seq = 0
        self._last_activity = time.monotonic()
        self._idle_released = False
        self._tasks: set[asyncio.Task] = set()
        self._visibility_task: asyncio.Task | None = None
        self._started = False

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        self.transport.on_radio_change(self._radio_changed)
        await self.refresh_radio()
        for lamp_id, name, address in self._lamp_specs:
            self._add(lamp_id, name, address)
        self._started = True
        self._spawn(self._idle_watchdog())

    async def stop(self) -> None:
        self._started = False
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*(lamp.stop() for lamp in self.lamps.values()), return_exceptions=True)
        try:
            await guarded(self.transport.aclose(), 3)
        except Exception:
            pass

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- lamps -----------------------------------------------------------------

    def _add(self, lamp_id: str, name: str, address: str) -> Lamp:
        lamp = Lamp(self, lamp_id, name, address)
        self.lamps[lamp_id] = lamp
        lamp.start(want=self.keep_connected and not self._idle_released)
        return lamp

    def add_lamp(self, lamp_id: str, name: str, address: str) -> None:
        if lamp_id in self.lamps:
            raise ValueError(f"light {lamp_id} already exists")
        self._add(lamp_id, name, address)

    async def remove_lamp(self, lamp_id: str) -> None:
        lamp = self.lamps.pop(lamp_id, None)
        if lamp is not None:
            await lamp.stop()

    def rename(self, lamp_id: str, name: str) -> None:
        lamp = self.lamps[lamp_id]
        lamp.name = name
        lamp._emit()

    def resolve(self, targets=None) -> list[Lamp]:
        """None/'all' -> every lamp; otherwise ids, 1-based numbers or addresses."""
        if targets is None or (isinstance(targets, str) and targets.lower() in ("all", "alle", "beide", "*")):
            return list(self.lamps.values())
        if isinstance(targets, (str, int)):
            targets = [targets]
        ordered = list(self.lamps.values())
        result: list[Lamp] = []
        for target in targets:
            key = str(target).strip()
            lamp = self.lamps.get(key)
            if lamp is None and key.isdigit() and 1 <= int(key) <= len(ordered):
                lamp = ordered[int(key) - 1]
            if lamp is None:
                lamp = next((l for l in ordered if l.address == key.upper()
                             or l.name.lower() == key.lower()), None)
            if lamp is None:
                raise KeyError(f"unknown light: {target}")
            if lamp not in result:
                result.append(lamp)
        return result

    def snapshot(self) -> list[LampInfo]:
        return [lamp.info() for lamp in self.lamps.values()]

    # -- commands --------------------------------------------------------------

    def command(self, targets=None, **fields) -> dict[str, asyncio.Future]:
        """Queue a change for the target lamps; returns a future per lamp
        that resolves True once written (or superseded), False if dropped."""
        clean = normalise_fields(fields)
        lamps = self.resolve(targets)
        if not clean or not lamps:
            return {}
        self._seq += 1
        self._touch()
        return {lamp.id: lamp.submit(clean, self._seq) for lamp in lamps}

    async def apply(self, targets=None, timeout: float = 20.0, **fields) -> dict[str, bool]:
        """Queue a change and wait until every lamp has it (or *timeout*)."""
        futures = self.command(targets, **fields)
        if not futures:
            return {}
        await asyncio.wait(futures.values(), timeout=timeout)
        return {lid: bool(f.done() and f.result()) for lid, f in futures.items()}

    def toggle(self, targets=None, transition: int | None = None) -> dict[str, asyncio.Future]:
        lamps = self.resolve(targets)
        any_on = any(lamp.info().target.power for lamp in lamps)   # queued power counts too
        return self.command([l.id for l in lamps], power=not any_on, transition=transition)

    def pending_lamps(self) -> list[str]:
        return [lamp.id for lamp in self.lamps.values() if lamp._pending]

    def set_connected(self, targets=None, want: bool = True) -> None:
        self._touch()
        for lamp in self.resolve(targets):
            lamp.set_wanted(want)

    def reconnect(self, targets=None) -> None:
        self._touch()
        for lamp in self.resolve(targets):
            lamp.reconnect(fresh=True)

    def identify(self, targets=None) -> None:
        self._touch()
        for lamp in self.resolve(targets):
            lamp.identify()

    async def wait_connected(self, targets=None, timeout: float = 20.0) -> bool:
        lamps = self.resolve(targets)
        for lamp in lamps:
            lamp.set_wanted(True)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if all(l.info().connected and l.info().state.power is not None for l in lamps):
                return True
            await asyncio.sleep(0.05)
        return False

    def _touch(self) -> None:
        self._last_activity = time.monotonic()
        if self._idle_released:
            self._idle_released = False

    async def _idle_watchdog(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.idle_release <= 0 or self._idle_released:
                continue
            if time.monotonic() - self._last_activity < self.idle_release:
                continue
            lamps = [l for l in self.lamps.values() if l.wanted and not l._pending]
            if lamps:
                self._idle_released = True
                log.info("No activity: lights released (they reconnect on the next command)")
                for lamp in lamps:
                    lamp.set_wanted(False)

    # -- radio -----------------------------------------------------------------

    async def refresh_radio(self) -> str:
        state = await self.transport.radio_state()
        if state != self.radio:
            previous, self.radio = self.radio, state
            if previous != "unknown" or state != "on":
                log.info("Bluetooth: %s", {"on": "on", "off": "OFF", "no_adapter": "no adapter",
                                           "unknown": "unknown"}.get(state, state))
            for lamp in self.lamps.values():
                if state == "on":
                    lamp._failures = 0
                lamp._poke()
        return state

    def _radio_changed(self) -> None:
        self._spawn(self.refresh_radio())

    # -- diagnostics / setup -----------------------------------------------------

    def check_visibility(self) -> None:
        """Scan briefly to tell 'lamp silent' apart from 'lamp heard but refusing'."""
        if self._visibility_task is not None and not self._visibility_task.done():
            return
        self._visibility_task = self._spawn(self._check_visibility())

    async def _check_visibility(self) -> None:
        wanted = {lamp.address for lamp in self.lamps.values() if not lamp.info().connected}
        if not wanted:
            return
        try:
            found = await self.transport.scan(6.0, want=wanted)
        except Exception as exc:
            log.debug("visibility check failed: %s", exc)
            return
        for lamp in self.lamps.values():
            if lamp.address in wanted:
                seen = found.get(lamp.address)
                lamp._set_visibility(seen)
                if seen is None:
                    log.info("%s: no Bluetooth signal heard (power off or out of range?)", lamp.name)
                else:
                    log.info("%s: heard (%s dBm) but connecting fails - trying again",
                             lamp.name, seen.rssi)

    async def discover(self, timeout: float = 8.0) -> list[Seen]:
        found = await self.transport.scan(timeout)
        return sorted(found.values(), key=lambda s: -(s.rssi or -999))

    async def pair(self, address: str) -> str | None:
        return await self.transport.pair(address.upper())

    # -- events ----------------------------------------------------------------

    def _lamp_changed(self, info: LampInfo) -> None:
        if self.on_change is not None:
            try:
                self.on_change(info)
            except Exception:
                log.exception("on_change callback failed")


# ---------------------------------------------------------------------------
# Background thread wrapper (GUI)
# ---------------------------------------------------------------------------


class EngineThread:
    """Runs a HueManager on its own asyncio loop in a daemon thread.

    Keeping Bluetooth off the GUI thread also keeps WinRT in an MTA apartment,
    which is what bleak needs; the GUI never blocks on Bluetooth.
    """

    def __init__(self, factory: Callable[[], HueManager],
                 on_started: Callable[[HueManager], Any] | None = None):
        self._factory = factory
        self._on_started = on_started
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._stop_event: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.manager: HueManager | None = None
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 15.0) -> None:
        self._thread = threading.Thread(target=self._main, name="hue-engine", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("the Bluetooth engine did not start in time")
        if self._error is not None:
            raise self._error

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._amain())
        finally:
            try:
                # Abandoned Bluetooth calls may still be pending; give them a
                # moment to honour cancellation, then close regardless.
                leftovers = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in leftovers:
                    task.cancel()
                if leftovers:
                    loop.run_until_complete(asyncio.wait(leftovers, timeout=1.0))
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _amain(self) -> None:
        self._stop_event = asyncio.Event()
        try:
            self.manager = self._factory()
            await self.manager.start()
            if self._on_started is not None:
                result = self._on_started(self.manager)
                if asyncio.iscoroutine(result):
                    await result
        except BaseException as exc:  # report to the starting thread
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        await self._stop_event.wait()
        try:
            await guarded(self.manager.stop(), 6)
        except Exception:
            log.debug("engine stop timed out", exc_info=True)

    def call(self, fn: Callable, *args, **kwargs) -> concurrent.futures.Future:
        """Run ``fn(*args)`` on the engine loop; coroutines are awaited."""
        result: concurrent.futures.Future = concurrent.futures.Future()

        def runner() -> None:
            try:
                value = fn(*args, **kwargs)
            except BaseException as exc:
                result.set_exception(exc)
                return
            if asyncio.iscoroutine(value):
                task = asyncio.ensure_future(value)

                def transfer(t: asyncio.Future) -> None:
                    if t.cancelled():
                        result.cancel()
                    elif t.exception() is not None:
                        result.set_exception(t.exception())
                    else:
                        result.set_result(t.result())

                task.add_done_callback(transfer)
            else:
                result.set_result(value)

        if self.loop is None or self.loop.is_closed():
            result.set_exception(RuntimeError("engine is not running"))
        else:
            self.loop.call_soon_threadsafe(runner)
        return result

    def stop(self, timeout: float = 7.0) -> None:
        if self.loop is not None and not self.loop.is_closed() and self._stop_event is not None:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout)


# ---------------------------------------------------------------------------
# Local control channel: single instance + instant CLI through the open GUI
# ---------------------------------------------------------------------------

IPC_PORT = 47819
APP_ID = "hue-controller"


def info_to_dict(info: LampInfo) -> dict[str, Any]:
    data = asdict(info)
    data["status"] = info.status.value
    data.pop("retry_at", None)
    data.pop("connected_since", None)
    return data


def fields_from_json(raw: dict[str, Any]) -> dict[str, Any]:
    fields = dict(raw)
    if isinstance(fields.get("xy"), list):
        fields["xy"] = tuple(fields["xy"])
    return fields


class IpcServer:
    """JSON-lines server on 127.0.0.1 used by hue_cli.py and a 2nd GUI start."""

    def __init__(self, manager: HueManager, on_show: Callable[[], None] | None = None,
                 port: int = IPC_PORT, extra: Callable[[dict], Any] | None = None,
                 on_quit: Callable[[], None] | None = None):
        self.manager = manager
        self.on_show = on_show
        self.on_quit = on_quit
        self.port = port
        self.extra = extra
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> bool:
        try:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
            return True
        except OSError as exc:
            log.warning("Control channel not started (port %d in use?): %s", self.port, exc)
            return False

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            request = json.loads(line.decode("utf-8"))
            response = await self._dispatch(request)
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        mgr = self.manager
        timeout = float(req.get("timeout", 15))
        if op == "ping":
            return {"ok": True, "app": APP_ID, "version": __version__, "pid": os.getpid()}
        if op == "show":
            if self.on_show:
                self.on_show()
            return {"ok": True}
        if op == "status":
            return {"ok": True, "lamps": [info_to_dict(i) for i in mgr.snapshot()]}
        if op in ("set", "set_many", "toggle"):
            futures: dict[str, asyncio.Future] = {}
            if op == "set":
                futures = mgr.command(req.get("targets"), **fields_from_json(req.get("fields", {})))
            elif op == "set_many":
                for item in req.get("commands", []):
                    futures.update(mgr.command(item.get("targets"), **fields_from_json(item.get("fields", {}))))
            else:
                futures = mgr.toggle(req.get("targets"), transition=req.get("transition"))
            if futures:
                await asyncio.wait(futures.values(), timeout=timeout)
            # 'pending' = queued; the app writes it as soon as the lamp connects.
            states = {k: ("ok" if f.result() else "failed") if f.done() else "pending"
                      for k, f in futures.items()}
            return {"ok": bool(states) and all(s == "ok" for s in states.values()),
                    "results": {k: s == "ok" for k, s in states.items()}, "states": states}
        if op == "quit":
            if self.on_quit:
                self.on_quit()
                return {"ok": True}
            return {"ok": False, "error": "quitting is not supported here"}
        if op == "identify":
            mgr.identify(req.get("targets"))
            return {"ok": True}
        if op == "reconnect":
            mgr.reconnect(req.get("targets"))
            return {"ok": True}
        if self.extra is not None:
            result = self.extra(req)
            if asyncio.iscoroutine(result):
                result = await result
            if result is not None:
                return result
        return {"ok": False, "error": f"unknown command: {op}"}


def ipc_request(request: dict[str, Any], port: int = IPC_PORT,
                connect_timeout: float = 0.4, reply_timeout: float = 20.0) -> dict[str, Any] | None:
    """Send one request to a running controller. ``None`` if none is running."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=connect_timeout) as sock:
            sock.settimeout(reply_timeout)
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        reply = json.loads(buffer.decode("utf-8"))
        return reply if isinstance(reply, dict) else None
    except (OSError, ValueError):
        return None


def controller_running(port: int = IPC_PORT) -> bool:
    reply = ipc_request({"op": "ping"}, port=port, reply_timeout=2.0)
    return bool(reply and reply.get("app") == APP_ID)


def quit_controller(port: int = IPC_PORT, timeout: float = 15.0) -> bool:
    """Ask a running Huetooth to quit and wait until its process is gone
    (used by the installer before it replaces the program files)."""
    reply = ipc_request({"op": "ping"}, port=port, reply_timeout=2.0)
    if not reply or reply.get("app") != APP_ID:
        return True
    handle = None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        if isinstance(reply.get("pid"), int):
            handle = kernel32.OpenProcess(0x00100000, False, reply["pid"])   # SYNCHRONIZE
        ipc_request({"op": "quit"}, port=port, reply_timeout=5.0)
        if handle:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    except Exception:
        log.debug("waiting for the app to quit failed", exc_info=True)
    finally:
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
    # Older version without a pid: wait until it stops answering.
    deadline = time.monotonic() + timeout
    while controller_running(port):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


# ---------------------------------------------------------------------------
# Simulation (tests and ``--sim``)
# ---------------------------------------------------------------------------


class SimDevice:
    """A fake lamp that speaks the same TLV protocol."""

    def __init__(self, address: str, name: str = "Sim light"):
        self.address = address.upper()
        self.name = name
        self.state: dict[str, Any] = {"power": True, "brightness": 200, "xy": (0.3, 0.15),
                                      "mireds": None, "effect": 0, "effect_speed": None}
        self.reachable = True
        self.paired = True
        self.links: list["SimLink"] = []
        self.payloads: list[bytes] = []

    def report(self) -> bytes:
        s = self.state
        fields = {"power": s["power"], "brightness": s["brightness"]}
        if s["mireds"] is not None:
            fields["mireds"] = s["mireds"]
        else:
            fields["xy"] = s["xy"]
        if s["effect"]:
            fields["effect"] = s["effect"]
            fields["effect_speed"] = s["effect_speed"] or 128
        return encode_command(fields)

    def apply(self, payload: bytes) -> None:
        """Mimics the real lamps, including their quirks (see encode_command)."""
        self.payloads.append(payload)
        new = decode_report(payload)
        kinds = _tlv_kinds(payload)
        colour = new.mireds is not None or new.xy is not None
        effect_off = TLV_EFFECT in kinds and new.effect == 0
        if new.power is not None:
            self.state["power"] = new.power
        if new.brightness is not None:
            self.state["brightness"] = new.brightness
        if colour and not effect_off:
            if new.mireds is not None:
                self.state["mireds"], self.state["xy"] = new.mireds, None
            else:
                self.state["xy"], self.state["mireds"] = new.xy, None
            if TLV_EFFECT not in kinds:
                self.state["effect"] = 0          # a plain colour ends any effect
        if TLV_EFFECT in kinds:
            self.state["effect"] = new.effect
            if new.effect_speed is not None:
                self.state["effect_speed"] = new.effect_speed
        self.notify()

    def external_change(self, **fields) -> None:
        """Pretend the phone app changed the lamp."""
        self.apply(encode_command(fields))

    def notify(self) -> None:
        data = self.report()
        for link in list(self.links):
            link.deliver(data)


def _tlv_kinds(data: bytes) -> set[int]:
    kinds, i = set(), 0
    while i + 2 <= len(data):
        kinds.add(data[i])
        i += 2 + data[i + 1]
    return kinds


class SimLink:
    def __init__(self, transport: "SimTransport", device: SimDevice,
                 on_report: Callable[[bytes], None], on_lost: Callable[[], None]):
        self._transport = transport
        self.device = device
        self.name = device.name
        self._on_report = on_report
        self._on_lost = on_lost
        self.closed = False
        self._loop = asyncio.get_running_loop()

    def deliver(self, data: bytes) -> None:
        if not self.closed:
            self._loop.call_soon(self._on_report, data)

    async def write(self, payload: bytes) -> None:
        from bleak.exc import BleakError, BleakGATTProtocolError

        tr = self._transport
        tr.write_count[self.device.address] += 1
        if self.closed:
            raise BleakError("Not connected")
        outcome = tr._next(tr.write_plan, self.device.address, tr.write_fail_rate,
                           ("error", "timeout", "drop"))
        await asyncio.sleep(tr._delay(tr.latency))
        if outcome == "error":
            raise BleakError("Could not write value: Unreachable")
        if outcome == "timeout":
            await asyncio.sleep(3600)
        if outcome == "hang":
            await _ignore_cancel(tr.hang_seconds)
        if outcome == "reject":
            raise BleakGATTProtocolError(0xFC)
        if outcome == "drop":
            self.drop()
            raise BleakError("Not connected")
        if self.closed:
            raise BleakError("Not connected")
        self.device.apply(payload)

    async def read(self) -> bytes:
        from bleak.exc import BleakError

        if self.closed:
            raise BleakError("Not connected")
        await asyncio.sleep(self._transport._delay(self._transport.latency))
        return self.device.report()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self in self.device.links:
            self.device.links.remove(self)
        self._on_lost()   # bleak also reports our own disconnects

    def drop(self) -> None:
        """Simulate the lamp/Windows dropping the link."""
        if self.closed:
            return
        self.closed = True
        if self in self.device.links:
            self.device.links.remove(self)
        self._loop.call_soon(self._on_lost)


async def _ignore_cancel(seconds: float) -> None:
    """Sleep that swallows cancellation, like a stuck WinRT call."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            await asyncio.sleep(end - time.monotonic())
        except asyncio.CancelledError:
            continue


class SimTransport:
    """Scriptable fake Bluetooth for tests and demo mode.

    ``connect_plan[address]`` / ``write_plan[address]`` hold outcomes that are
    used one by one ('ok', 'timeout', 'error', 'notfound', 'gatt', 'hang',
    'drop', 'reject'); when empty, random failures follow the *_rate values.
    """

    def __init__(self, devices: Iterable[SimDevice], *, seed: int | None = None,
                 latency: tuple[float, float] = (0.0, 0.0),
                 connect_delay: tuple[float, float] = (0.0, 0.0),
                 connect_fail_rate: float = 0.0, write_fail_rate: float = 0.0,
                 drop_rate: float = 0.0):
        self.devices = {d.address: d for d in devices}
        self.rng = random.Random(seed)
        self.latency = latency
        self.connect_delay = connect_delay
        self.connect_fail_rate = connect_fail_rate
        self.write_fail_rate = write_fail_rate
        self.drop_rate = drop_rate
        self.hang_seconds = 1.0      # how long a 'hang' ignores cancellation
        self.radio = "on"
        self.connect_plan: dict[str, list[str]] = defaultdict(list)
        self.write_plan: dict[str, list[str]] = defaultdict(list)
        self.open_count: dict[str, int] = defaultdict(int)
        self.write_count: dict[str, int] = defaultdict(int)
        self.fresh_gatt_opens: dict[str, int] = defaultdict(int)
        self._radio_callbacks: list[Callable[[], None]] = []
        self._dropper: asyncio.Task | None = None

    def _delay(self, span: tuple[float, float]) -> float:
        low, high = span
        return low if high <= low else self.rng.uniform(low, high)

    def _next(self, plan: dict[str, list[str]], address: str, rate: float, failures: tuple[str, ...]) -> str:
        if plan[address]:
            return plan[address].pop(0)
        if rate and self.rng.random() < rate:
            return self.rng.choice(failures)
        return "ok"

    def links(self, address: str) -> list[SimLink]:
        return list(self.devices[address.upper()].links)

    def set_radio(self, state: str) -> None:
        self.radio = state
        if state != "on":   # like Windows: switching Bluetooth off drops every link
            for device in self.devices.values():
                for link in list(device.links):
                    link.drop()
        for callback in self._radio_callbacks:
            callback()

    def on_radio_change(self, callback: Callable[[], None]) -> None:
        self._radio_callbacks.append(callback)

    async def radio_state(self) -> str:
        if self.drop_rate and self._dropper is None:
            self._dropper = asyncio.create_task(self._random_drops())
        return self.radio

    async def _random_drops(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            for device in self.devices.values():
                for link in list(device.links):
                    if self.rng.random() < self.drop_rate:
                        link.drop()

    async def open(self, address: str, *, timeout: float, fresh_gatt: bool,
                   on_report: Callable[[bytes], None], on_lost: Callable[[], None]) -> SimLink:
        from bleak.exc import (BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason,
                               BleakCharacteristicNotFoundError, BleakDeviceNotFoundError, BleakError)

        address = address.upper()
        self.open_count[address] += 1
        if fresh_gatt:
            self.fresh_gatt_opens[address] += 1
        if self.radio != "on":
            raise BleakBluetoothNotAvailableError("radio off", BleakBluetoothNotAvailableReason.POWERED_OFF)
        device = self.devices.get(address)
        if device is None or not device.paired:
            raise BleakDeviceNotFoundError(address, f"Device with address {address} was not found.")
        outcome = self._next(self.connect_plan, address, self.connect_fail_rate, ("timeout", "error"))
        await asyncio.sleep(self._delay(self.connect_delay))
        if outcome == "timeout":
            await asyncio.sleep(timeout)
            raise TimeoutError("sim connect timeout")
        if not device.reachable:
            # Like Windows: keep trying during the attempt and connect as soon as the lamp is back.
            deadline = time.monotonic() + timeout
            while not device.reachable:
                if time.monotonic() >= deadline:
                    raise TimeoutError("sim connect timeout")
                await asyncio.sleep(0.05)
        if outcome == "hang":
            await _ignore_cancel(self.hang_seconds)
        if outcome == "error":
            raise BleakError("Could not get GATT services: Unreachable")
        if outcome == "notfound":
            raise BleakDeviceNotFoundError(address, "not found")
        if outcome == "gatt":
            raise BleakCharacteristicNotFoundError(CONTROL_CHAR)
        if outcome == "cancelled":
            raise OSError(0, "The operation was canceled by the user.", None, -2147023673)
        link = SimLink(self, device, on_report, on_lost)
        device.links.append(link)
        if outcome == "busy":
            # Like bleak: the link opened, setup failed, closing it reports a disconnect.
            await link.close()
            raise LampBusy("light service not accessible")
        return link

    async def scan(self, timeout: float, want: set[str] | None = None) -> dict[str, Seen]:
        await asyncio.sleep(min(timeout, 0.05))
        return {a: Seen(a, d.name, -60) for a, d in self.devices.items() if d.reachable}

    async def pair(self, address: str, timeout: float = 30.0) -> str | None:
        device = self.devices.get(address.upper())
        if device is None:
            from bleak.exc import BleakDeviceNotFoundError
            raise BleakDeviceNotFoundError(address, "not found")
        device.paired = True
        return device.name

    async def aclose(self) -> None:
        if self._dropper is not None:
            self._dropper.cancel()


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def setup_logging(log_file: Path | None = None, level: int = logging.INFO,
                  console: bool = False) -> None:
    root = logging.getLogger("hue")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3,
                                                       encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)
        root.addHandler(handler)
    if console:
        stream = logging.StreamHandler()
        stream.setLevel(level)
        stream.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%H:%M:%S"))
        root.addHandler(stream)
