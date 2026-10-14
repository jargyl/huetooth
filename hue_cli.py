"""Command line for Monitor Lights (control your Hue Bluetooth lights from a terminal).

If the Monitor Lights app is open, commands go through it (instant, no new
Bluetooth connection). Otherwise this script connects by itself, and a light
it cannot reach in time is handed to an invisible Monitor Lights that applies
the command as soon as the light connects.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

import hue_colors as colors
import hue_config as config
from hue_engine import (EFFECTS, IPC_PORT, HueManager, LampStatus, controller_running, ipc_request,
                        setup_logging)

# The ready-made download ships this as lights.exe next to "Monitor Lights.exe".
FROZEN = getattr(sys, "frozen", False)
APP_EXE = "Monitor Lights.exe"
PROG = "lights" if FROZEN else "py hue_cli.py"

EXAMPLES = [
    ("", "all lights purple (the default scene)"),
    ("on", ""),
    ("off -l 2", "only light 2"),
    ("toggle", ""),
    ("brightness 40", "40 %"),
    ("color red", ""),
    ("color #ff8800 -l 1 -b 60", "any color, light 1, 60 % bright"),
    ("white 2700K", "or: white warm, white daylight"),
    ("effect candle", "or: effect off"),
    ("scene Evening", ""),
    ("status", "connection and state of each light"),
    ("blink -l 1", "find out which light is which"),
    ("list", "all colors, whites, effects and scenes"),
]


def examples_text() -> str:
    rows = [(f"{PROG} {command}".rstrip(), note) for command, note in EXAMPLES]
    width = max(len(row) for row, _ in rows) + 3
    return "examples:\n" + "\n".join(f"  {row.ljust(width)}{note}".rstrip() for row, note in rows)

# English commands, plus the Dutch words older versions used.
COMMANDS = {
    "on": "on", "aan": "on", "off": "off", "uit": "off",
    "toggle": "toggle", "wissel": "toggle",
    "brightness": "brightness", "bri": "brightness", "dim": "brightness", "helderheid": "brightness",
    "color": "color", "colour": "color", "kleur": "color",
    "white": "white", "temp": "white", "wit": "white",
    "effect": "effect", "scene": "scene", "scenes": "scene", "scène": "scene",
    "status": "status", "blink": "identify", "identify": "identify", "knipper": "identify",
    "list": "list", "lijst": "list",
}


class UsageError(Exception):
    pass


def bri_of(text: str) -> int:
    pct = float(str(text).rstrip("%").replace(",", "."))
    if not 0 <= pct <= 100:
        raise UsageError("brightness must be between 0 and 100 (%)")
    return max(1, min(254, round(pct * 254 / 100)))


def effect_of(text: str) -> int:
    raw = text.strip().lower()
    if raw in ("off", "stop", "none", "0", "uit", "geen"):
        return 0
    for eid, name, alias in EFFECTS:
        if raw in (name.lower(), alias, str(eid)):
            return eid
    names = ", ".join(name.lower() for _, name, _ in EFFECTS)
    raise UsageError(f"unknown effect '{text}'. Choose from: {names}, off")


def resolve_ids(cfg: dict, light: str) -> list[str]:
    lamps = cfg["lamps"]
    key = light.strip().lower()
    if key in ("all", "both", "*", "alle", "beide"):
        return [l["id"] for l in lamps]
    ids = []
    for part in key.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(lamps):
            match = lamps[int(part) - 1]
        else:
            match = next((l for l in lamps if part in (l["id"].lower(), l["name"].lower(),
                                                       l["address"].lower())), None)
        if match is None:
            raise UsageError(f"unknown light '{part}' (use 1..{len(lamps)}, a name or 'all')")
        ids.append(match["id"])
    return ids


def build_plan(args, cfg) -> tuple[str, list[tuple[list[str], dict]]]:
    """Returns (kind, commands); kind is 'set', 'toggle', 'status', 'identify' or 'list'."""
    command = COMMANDS.get((args.command or "scene").lower())
    if command is None:
        raise UsageError(f"unknown command '{args.command}' (see: {PROG} -h)")
    value = " ".join(args.value).strip()
    ids = resolve_ids(cfg, args.light)
    extra = {}
    if args.fade is not None:
        extra["transition"] = round(args.fade * 10)
    if args.brightness is not None:
        extra["brightness"] = bri_of(args.brightness)

    if command in ("status", "identify", "list"):
        return command, [(ids, {})]
    if command == "toggle":
        return "toggle", [(ids, extra)]
    if command == "on":
        return "set", [(ids, {"power": True, **extra})]
    if command == "off":
        return "set", [(ids, {"power": False, **extra})]
    if command == "brightness":
        if not value:
            raise UsageError("give a brightness, e.g. 'brightness 40'")
        return "set", [(ids, {**extra, "power": True, "brightness": bri_of(value)})]
    if command == "color":
        if not value:
            raise UsageError("give a color, e.g. 'color red', 'color #ff8800' or 'color 0.3,0.15'")
        try:
            xy = colors.parse_color(value)
        except ValueError:
            names = ", ".join(n.lower() for n, _ in colors.COLOR_PRESETS)
            raise UsageError(f"unknown color '{value}'. Use #rrggbb, x,y or: {names}") from None
        return "set", [(ids, {**extra, "power": True, "xy": xy})]
    if command == "white":
        try:
            mireds = colors.parse_white(value or "warm")
        except ValueError:
            raise UsageError(f"unknown white '{value}', e.g. 2700K or warm") from None
        return "set", [(ids, {**extra, "power": True, "mireds": mireds})]
    if command == "effect":
        effect = effect_of(value or "candle")
        if effect == 0:
            return "set", [(ids, {"effect": 0, **extra})]
        fields = {"power": True, "effect": effect, "effect_speed": bri_of(args.speed), **extra}
        fields.update({1: {"mireds": 455}, 2: {"xy": (0.58, 0.39)}}.get(effect, {}))
        return "set", [(ids, fields)]
    # scene
    name = value or config.DEFAULT_SCENE
    scene = config.find_scene(cfg, name)
    if scene is None:
        names = ", ".join(s["name"] for s in config.all_scenes(cfg))
        raise UsageError(f"unknown scene '{name}'. Choose from: {names}")
    per_lamp = config.scene_fields(scene, [l["id"] for l in cfg["lamps"]])
    return "set", [([lid], {**fields, **extra}) for lid, fields in per_lamp.items() if lid in ids]


def lamp_name(cfg, lamp_id):
    return next((l["name"] for l in cfg["lamps"] if l["id"] == lamp_id), lamp_id)


def print_results(cfg, states: dict[str, str], timings: dict[str, float] | None = None,
                  details: dict[str, str] | None = None) -> int:
    """states: light id -> 'ok', 'pending' (queued, applied once connected) or 'failed'."""
    labels = {"ok": "OK    ", "pending": "QUEUED", "failed": "FAILED"}
    for lamp_id, state in states.items():
        line = f"  {labels[state]} {lamp_name(cfg, lamp_id)}"
        if state == "ok" and timings and lamp_id in timings:
            line += f"  ({timings[lamp_id]:.1f} s)"
        if state == "pending":
            line += "  - applied as soon as the light connects"
        if state == "failed" and details and details.get(lamp_id):
            line += f"  - {details[lamp_id]}"
        print(line)
    if not states:
        return 2
    return 1 if any(s == "failed" for s in states.values()) else 0


def describe_state(info: dict) -> str:
    state = info.get("state") or {}
    if state.get("power") is None:
        return "state unknown"
    if not state.get("power"):
        return "off"
    text = f"on, {max(1, round((state.get('brightness') or 1) * 100 / 254))}%"
    if state.get("mireds"):
        text += f", white {colors.mireds_to_kelvin(state['mireds'])} K"
    elif state.get("xy"):
        x, y = state["xy"]
        text += f", color {colors.to_hex(colors.xy_to_rgb(x, y))} (x {x:.3f}, y {y:.3f})"
    if state.get("effect"):
        name = next((n for eid, n, _ in EFFECTS if eid == state["effect"]), "effect")
        text += f", effect {name.lower()}"
    return text


# -- via the running app ----------------------------------------------------------


def via_app(kind, plan, cfg, args) -> int:
    started = time.monotonic()
    if kind == "status":
        reply = ipc_request({"op": "status"}, port=args.port, reply_timeout=5)
        if not reply:
            return -1
        for info in reply["lamps"]:
            if info["id"] in plan[0][0]:
                print(f"  {info['name']}: {info['status']} - {describe_state(info)}")
        return 0
    if kind == "identify":
        reply = ipc_request({"op": "identify", "targets": plan[0][0]}, port=args.port)
        return -1 if reply is None else 0
    # The app keeps a command queued for a light that is not connected, so there
    # is no need to wait long here.
    timeout = args.timeout if args.timeout is not None else 8.0
    if kind == "toggle":
        request = {"op": "toggle", "targets": plan[0][0], "timeout": timeout,
                   "transition": plan[0][1].get("transition")}
    else:
        request = {"op": "set_many", "timeout": timeout,
                   "commands": [{"targets": ids, "fields": fields} for ids, fields in plan]}
    reply = ipc_request(request, port=args.port, reply_timeout=timeout + 5)
    if reply is None:
        return -1
    if "results" not in reply:
        print(f"Error: {reply.get('error')}")
        return 1
    states = reply.get("states") or {k: ("ok" if v else "failed") for k, v in reply["results"].items()}
    elapsed = time.monotonic() - started
    print(f"Through the open app ({elapsed * 1000:.0f} ms):")
    return print_results(cfg, states)


# -- hand-over to an invisible app ---------------------------------------------------


def start_background_app(config_path: Path, port: int) -> None:
    """Start Monitor Lights invisibly (no window, no console)."""
    if FROZEN:
        here = Path(sys.executable).resolve().parent
        command = [str(here / APP_EXE), "--background"]
    else:
        here = Path(__file__).resolve().parent
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw if pythonw.exists() else sys.executable)
        command = [exe, str(here / "hue_controller.py"), "--background"]
    if config_path != config.CONFIG_PATH:
        command += ["--config", str(config_path)]
    if port != IPC_PORT:
        command += ["--port", str(port)]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command, cwd=str(here), creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def hand_off(commands: list[tuple[list[str], dict]], config_path: Path, port: int) -> bool:
    """Give queued commands to (a freshly started, invisible) Monitor Lights."""
    if not controller_running(port):
        start_background_app(config_path, port)
        deadline = time.monotonic() + 20
        while not controller_running(port):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.25)
    reply = ipc_request({"op": "set_many", "timeout": 0.5,
                         "commands": [{"targets": ids, "fields": fields} for ids, fields in commands]},
                        port=port, reply_timeout=10)
    return bool(reply and "states" in reply)


# -- standalone ----------------------------------------------------------------------


def progress_text(info) -> str | None:
    if info.status is LampStatus.CONNECTING:
        return "connecting..." if info.attempt <= 1 else f"connecting (attempt {info.attempt})..."
    if info.status is LampStatus.WAITING:
        if info.error_kind == "cancelled":
            return "Windows is still closing an earlier connection, trying again..."
        if info.error_kind == "busy":
            return "another program on this PC is using the light; close it first"
        return f"{info.detail}; trying again..."
    if info.status in (LampStatus.BT_OFF, LampStatus.NO_ADAPTER, LampStatus.NOT_PAIRED):
        return info.detail
    return None


async def report_progress(manager, ids, finished: asyncio.Event) -> None:
    """After a short while, tell the user what is taking so long."""
    shown: dict[str, str] = {}
    try:
        await asyncio.wait_for(finished.wait(), 2.5)
        return
    except TimeoutError:
        pass
    while not finished.is_set():
        for lamp_id in ids:
            info = manager.lamps[lamp_id].info()
            text = None if info.connected else progress_text(info)
            if text and shown.get(lamp_id) != text:
                print(f"  ... {info.name}: {text}", flush=True)
                shown[lamp_id] = text
        try:
            await asyncio.wait_for(finished.wait(), 1.0)
        except TimeoutError:
            pass


async def standalone(kind, plan, cfg, args) -> int:
    timeout = args.timeout if args.timeout is not None else 15.0
    manager = HueManager(config.lamp_specs(cfg), keep_connected=False, pending_ttl=0)
    await manager.start()
    started = time.monotonic()
    finished = asyncio.Event()
    reporter = asyncio.create_task(report_progress(manager, plan[0][0] if kind != "set" else
                                                   sorted({i for ids, _ in plan for i in ids}), finished))
    leftover: list[tuple[list[str], dict]] = []
    try:
        ids = plan[0][0]
        if kind == "status":
            await manager.wait_connected(ids, timeout=timeout)
            for lamp_id in ids:
                info = manager.lamps[lamp_id].info()
                state = {"power": info.state.power, "brightness": info.state.brightness,
                         "xy": info.state.xy, "mireds": info.state.mireds, "effect": info.state.effect}
                status = "connected" if info.connected else info.detail or info.status.value
                print(f"  {info.name}: {status} - {describe_state({'state': state})}")
            return 0
        if kind == "identify":
            ok = await manager.wait_connected(ids, timeout=timeout)
            manager.identify(ids)
            await asyncio.sleep(2.2)
            return 0 if ok else 1
        if kind == "toggle":
            await manager.wait_connected(ids, timeout=timeout)
            futures = manager.toggle(ids, transition=plan[0][1].get("transition"))
        else:
            futures = {}
            for targets, fields in plan:
                futures.update(manager.command(targets, **fields))
        timings: dict[str, float] = {}
        for lamp_id, future in futures.items():
            future.add_done_callback(lambda _f, l=lamp_id: timings.setdefault(l, time.monotonic() - started))
        if futures:
            await asyncio.wait(futures.values(), timeout=timeout)
        states = {lid: ("ok" if f.result() else "failed") if f.done() else "pending"
                  for lid, f in futures.items()}
        details = {lid: manager.lamps[lid].info().detail for lid in states}
        for lamp_id, state in states.items():
            if state != "pending":
                continue
            if kind == "toggle":
                fields = {"power": bool(manager.lamps[lamp_id].info().target.power)}
            else:
                fields = {}
                for targets, plan_fields in plan:          # targets are light ids (see build_plan)
                    if lamp_id in targets:
                        fields.update(plan_fields)
            leftover.append(([lamp_id], fields))
    finally:
        finished.set()
        await reporter
        await manager.stop()

    if leftover:
        if args.no_background or not hand_off(leftover, args.config, args.port):
            for (lamp_id,), _fields in leftover:
                states[lamp_id] = "failed"
                if not args.no_background:
                    details[lamp_id] = "could not hand the command to Monitor Lights in the background"
        else:
            print("Not every light was reachable: Monitor Lights finishes this in the background.")
    return print_results(cfg, states, timings, details)


def opened_by_double_click() -> bool:
    """True when lights.exe was started from Explorer (not a terminal, script or Stream Deck)."""
    if not FROZEN:
        return False
    try:
        import ctypes
        import os
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, os.getppid())   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            size = wintypes.DWORD(1024)
            name = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                return False
            return Path(name.value).name.lower() == "explorer.exe"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if argv is None and len(sys.argv) == 1 and opened_by_double_click():
        print("lights.exe controls your lights from a terminal, a shortcut or a script.\n"
              "Open the app itself with \"Monitor Lights.exe\".\n")
        print(examples_text())
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
        return 0
    parser = argparse.ArgumentParser(
        prog=PROG, description="Control your Philips Hue Bluetooth lights from the command line.",
        epilog=examples_text(), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", help="on, off, toggle, brightness, color, white, effect, "
                                                   "scene, status, blink, list (default: scene)")
    parser.add_argument("value", nargs="*", help="value for the command")
    parser.add_argument("-l", "--light", default="all", help="1, 2, a name, or 'all' (default)")
    parser.add_argument("-b", "--brightness", help="brightness in %% for color/white/effect/scene")
    parser.add_argument("--speed", default="50", help="effect speed in %% (default 50)")
    parser.add_argument("--fade", type=float, help="fade time in seconds, e.g. 0 or 1.5")
    parser.add_argument("--timeout", type=float, default=None,
                        help="how long to wait for the lights (default 15 s; through the app 8 s)")
    parser.add_argument("--no-background", action="store_true",
                        help="do not hand unreachable lights to Monitor Lights in the background")
    parser.add_argument("-v", "--verbose", action="store_true", help="show connection details")
    # Hidden: testing helpers and the option names of older (Dutch) versions.
    parser.add_argument("--direct", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, default=config.CONFIG_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=IPC_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--lamp", dest="light", help=argparse.SUPPRESS)
    parser.add_argument("--helderheid", dest="brightness", help=argparse.SUPPRESS)
    parser.add_argument("--snelheid", dest="speed", help=argparse.SUPPRESS)
    parser.add_argument("--overgang", dest="fade", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--niet-achtergrond", dest="no_background", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    cfg = config.load_config(args.config)
    if not cfg["lamps"] and COMMANDS.get((args.command or "").lower()) != "list":
        print("No lights set up yet. Start the app first (Monitor Lights) and click 'Find lights'.")
        return 2
    try:
        kind, plan = build_plan(args, cfg)
    except UsageError as exc:
        print(f"Error: {exc}")
        return 2

    if kind == "list":
        print("Scenes:  " + ", ".join(s["name"] for s in config.all_scenes(cfg)))
        print("Effects: " + ", ".join(name.lower() for _, name, _ in EFFECTS) + ", off")
        print("Colors:  " + ", ".join(n.lower() for n, _ in colors.COLOR_PRESETS) + ", #rrggbb, x,y")
        print("Whites:  " + ", ".join(f"{n.lower()} ({k} K)" for n, k in colors.WHITE_PRESETS))
        print("Lights:  " + (", ".join(f"{i + 1} = {l['name']}" for i, l in enumerate(cfg["lamps"]))
                             or "none yet"))
        return 0
    if kind == "set" and not plan:
        print("Nothing to do for the chosen light(s).")
        return 0

    setup_logging(config.LOG_DIR / "cli.log", level=logging.INFO if args.verbose else logging.WARNING,
                  console=True)
    if not args.direct and controller_running(args.port):
        code = via_app(kind, plan, cfg, args)
        if code >= 0:
            return code
        print("The app did not answer; connecting directly.")
    return asyncio.run(standalone(kind, plan, cfg, args))


if __name__ == "__main__":
    sys.exit(main())
