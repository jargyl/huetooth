"""Schedules: "at 22:00 every day, make the lights red".

A schedule is applied when its time comes while Huetooth runs (also hidden in
the tray). If Huetooth wasn't running then, it catches up when it starts, as
long as that is before the schedule's 'until' time. Every occurrence is applied
once, so changing the lights afterwards is never undone.

A schedule (stored in the settings file):
    {"id": "S1", "enabled": true, "time": "22:00", "until": "04:00" or null,
     "days": [0..6] (Monday = 0), "lights": "all" or [light ids],
     "action": {"type": "scene", "scene": "Sunset"}
             | {"type": "color", "xy": [x, y], "brightness": 1..254}
             | {"type": "white", "mireds": 153..500, "brightness": 1..254}
             | {"type": "off"},
     "last_run": "2026-10-14T22:00" or null}
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STAMP = "%Y-%m-%dT%H:%M"
# Without an 'until' time a schedule only fires at its time, with a small margin
# for a PC that was busy or just woke up.
EXACT_WINDOW = timedelta(minutes=2)


def parse_time(text: str) -> str | None:
    """'22', '22:00', '2200', '22.00', '22h', '7:30' -> 'HH:MM' (None if it isn't a time)."""
    raw = str(text).strip().lower().replace(".", ":").replace("h", ":").replace("u", ":")
    match = re.fullmatch(r"(\d{1,2}):?(\d{2})?:?", raw)
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2) or 0)
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _at(day: datetime, hhmm: str) -> datetime:
    hours, minutes = hhmm.split(":")
    return day.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)


def _days(rule: dict[str, Any]) -> set[int]:
    return set(rule.get("days") or range(7))


def window(rule: dict[str, Any]) -> timedelta:
    """How long after its time a schedule may still be applied (catch-up)."""
    if not rule.get("until"):
        return EXACT_WINDOW
    span = (_minutes(rule["until"]) - _minutes(rule["time"])) % (24 * 60)
    return timedelta(minutes=span or 24 * 60)


def last_occurrence(rule: dict[str, Any], now: datetime) -> datetime | None:
    """The latest moment (not after now) the schedule was due, on one of its days."""
    for back in range(8):
        day = now - timedelta(days=back)
        moment = _at(day, rule["time"])
        if moment <= now and moment.weekday() in _days(rule):
            return moment
    return None


def next_occurrence(rule: dict[str, Any], now: datetime) -> datetime | None:
    for ahead in range(8):
        day = now + timedelta(days=ahead)
        moment = _at(day, rule["time"])
        if moment > now and moment.weekday() in _days(rule):
            return moment
    return None


def due(rule: dict[str, Any], now: datetime) -> datetime | None:
    """The occurrence to apply right now, or None (not due, too late, or done already)."""
    if not rule.get("enabled", True):
        return None
    moment = last_occurrence(rule, now)
    if moment is None or now - moment > window(rule):
        return None
    if (rule.get("last_run") or "") >= moment.strftime(STAMP):
        return None
    return moment


def seconds_until_next(rules: list[dict[str, Any]], now: datetime) -> float | None:
    moments = [m for m in (next_occurrence(r, now) for r in rules if r.get("enabled", True)) if m]
    return (min(moments) - now).total_seconds() if moments else None


def action_fields(action: dict[str, Any]) -> dict[str, Any] | None:
    """Command fields for a colour, white or off action (scenes are looked up by the app)."""
    kind = action.get("type")
    if kind == "off":
        return {"power": False}
    if kind == "color":
        return {"power": True, "xy": tuple(action["xy"]), "brightness": action.get("brightness", 200), "effect": 0}
    if kind == "white":
        return {"power": True, "mireds": action["mireds"], "brightness": action.get("brightness", 200), "effect": 0}
    return None


def describe_days(days: list[int] | None) -> str:
    chosen = sorted(set(days or range(7)))
    if len(chosen) == 7:
        return "Every day"
    if chosen == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if chosen == [5, 6]:
        return "Weekends"
    return ", ".join(DAYS[d] for d in chosen)


def new_id(rules: list[dict[str, Any]]) -> str:
    used = {r.get("id") for r in rules}
    n = 1
    while f"S{n}" in used:
        n += 1
    return f"S{n}"


def clean(raw: Any) -> list[dict[str, Any]]:
    """Schedules from the settings file; anything malformed is dropped."""
    rules: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return rules
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        time = parse_time(entry.get("time", ""))
        action = entry.get("action")
        if not time or not isinstance(action, dict):
            continue
        kind = action.get("type")
        try:
            if kind == "scene" and isinstance(action.get("scene"), str) and action["scene"]:
                action = {"type": "scene", "scene": action["scene"]}
            elif kind == "color":
                x, y = (float(v) for v in action["xy"])
                action = {"type": "color", "xy": [x, y], "brightness": _bri(action)}
            elif kind == "white":
                action = {"type": "white", "mireds": max(153, min(500, int(action["mireds"]))),
                          "brightness": _bri(action)}
            elif kind == "off":
                action = {"type": "off"}
            else:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        days = entry.get("days")
        days = sorted({d for d in days if isinstance(d, int) and 0 <= d <= 6}) if isinstance(days, list) else []
        lights = entry.get("lights", "all")
        if not (lights == "all" or (isinstance(lights, list) and all(isinstance(l, str) for l in lights))):
            lights = "all"
        last_run = entry.get("last_run")
        rules.append({
            "id": entry.get("id") if isinstance(entry.get("id"), str) else new_id(rules),
            "enabled": bool(entry.get("enabled", True)),
            "time": time,
            "until": parse_time(entry["until"]) if entry.get("until") else None,
            "days": days or list(range(7)),
            "lights": lights,
            "action": action,
            "last_run": last_run if isinstance(last_run, str) else None,
        })
    return rules


def _bri(action: dict[str, Any]) -> int:
    return max(1, min(254, int(action.get("brightness", 200))))
