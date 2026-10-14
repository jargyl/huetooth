"""Hardware test against the real lamps (changes their colour; restores at the end).

Usage:  py tests/hw_test.py [connect N] [commands N] [recover N] [effects] [all]
"""

import asyncio
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hue_config as config  # noqa: E402
from hue_engine import EFFECTS, HueManager, LampStatus, setup_logging  # noqa: E402

GAMUT = ((0.6915, 0.3083), (0.17, 0.7), (0.1532, 0.0475))


def random_xy(rng):
    """Uniform point well inside gamut C so the lamp does not clamp it."""
    a, b = sorted((rng.random(), rng.random()))
    w = (a, b - a, 1 - b)
    centre = (sum(p[0] for p in GAMUT) / 3, sum(p[1] for p in GAMUT) / 3)
    x = sum(wi * p[0] for wi, p in zip(w, GAMUT))
    y = sum(wi * p[1] for wi, p in zip(w, GAMUT))
    return (round(centre[0] + (x - centre[0]) * 0.85, 4), round(centre[1] + (y - centre[1]) * 0.85, 4))


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def matches(state, fields):
    if "power" in fields and state.power != fields["power"]:
        return False
    if "brightness" in fields and state.brightness != fields["brightness"]:
        return False
    if "mireds" in fields and state.mireds != fields["mireds"]:
        return False
    if "xy" in fields:
        if state.xy is None or abs(state.xy[0] - fields["xy"][0]) > 0.01 or abs(state.xy[1] - fields["xy"][1]) > 0.01:
            return False
    return True


async def wait_until(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def new_manager(cfg, **kw):
    return HueManager(config.lamp_specs(cfg), **kw)


async def test_connect(cfg, rounds, gap=45):
    # A long gap mimics opening the app; reconnecting within ~20 s of closing is
    # slower (Windows is still tearing the old links down) - see Timing.cancelled_retry.
    print(f"\n== Cold connects: {rounds} rounds (both lights in parallel, {gap} s apart) ==")
    times, failures = [], 0
    for r in range(1, rounds + 1):
        mgr = new_manager(cfg)
        started = time.monotonic()
        await mgr.start()
        ok = await mgr.wait_connected(timeout=30)
        elapsed = time.monotonic() - started
        attempts = {i.name: i.attempt for i in mgr.snapshot()}
        await mgr.stop()
        if ok:
            times.append(elapsed)
        else:
            failures += 1
        print(f"  round {r}: {'OK' if ok else 'FAILED'} in {elapsed:.2f}s")
        await asyncio.sleep(gap)
    if times:
        print(f"  => {len(times)}/{rounds} succeeded; median {statistics.median(times):.2f}s, "
              f"max {max(times):.2f}s")
    return failures == 0


async def test_commands(cfg, count):
    print(f"\n== {count} random commands on an open connection ==")
    rng = random.Random(42)
    mgr = new_manager(cfg)
    await mgr.start()
    ok_all = True
    try:
        t0 = time.monotonic()
        if not await mgr.wait_connected(timeout=30):
            print("  could not connect")
            return False
        print(f"  connected in {time.monotonic() - t0:.2f}s")
        ack_ms, settle_ms, failed = [], [], 0
        ids = list(mgr.lamps)
        for n in range(count):
            target = rng.choice(ids + [None])
            kind = rng.random()
            if kind < 0.15:
                fields = {"power": rng.random() < 0.5}
            elif kind < 0.5:
                fields = {"power": True, "brightness": rng.randint(1, 254)}
            elif kind < 0.8:
                fields = {"power": True, "xy": random_xy(rng), "effect": 0}
            else:
                fields = {"power": True, "mireds": rng.randint(153, 500), "effect": 0}
            fields["transition"] = 0
            start = time.monotonic()
            results = await mgr.apply(target, timeout=15, **fields)
            ack = time.monotonic()
            lamps = mgr.resolve(target)
            converged = await wait_until(lambda: all(matches(l.info().state, fields) for l in lamps), 5)
            done = time.monotonic()
            if not all(results.values()) or not converged:
                failed += 1
                ok_all = False
                states = {l.name: l.info().state for l in lamps}
                print(f"  #{n}: PROBLEM results={results} converged={converged} fields={fields} states={states}")
            else:
                ack_ms.append((ack - start) * 1000)
                settle_ms.append((done - start) * 1000)
        print(f"  => {count - failed}/{count} applied and confirmed by the light")
        if ack_ms:
            print(f"  write: median {statistics.median(ack_ms):.0f} ms, p90 {pct(ack_ms, 90):.0f} ms, "
                  f"max {max(ack_ms):.0f} ms")
            print(f"  light confirms state: median {statistics.median(settle_ms):.0f} ms, "
                  f"p90 {pct(settle_ms, 90):.0f} ms")
        reconnects = [i for i in mgr.snapshot() if i.status is not LampStatus.CONNECTED]
        if reconnects:
            print("  note, not connected at the end:", reconnects)
    finally:
        await mgr.stop()
    return ok_all


async def test_recover(cfg, rounds):
    print(f"\n== Recovery after a dropped connection: {rounds} times ==")
    mgr = new_manager(cfg)
    await mgr.start()
    ok_all = True
    try:
        await mgr.wait_connected(timeout=30)
        times = []
        for r in range(rounds):
            lamp = list(mgr.lamps.values())[r % len(mgr.lamps)]
            link = lamp._link
            started = time.monotonic()
            # Close the link underneath the supervisor, as if the lamp dropped it.
            await link.close()
            await wait_until(lambda: not lamp.info().connected, 5)
            back = await wait_until(lambda: lamp.info().connected, 30)
            brightness = 60 + r * 20
            result = await mgr.apply(lamp.id, timeout=10, brightness=brightness, transition=0)
            elapsed = time.monotonic() - started
            print(f"  {lamp.name}: {'back' if back else 'NOT back'} + command {result[lamp.id]} "
                  f"in {elapsed:.2f}s")
            ok_all &= back and result[lamp.id]
            times.append(elapsed)
            await asyncio.sleep(1)
        print(f"  => median recovery+command {statistics.median(times):.2f}s")
    finally:
        await mgr.stop()
    return ok_all


async def test_effects(cfg):
    print("\n== Effects ==")
    mgr = new_manager(cfg)
    await mgr.start()
    ok_all = True
    try:
        await mgr.wait_connected(timeout=30)
        res = await mgr.apply(None, timeout=10, effect=0)
        print(f"  effect off while no effect runs: {res}")
        ok_all &= all(res.values())
        for eid, name, _alias in EFFECTS:
            res = await mgr.apply(None, timeout=10, power=True, effect=eid, effect_speed=128)
            await asyncio.sleep(1.2)
            reported = {l.name: l.info().state.effect for l in mgr.lamps.values()}
            print(f"  {name:12s} ({eid:2d}): written={all(res.values())} light reports effect={reported}")
            ok_all &= all(res.values())
        res = await mgr.apply(None, timeout=10, effect=0)
        await asyncio.sleep(1.0)
        print(f"  stop effect: {res}; light reports {[l.info().state.effect for l in mgr.lamps.values()]}")
        # effect without colour vs. colour + effect
        res = await mgr.apply(None, timeout=10, power=True, mireds=400, effect=1, effect_speed=100)
        await asyncio.sleep(1.0)
        print(f"  candle on warm white: {res}; {[l.info().state for l in mgr.lamps.values()]}")
        await mgr.apply(None, timeout=10, effect=0)
    finally:
        await mgr.stop()
    return ok_all


async def restore(cfg):
    mgr = new_manager(cfg)
    await mgr.start()
    try:
        scene = config.find_scene(cfg, config.DEFAULT_SCENE)
        per_lamp = config.scene_fields(scene, list(mgr.lamps))
        results = await asyncio.gather(*(mgr.apply(lid, timeout=20, **f) for lid, f in per_lamp.items()))
        print(f"\nRestored to '{scene['name']}': {results}")
    finally:
        await mgr.stop()


async def main(argv):
    setup_logging(config.LOG_DIR / "hw_test.log")
    cfg = config.load_config()
    if not cfg["lamps"]:
        print("No lights set up yet - add them in the Monitor Lights app first")
        return 2
    args = argv or ["all"]
    plan = []
    i = 0
    while i < len(args):
        name = args[i]
        number = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else None
        i += 2 if number is not None else 1
        if name == "all":
            plan += [("connect", 3), ("commands", 150), ("recover", 4), ("effects", None)]
        else:
            plan.append((name, number))
    ok = True
    try:
        for name, number in plan:
            if name == "connect":
                ok &= await test_connect(cfg, number or 5)
            elif name == "commands":
                ok &= await test_commands(cfg, number or 100)
            elif name == "recover":
                ok &= await test_recover(cfg, number or 4)
            elif name == "effects":
                ok &= await test_effects(cfg)
    finally:
        await restore(cfg)
    print("\nRESULT:", "ALL OK" if ok else "THERE WERE PROBLEMS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
