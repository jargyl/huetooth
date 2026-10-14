"""Unit tests for the Hue engine, using the scriptable SimTransport.

Run:  py -m unittest discover -s tests -v
"""

import asyncio
import json
import logging
import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hue_colors as colors  # noqa: E402
import hue_config as config  # noqa: E402
from hue_engine import (  # noqa: E402
    EngineThread, HueManager, IpcServer, LampState, LampStatus, SimDevice, SimTransport, Timing,
    decode_report, encode_command, ipc_request, normalise_fields,
)

FAST = Timing(connect_timeouts=(0.3,), connect_grace=0.2, backoff=(0.01, 0.02, 0.05),
              write_timeout=0.3, read_timeout=0.3, close_timeout=0.3, keepalive=0.5,
              pending_ttl=3.0, not_paired_retry=0.2, radio_poll=0.05, flap_window=0.02,
              min_retry_gap=0.0)

A1, A2 = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"


def make(timing=FAST, keep_connected=True, **transport_kw):
    devices = [SimDevice(A1, "Sim 1"), SimDevice(A2, "Sim 2")]
    transport = SimTransport(devices, seed=1, **transport_kw)
    events = []
    manager = HueManager([("L1", "Light 1", A1), ("L2", "Light 2", A2)], transport=transport,
                         timing=timing, keep_connected=keep_connected, on_change=events.append)
    return manager, transport, devices, events


async def until(predicate, timeout=3.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


class ProtocolTests(unittest.TestCase):
    def test_encode_matches_huble_layout(self):
        payload = encode_command({"power": True, "brightness": 200, "xy": (0.3, 0.15)})
        self.assertEqual(payload.hex(), "0101010201c80404cc4c6626")

    def test_encode_transition_and_effect(self):
        self.assertEqual(encode_command({"brightness": 60, "transition": 0}).hex(), "02013c05020000")
        self.assertEqual(encode_command({"effect": 0}).hex(), "060100")
        self.assertEqual(encode_command({"mireds": 370}).hex(), "03027201")
        self.assertEqual(encode_command({"brightness": 100, "effect": 0}).hex(), "020164060100")

    def test_effect_off_never_rides_with_colour(self):
        # The real lamps ignore a colour that shares a packet with "effect off".
        self.assertEqual(encode_command({"xy": (0.5, 0.4), "effect": 0}).hex(), "040400806666")
        self.assertEqual(encode_command({"mireds": 300, "effect": 0}).hex(), "03022c01")
        self.assertEqual(encode_command({"mireds": 400, "effect": 1, "effect_speed": 100}).hex(),
                         "03029001060101080164")

    def test_sim_device_mimics_lamp_quirks(self):
        dev = SimDevice(A1)
        dev.apply(bytes.fromhex("0404ff7f6666060100"))           # colour + effect off: ignored
        self.assertEqual(dev.state["xy"], (0.3, 0.15))
        dev.apply(encode_command({"effect": 1, "effect_speed": 120}))
        dev.apply(encode_command({"brightness": 80}))             # brightness keeps the effect
        self.assertEqual(dev.state["effect"], 1)
        dev.apply(encode_command({"xy": (0.2, 0.45)}))            # a colour ends it
        self.assertEqual((dev.state["effect"], round(dev.state["xy"][0], 2)), (0, 0.2))

    def test_decode_real_reports(self):
        xy = decode_report(bytes.fromhex("0101010201c80404cb4c6526"))
        self.assertTrue(xy.power)
        self.assertEqual(xy.brightness, 200)
        self.assertAlmostEqual(xy.xy[0], 0.3, places=3)
        self.assertAlmostEqual(xy.xy[1], 0.15, places=3)
        self.assertEqual(xy.mode, "xy")
        ct = decode_report(bytes.fromhex("0101010201c803027201"))
        self.assertEqual((ct.mireds, ct.xy, ct.mode), (370, None, "ct"))
        fx = decode_report(bytes.fromhex("0101010201c80404cc8c6666060101080180"))
        self.assertEqual((fx.effect, fx.effect_speed), (1, 128))

    def test_decode_garbage(self):
        self.assertEqual(decode_report(b""), LampState())
        self.assertEqual(decode_report(b"\x01"), LampState())
        self.assertIsNone(decode_report(b"\x02\x05\x01").brightness)        # truncated
        self.assertTrue(decode_report(bytes.fromhex("0902aabb010101")).power)  # unknown TLV skipped

    def test_normalise(self):
        self.assertEqual(normalise_fields({"brightness": 0})["brightness"], 1)
        self.assertEqual(normalise_fields({"brightness": 999})["brightness"], 254)
        self.assertEqual(normalise_fields({"mireds": 50})["mireds"], 153)
        self.assertEqual(normalise_fields({"power": None}), {})
        with self.assertRaises(ValueError):
            normalise_fields({"xy": (0.3, 0.3), "mireds": 300})
        with self.assertRaises(ValueError):
            normalise_fields({"colour": 1})


class ColourTests(unittest.TestCase):
    def test_rgb_xy(self):
        x, y = colors.rgb_to_xy(255, 0, 0)
        self.assertAlmostEqual(x, 0.7006, places=3)
        self.assertAlmostEqual(y, 0.2993, places=3)
        r, g, b = colors.xy_to_rgb(x, y)
        self.assertEqual(r, 255)
        self.assertLess(g, 40)

    def test_parsers(self):
        self.assertEqual(colors.parse_color("0.3,0.15"), (0.3, 0.15))
        self.assertEqual(colors.parse_color("Purple"), (0.3, 0.15))
        self.assertEqual(colors.parse_color("sky blue"), colors.parse_color("skyblue"))
        self.assertEqual(colors.parse_color("paars"), (0.3, 0.15))          # older Dutch name
        self.assertEqual(colors.parse_color("#ff0000"), colors.rgb_to_xy(255, 0, 0))
        self.assertEqual(colors.parse_white("2700K"), 370)
        self.assertEqual(colors.parse_white("370"), 370)
        self.assertEqual(colors.parse_white("warm"), 370)
        self.assertEqual(colors.parse_white("Daylight"), 200)
        with self.assertRaises(ValueError):
            colors.parse_color("no-such-color")

    def test_kelvin(self):
        self.assertEqual(colors.kelvin_to_mireds(6500), 154)
        self.assertEqual(colors.mireds_to_kelvin(500), 2000)
        r, g, b = colors.kelvin_to_rgb(2700)
        self.assertGreater(r, b)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "data" / "config.json"

    def test_cleans_up_hand_edited_lights(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"lamps": [
            {"id": "L1", "name": "Left", "address": "aa:bb:cc:dd:ee:01"},
            {"id": "L2", "name": "", "address": "AA-BB-CC-DD-EE-02"},
            {"id": "L3", "name": "duplicate", "address": "AA:BB:CC:DD:EE:01"},
            {"id": "L4", "name": "bogus", "address": "not an address"}]}))
        cfg = config.load_config(self.path)
        self.assertEqual([l["address"] for l in cfg["lamps"]], ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"])
        self.assertEqual([l["name"] for l in cfg["lamps"]], ["Left", "Light 2"])

    def test_missing_file_gives_defaults(self):
        cfg = config.load_config(self.path)
        self.assertEqual(cfg["lamps"], [])
        self.assertEqual(cfg["settings"], config.DEFAULT_SETTINGS)
        self.assertFalse(self.path.exists())                # nothing is written until something is saved

    def test_roundtrip_and_corrupt(self):
        cfg = config.load_config(self.path)
        cfg["lamps"].append({"id": "L1", "name": "Left", "address": "AA:BB:CC:DD:EE:01"})
        cfg["settings"]["idle_release_minutes"] = 5
        config.save_config(cfg, self.path)
        again = config.load_config(self.path)
        self.assertEqual(again["lamps"][0]["name"], "Left")
        self.assertEqual(again["settings"]["idle_release_minutes"], 5)
        self.path.write_text("{ broken")
        fresh = config.load_config(self.path)
        self.assertEqual(fresh["lamps"], [])
        self.assertTrue(self.path.with_suffix(".corrupt.json").exists())

    def test_scene_fields(self):
        self.assertIs(config.find_scene({"scenes": []}, "gamen"), config.find_scene({"scenes": []}, "Gaming"))
        scene = config.find_scene({"scenes": []}, "gaming")
        per_lamp = config.scene_fields(scene, ["L1", "L2", "L3"])
        self.assertEqual(per_lamp["L1"]["xy"], (0.16, 0.30))
        self.assertEqual(per_lamp["L3"]["xy"], (0.16, 0.30))     # cycles
        self.assertEqual(per_lamp["L2"]["effect"], 0)              # plain scenes stop effects
        user = {"name": "Mijn", "states": {"L2": {"power": False}}}
        self.assertEqual(config.scene_fields(user, ["L1", "L2"]), {"L2": {"power": False}})


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.errors = []
        handler = logging.Handler(level=logging.ERROR)
        handler.emit = lambda record: self.errors.append(record)
        self.handler = handler
        logging.getLogger("hue").addHandler(handler)

    async def asyncTearDown(self):
        logging.getLogger("hue").removeHandler(self.handler)

    async def test_connects_and_applies(self):
        mgr, tr, devs, _ = make()
        await mgr.start()
        try:
            self.assertTrue(await mgr.wait_connected(timeout=2))
            self.assertEqual(await mgr.apply(None, power=False, timeout=2), {"L1": True, "L2": True})
            self.assertFalse(devs[0].state["power"])
            self.assertTrue(await until(lambda: mgr.lamps["L1"].info().state.power is False))
            self.assertEqual(await mgr.apply("2", brightness=10, timeout=2), {"L2": True})
            self.assertEqual((devs[0].state["brightness"], devs[1].state["brightness"]), (200, 10))
        finally:
            await mgr.stop()
        self.assertEqual(self.errors, [])

    async def test_retries_until_connected(self):
        mgr, tr, devs, _ = make()
        tr.connect_plan[A1] += ["timeout", "error", "timeout"]
        await mgr.start()
        try:
            self.assertTrue(await mgr.wait_connected(["L1"], timeout=5))
            self.assertEqual(tr.open_count[A1], 4)
        finally:
            await mgr.stop()

    async def test_cancelled_setup_retries_fast_and_quietly(self):
        # "Operation canceled" right after a disconnect: the lamp is there, so
        # retry quickly instead of backing off, and log the streak only once.
        slow = Timing(**{**FAST.__dict__, "backoff": (5.0,), "cancelled_retry": 0.05})
        mgr, tr, devs, events = make(timing=slow)
        tr.connect_plan[A1] += ["cancelled"] * 4
        lines = []
        handler = logging.Handler(level=logging.INFO)
        handler.emit = lambda record: lines.append(record.getMessage())
        hue_log = logging.getLogger("hue")
        old_level = hue_log.level
        hue_log.setLevel(logging.DEBUG)
        hue_log.addHandler(handler)
        await mgr.start()
        try:
            started = time.monotonic()
            self.assertTrue(await mgr.wait_connected(["L1"], timeout=3))
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(any(e.id == "L1" and e.error_kind == "cancelled" for e in events))
            self.assertEqual(mgr.lamps["L1"].info().error_kind, "")
        finally:
            hue_log.removeHandler(handler)
            hue_log.setLevel(old_level)
            await mgr.stop()
        self.assertEqual(sum("connecting failed" in line for line in lines), 1)
        self.assertTrue(any("after 4 failed attempts" in line for line in lines))

    async def test_failed_setup_keeps_backoff(self):
        # A link that opens but cannot be set up is closed again; that close must
        # not wake the lamp up early and skip the pause between attempts.
        paced = Timing(**{**FAST.__dict__, "backoff": (0.4,)})
        mgr, tr, devs, events = make(timing=paced)
        tr.connect_plan[A1] += ["busy"] * 20
        await mgr.start()
        try:
            await asyncio.sleep(1.3)
            self.assertLessEqual(tr.open_count[A1], 4)
            info = mgr.lamps["L1"].info()
            self.assertEqual((info.status, info.error_kind), (LampStatus.WAITING, "busy"))
            self.assertIn("Another program", info.detail)
        finally:
            await mgr.stop()

    async def test_command_survives_outage(self):
        mgr, tr, devs, _ = make()
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            devs[0].reachable = False
            for link in tr.links(A1):
                link.drop()
            future = mgr.command("L1", brightness=42)["L1"]
            await asyncio.sleep(0.6)
            self.assertFalse(future.done())
            self.assertTrue(mgr.lamps["L1"].info().pending)
            devs[0].reachable = True
            self.assertTrue(await asyncio.wait_for(future, 3))
            self.assertEqual(devs[0].state["brightness"], 42)
        finally:
            await mgr.stop()

    async def test_command_skips_backoff(self):
        slow = Timing(**{**FAST.__dict__, "backoff": (30.0,)})
        mgr, tr, devs, _ = make(timing=slow)
        tr.connect_plan[A1] += ["error"]
        await mgr.start()
        try:
            self.assertTrue(await until(lambda: mgr.lamps["L1"].info().status is LampStatus.WAITING))
            started = time.monotonic()
            self.assertEqual(await mgr.apply("L1", power=False, timeout=3), {"L1": True})
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            await mgr.stop()

    async def test_write_failures_reconnect(self):
        for outcome in ("error", "timeout", "hang", "drop"):
            with self.subTest(outcome=outcome):
                mgr, tr, devs, _ = make()
                tr.hang_seconds = 0.6
                await mgr.start()
                try:
                    await mgr.wait_connected(timeout=2)
                    tr.write_plan[A1] += [outcome]
                    self.assertEqual(await mgr.apply("L1", brightness=33, timeout=4), {"L1": True})
                    self.assertEqual(devs[0].state["brightness"], 33)
                    self.assertGreaterEqual(tr.open_count[A1], 2)
                finally:
                    await mgr.stop()
                await asyncio.sleep(tr.hang_seconds)   # let an abandoned 'hang' run out

    async def test_hung_connect_is_abandoned(self):
        mgr, tr, devs, _ = make()
        tr.hang_seconds = 1.0
        tr.connect_plan[A1] += ["hang"]
        await mgr.start()
        try:
            started = time.monotonic()
            self.assertTrue(await mgr.wait_connected(["L1"], timeout=3))
            self.assertLess(time.monotonic() - started, 1.0)       # did not wait for the hang
            await asyncio.sleep(1.0)                                # hung attempt finishes late...
            self.assertTrue(await until(lambda: len(tr.links(A1)) == 1))  # ...and is closed again
        finally:
            await mgr.stop()

    async def test_latest_wins(self):
        mgr, tr, devs, _ = make(latency=(0.03, 0.03))
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            futures = [mgr.command("L1", brightness=v)["L1"] for v in range(1, 101)]
            results = await asyncio.wait_for(asyncio.gather(*futures), 3)
            self.assertTrue(all(results))
            self.assertEqual(devs[0].state["brightness"], 100)
            self.assertLessEqual(tr.write_count[A1], 4)
        finally:
            await mgr.stop()

    async def test_xy_replaces_mireds(self):
        mgr, tr, devs, _ = make(latency=(0.05, 0.05))
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            mgr.command("L1", brightness=5)               # occupies the link
            mgr.command("L1", mireds=300)
            last = mgr.command("L1", xy=(0.2, 0.3))["L1"]
            self.assertTrue(await asyncio.wait_for(last, 2))
            self.assertEqual((devs[0].state["mireds"], devs[0].state["xy"][0] > 0.19), (None, True))
        finally:
            await mgr.stop()

    async def test_pending_expires(self):
        short = Timing(**{**FAST.__dict__, "pending_ttl": 0.5})
        mgr, tr, devs, _ = make(timing=short)
        devs[0].reachable = False
        await mgr.start()
        try:
            future = mgr.command("L1", power=False)["L1"]
            self.assertFalse(await asyncio.wait_for(future, 3))
            self.assertFalse(mgr.lamps["L1"].info().pending)
            self.assertEqual(mgr.lamps["L1"].info().dropped, 1)
            devs[0].reachable = True
            await mgr.wait_connected(["L1"], timeout=3)
            self.assertTrue(devs[0].state["power"])     # stale command was not applied later
        finally:
            await mgr.stop()

    async def test_pending_kept_until_lamp_appears(self):
        # pending_ttl 0: set colours long before the lamp is reachable; applied when it shows up.
        short = Timing(**{**FAST.__dict__, "pending_ttl": 0.3})
        mgr, tr, devs, _ = make(timing=short)
        mgr.pending_ttl = 0
        devs[0].reachable = False
        await mgr.start()
        try:
            future = mgr.command("L1", power=True, brightness=33, xy=(0.6, 0.38))["L1"]
            await asyncio.sleep(1.0)                   # well past the timing's 0.3 s
            info = mgr.lamps["L1"].info()
            self.assertTrue(info.pending)
            self.assertFalse(future.done())
            self.assertEqual((info.pending_state.brightness, info.pending_state.xy), (33, (0.6, 0.38)))
            self.assertIsNone(info.state.power)        # never connected yet: nothing known
            self.assertEqual(info.target, info.pending_state)
            devs[0].reachable = True
            self.assertTrue(await asyncio.wait_for(future, 5))
            self.assertEqual(devs[0].state["brightness"], 33)
            info = mgr.lamps["L1"].info()
            self.assertIsNone(info.pending_state)
            self.assertEqual(info.dropped, 0)
        finally:
            await mgr.stop()

    async def test_pending_state_preview(self):
        mgr, tr, devs, _ = make()
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            await mgr.apply("L1", effect=1, effect_speed=120, timeout=2)
            self.assertTrue(await until(lambda: mgr.lamps["L1"].info().state.effect == 1))
            devs[0].reachable = False
            for link in tr.links(A1):
                link.drop()
            mgr.command("L1", mireds=300)
            preview = mgr.lamps["L1"].info().pending_state
            self.assertEqual((preview.mireds, preview.xy, preview.effect), (300, None, 0))
            self.assertEqual(preview.brightness, 200)  # untouched fields keep the reported value
        finally:
            await mgr.stop()

    async def test_toggle_uses_queued_power(self):
        mgr, tr, devs, _ = make()
        devs[0].reachable = False
        await mgr.start()
        try:
            mgr.toggle("L1")                           # unknown -> on
            self.assertTrue(mgr.lamps["L1"].info().target.power)
            mgr.toggle("L1")                           # queued on -> off
            self.assertFalse(mgr.lamps["L1"].info().target.power)
            devs[0].reachable = True
            self.assertTrue(await until(lambda: devs[0].state["power"] is False
                                        and not mgr.lamps["L1"].info().pending, timeout=4))
        finally:
            await mgr.stop()

    async def test_radio_off_then_on(self):
        mgr, tr, devs, _ = make()
        tr.radio = "off"
        await mgr.start()
        try:
            self.assertTrue(await until(lambda: mgr.lamps["L1"].info().status is LampStatus.BT_OFF))
            self.assertEqual(tr.open_count[A1], 0)
            tr.set_radio("on")
            self.assertTrue(await mgr.wait_connected(timeout=2))
        finally:
            await mgr.stop()

    async def test_no_adapter(self):
        mgr, tr, devs, _ = make()
        tr.radio = "no_adapter"
        await mgr.start()
        try:
            self.assertTrue(await until(lambda: mgr.lamps["L2"].info().status is LampStatus.NO_ADAPTER))
            tr.radio = "on"     # dongle plugged back in, no event: the poll must notice
            self.assertTrue(await mgr.wait_connected(timeout=2))
        finally:
            await mgr.stop()

    async def test_not_paired(self):
        mgr, tr, devs, _ = make()
        devs[1].paired = False
        await mgr.start()
        try:
            self.assertTrue(await until(lambda: mgr.lamps["L2"].info().status is LampStatus.NOT_PAIRED))
            self.assertTrue(await mgr.wait_connected(["L1"], timeout=2))
            devs[1].paired = True
            self.assertTrue(await mgr.wait_connected(["L2"], timeout=3))
        finally:
            await mgr.stop()

    async def test_rejected_write_is_dropped(self):
        mgr, tr, devs, _ = make()
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            tr.write_plan[A1] += ["reject"]
            self.assertEqual(await mgr.apply("L1", effect=99, timeout=2), {"L1": False})
            self.assertEqual(tr.open_count[A1], 1)                  # link kept
            self.assertEqual(await mgr.apply("L1", power=False, timeout=2), {"L1": True})
        finally:
            await mgr.stop()
        self.errors.clear()   # the rejection is logged as an error on purpose

    async def test_gatt_error_uses_fresh_gatt(self):
        mgr, tr, devs, _ = make()
        tr.connect_plan[A1] += ["gatt"]
        await mgr.start()
        try:
            await mgr.wait_connected(["L1"], timeout=2)
            self.assertEqual(tr.fresh_gatt_opens[A1], 1)
        finally:
            await mgr.stop()

    async def test_external_change_is_reported(self):
        mgr, tr, devs, events = make()
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            devs[0].external_change(brightness=10, mireds=400)
            self.assertTrue(await until(lambda: mgr.lamps["L1"].info().state.mireds == 400))
            self.assertTrue(any(e.id == "L1" and e.state.brightness == 10 for e in events))
        finally:
            await mgr.stop()

    async def test_release_then_command_reconnects(self):
        mgr, tr, devs, _ = make()
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            mgr.set_connected(None, False)
            self.assertTrue(await until(lambda: all(i.status is LampStatus.RELEASED for i in mgr.snapshot())))
            self.assertEqual(tr.links(A1), [])
            self.assertEqual(await mgr.apply("L1", power=False, timeout=2), {"L1": True})
            self.assertTrue(mgr.lamps["L1"].info().connected)
            self.assertIs(mgr.lamps["L2"].info().status, LampStatus.RELEASED)
        finally:
            await mgr.stop()

    async def test_idle_release(self):
        mgr, tr, devs, _ = make()
        mgr.idle_release = 0.2
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            self.assertTrue(await until(lambda: all(i.status is LampStatus.RELEASED for i in mgr.snapshot()),
                                        timeout=3))
            self.assertEqual(await mgr.apply(None, brightness=99, timeout=2), {"L1": True, "L2": True})
        finally:
            await mgr.stop()

    async def test_identify_restores(self):
        mgr, tr, devs, _ = make()
        await mgr.start()
        try:
            await mgr.wait_connected(timeout=2)
            mgr.identify("L1")
            self.assertTrue(await until(lambda: len(devs[0].payloads) >= 5, timeout=4))
            self.assertEqual(devs[0].state["brightness"], 200)
            self.assertTrue(devs[0].state["power"])
            self.assertIn(15, [decode_report(p).brightness for p in devs[0].payloads])
        finally:
            await mgr.stop()

    async def test_flapping_link_backs_off(self):
        flappy = Timing(**{**FAST.__dict__, "flap_window": 1.0})
        mgr, tr, devs, events = make(timing=flappy)
        await mgr.start()
        try:
            await mgr.wait_connected(["L1"], timeout=2)
            for link in tr.links(A1):
                link.drop()
            self.assertTrue(await until(lambda: any(e.id == "L1" and e.status is LampStatus.CONNECTING
                                                    and e.attempt == 2 for e in events)))
            self.assertTrue(await mgr.wait_connected(["L1"], timeout=2))
        finally:
            await mgr.stop()

    async def test_stop_is_fast_even_when_hung(self):
        mgr, tr, devs, _ = make()
        tr.hang_seconds = 1.5
        tr.connect_plan[A1] += ["hang"] * 5
        await mgr.start()
        await asyncio.sleep(0.1)
        started = time.monotonic()
        await mgr.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(all(i.status is LampStatus.STOPPED for i in mgr.snapshot()))
        await asyncio.sleep(1.6)    # let the abandoned hang finish before the loop closes

    async def test_random_failure_stress(self):
        """Hundreds of commands under heavy random failures still end in the last state."""
        mgr, tr, devs, _ = make(connect_fail_rate=0.3, write_fail_rate=0.15, drop_rate=0.03,
                                latency=(0.001, 0.01))
        tr.hang_seconds = 0.4
        rng = random.Random(7)
        await mgr.start()
        try:
            for _ in range(300):
                target = rng.choice(["L1", "L2", None])
                fields = rng.choice([{"power": rng.random() > 0.3}, {"brightness": rng.randint(1, 254)},
                                     {"xy": (rng.random() * 0.6, rng.random() * 0.6)},
                                     {"mireds": rng.randint(153, 500)}])
                mgr.command(target, **fields)
                await asyncio.sleep(rng.random() * 0.01)
            final = {"power": True, "brightness": 77, "xy": (0.2, 0.3), "effect": 0}
            results = await mgr.apply(None, timeout=15, **final)
            self.assertEqual(results, {"L1": True, "L2": True})
            for dev in devs:
                self.assertTrue(dev.state["power"])
                self.assertEqual(dev.state["brightness"], 77)
                self.assertAlmostEqual(dev.state["xy"][0], 0.2, places=3)
                self.assertIsNone(dev.state["mireds"])
        finally:
            await mgr.stop()
        self.assertEqual([r.getMessage() for r in self.errors], [])


class IpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip(self):
        mgr, tr, devs, _ = make()
        await mgr.start()
        shown = []
        server = IpcServer(mgr, on_show=lambda: shown.append(True), port=0)
        self.assertTrue(await server.start())
        port = server._server.sockets[0].getsockname()[1]
        try:
            await mgr.wait_connected(timeout=2)
            ask = lambda req: asyncio.to_thread(ipc_request, req, port)  # noqa: E731
            self.assertEqual((await ask({"op": "ping"}))["app"], "hue-controller")
            reply = await ask({"op": "set", "targets": ["L2"], "fields": {"xy": [0.5, 0.4], "brightness": 50}})
            self.assertEqual(reply, {"ok": True, "results": {"L2": True}, "states": {"L2": "ok"}})
            self.assertEqual(devs[1].state["brightness"], 50)
            # An unreachable lamp: the command is accepted and stays queued.
            devs[1].reachable = False
            for link in tr.links(A2):
                link.drop()
            reply = await ask({"op": "set_many", "timeout": 0.3, "commands": [
                {"targets": None, "fields": {"brightness": 90}}]})
            self.assertEqual(reply["states"], {"L1": "ok", "L2": "pending"})
            self.assertFalse(reply["ok"])
            devs[1].reachable = True
            self.assertTrue(await until(lambda: devs[1].state["brightness"] == 90, timeout=4))
            self.assertTrue(await until(lambda: mgr.lamps["L2"].info().connected, timeout=4))
            reply = await ask({"op": "toggle"})
            self.assertTrue(reply["ok"])
            self.assertFalse(devs[0].state["power"] or devs[1].state["power"])
            status = await ask({"op": "status"})
            self.assertEqual([l["status"] for l in status["lamps"]], ["connected", "connected"])
            await ask({"op": "show"})
            self.assertEqual(shown, [True])
            self.assertFalse((await ask({"op": "nonsense"}))["ok"])
        finally:
            await server.close()
            await mgr.stop()
        self.assertIsNone(await asyncio.to_thread(ipc_request, {"op": "ping"}, port))


class QuitTests(unittest.TestCase):
    """``Huetooth.exe --quit`` (used by the installer) must only return
    once the running app's process is really gone, so its files are free."""

    def test_quit_waits_for_the_process_to_exit(self):
        import subprocess
        import textwrap

        from hue_engine import controller_running, quit_controller

        port = 47838
        self.assertTrue(quit_controller(port, timeout=1))     # nothing running: fine
        script = textwrap.dedent(f"""
            import asyncio, sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
            from hue_engine import HueManager, IpcServer, SimTransport

            async def main():
                done = asyncio.Event()
                server = IpcServer(HueManager([], transport=SimTransport([])), on_quit=done.set, port={port})
                await server.start()
                print("ready", flush=True)
                await done.wait()
                await server.close()
                time.sleep(0.8)          # shutting down takes a while (Bluetooth links closing)

            asyncio.run(main())
        """)
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            self.assertTrue(controller_running(port))
            started = time.monotonic()
            self.assertTrue(quit_controller(port, timeout=10))
            self.assertIsNotNone(proc.poll(), "returned while the app was still running")
            self.assertGreater(time.monotonic() - started, 0.6)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait(5)


class EngineThreadTests(unittest.TestCase):
    def test_thread_lifecycle(self):
        transport = SimTransport([SimDevice(A1)], seed=3)
        events = []
        engine = EngineThread(lambda: HueManager([("L1", "Light 1", A1)], transport=transport,
                                                 timing=FAST, on_change=events.append))
        engine.start()
        try:
            ok = engine.call(lambda: engine.manager.wait_connected(timeout=2)).result(3)
            self.assertTrue(ok)
            result = engine.call(lambda: engine.manager.apply("L1", power=False, timeout=2)).result(3)
            self.assertEqual(result, {"L1": True})
            self.assertNotEqual(threading.current_thread().name, "hue-engine")
        finally:
            engine.stop()
        self.assertFalse(engine._thread.is_alive())
        self.assertIs(events[-1].status, LampStatus.STOPPED)


if __name__ == "__main__":
    unittest.main()
