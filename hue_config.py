"""Settings file: lights, settings and scenes.

Stored per user in %APPDATA%\\Huetooth\\config.json (logs next to it),
so the program folder stays clean and may be read-only.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "Huetooth"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"

ADDRESS_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "idle_release_minutes": 0,   # 0 = keep the lights connected while the app is open
    "transition": 4,             # default fade for buttons, in steps of 100 ms
    "pending_keep_minutes": 0,   # how long a choice waits for an unreachable light; 0 = until it connects
    "close_to_tray": True,       # the window's close button keeps Huetooth running in the tray
}

# Scenes: "states" is a list (applied by light position, repeating) or a dict
# keyed by light id (user scenes).
BUILTIN_SCENES: list[dict[str, Any]] = [
    {"name": "Purple",
     "states": [{"power": True, "brightness": 200, "xy": [0.30, 0.15]}]},
    {"name": "Focus",
     "states": [{"power": True, "brightness": 254, "mireds": 233}]},
    {"name": "Evening",
     "states": [{"power": True, "brightness": 150, "mireds": 370}]},
    {"name": "Movie",
     "states": [{"power": True, "brightness": 45, "mireds": 455}]},
    {"name": "Gaming",
     "states": [{"power": True, "brightness": 230, "xy": [0.16, 0.30]},
                {"power": True, "brightness": 230, "xy": [0.39, 0.17]}]},
    {"name": "Sunset",
     "states": [{"power": True, "brightness": 190, "xy": [0.59, 0.385]},
                {"power": True, "brightness": 190, "xy": [0.45, 0.25]}]},
    {"name": "Ocean",
     "states": [{"power": True, "brightness": 210, "xy": [0.155, 0.06]},
                {"power": True, "brightness": 210, "xy": [0.16, 0.30]}]},
    {"name": "Candles",
     "states": [{"power": True, "brightness": 140, "xy": [0.56, 0.40], "effect": 1, "effect_speed": 110}]},
]

DEFAULT_SCENE = "Purple"
# Dutch scene names still work on the command line.
SCENE_ALIASES = {"paars": "purple", "avond": "evening", "film": "movie", "gamen": "gaming",
                 "zonsondergang": "sunset", "oceaan": "ocean", "kaarsjes": "candles"}


def _new_config() -> dict[str, Any]:
    return {"lamps": [], "settings": dict(DEFAULT_SETTINGS), "scenes": [], "ui": {}}


def normalise_address(address: str) -> str | None:
    text = str(address).strip().upper().replace("-", ":")
    return text if ADDRESS_RE.match(text) else None


def next_lamp_id(lamps: list[dict[str, Any]]) -> str:
    used = {lamp["id"] for lamp in lamps}
    n = 1
    while f"L{n}" in used:
        n += 1
    return f"L{n}"


def _clean_lamps(raw: Any) -> list[dict[str, Any]]:
    lamps: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return lamps
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        address = normalise_address(entry.get("address", ""))
        if not address or any(l["address"] == address for l in lamps):
            continue
        lamp_id = str(entry.get("id") or "").strip()
        if not lamp_id or any(l["id"] == lamp_id for l in lamps):
            lamp_id = next_lamp_id(lamps)
        name = str(entry.get("name") or "").strip() or f"Light {len(lamps) + 1}"
        lamps.append({"id": lamp_id, "name": name, "address": address})
    return lamps


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = _new_config()
    data: Any = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Keep the broken file for inspection and carry on with defaults.
            try:
                os.replace(path, path.with_suffix(".corrupt.json"))
            except OSError:
                pass
    if isinstance(data, dict):
        cfg["lamps"] = _clean_lamps(data.get("lamps"))
        settings = data.get("settings")
        if isinstance(settings, dict):
            for key, default in DEFAULT_SETTINGS.items():
                value = settings.get(key, default)
                cfg["settings"][key] = value if isinstance(value, type(default)) else default
        scenes = data.get("scenes")
        if isinstance(scenes, list):
            cfg["scenes"] = [s for s in scenes if isinstance(s, dict) and s.get("name")
                             and isinstance(s.get("states"), (list, dict))]
        if isinstance(data.get("ui"), dict):
            cfg["ui"] = data["ui"]
    return cfg


def save_config(cfg: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    """Atomic write: a crash halfway never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def lamp_specs(cfg: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(lamp["id"], lamp["name"], lamp["address"]) for lamp in cfg["lamps"]]


def all_scenes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return BUILTIN_SCENES + list(cfg.get("scenes", []))


def find_scene(cfg: dict[str, Any], name: str) -> dict[str, Any] | None:
    wanted = name.strip().lower()
    wanted = SCENE_ALIASES.get(wanted, wanted)
    for scene in reversed(all_scenes(cfg)):   # user scenes win over built-ins
        if scene["name"].lower() == wanted:
            return scene
    return None


def scene_fields(scene: dict[str, Any], lamp_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Per-light command fields for a scene. Plain scenes also stop effects."""
    states = scene["states"]
    result: dict[str, dict[str, Any]] = {}
    for index, lamp_id in enumerate(lamp_ids):
        if isinstance(states, dict):
            state = states.get(lamp_id)
            if state is None:
                continue
        else:
            if not states:
                continue
            state = states[index % len(states)]
        fields = {k: v for k, v in state.items()
                  if k in ("power", "brightness", "xy", "mireds", "effect", "effect_speed")}
        if isinstance(fields.get("xy"), list):
            fields["xy"] = tuple(fields["xy"])
        if fields.get("power", True) and "effect" not in fields:
            fields["effect"] = 0
        result[lamp_id] = fields
    return result
