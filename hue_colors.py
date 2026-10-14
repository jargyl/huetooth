"""Colour helpers for Hue lamps: sRGB <-> CIE xy, colour temperature and presets."""

from __future__ import annotations

import colorsys
import math
import re

# Gamut C corners (current Hue colour lamps). The lamp clamps anything outside
# by itself; we only use these to pick vivid presets.
GAMUT_C = ((0.6915, 0.3083), (0.17, 0.7), (0.1532, 0.0475))

KELVIN_MIN, KELVIN_MAX = 2000, 6500  # 500 .. 153 mireds


def _expand(c: float) -> float:
    return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92


def _compress(c: float) -> float:
    return 1.055 * c ** (1 / 2.4) - 0.055 if c > 0.0031308 else 12.92 * c


def rgb_to_xy(r: int, g: int, b: int) -> tuple[float, float]:
    """sRGB 0..255 -> CIE xy (wide-gamut D65 matrix recommended by Philips)."""
    r, g, b = (_expand(v / 255) for v in (r, g, b))
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    if total <= 0:
        return (0.3227, 0.329)
    return (round(x / total, 4), round(y / total, 4))


def xy_to_rgb(x: float, y: float) -> tuple[int, int, int]:
    """CIE xy -> displayable sRGB at full value (brightest channel = 255)."""
    y = max(y, 1e-4)
    big_y = 1.0
    big_x = big_y / y * x
    big_z = big_y / y * (1 - x - y)
    r = big_x * 1.656492 - big_y * 0.354851 - big_z * 0.255038
    g = -big_x * 0.707196 + big_y * 1.655397 + big_z * 0.036152
    b = big_x * 0.051713 - big_y * 0.121364 + big_z * 1.011530
    r, g, b = (max(0.0, v) for v in (r, g, b))
    top = max(r, g, b) or 1.0
    return tuple(int(round(_compress(v / top) * 255)) for v in (r, g, b))  # type: ignore[return-value]


def kelvin_to_mireds(kelvin: float) -> int:
    return int(round(1_000_000 / max(1000.0, kelvin)))


def mireds_to_kelvin(mireds: float) -> int:
    return int(round(1_000_000 / max(1.0, mireds)))


def kelvin_to_rgb(kelvin: float) -> tuple[int, int, int]:
    """Approximate colour of a black body (Tanner Helland), for display."""
    t = max(1000.0, min(40000.0, kelvin)) / 100
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0
    return tuple(int(max(0, min(255, v))) for v in (r, g, b))  # type: ignore[return-value]


def mireds_to_rgb(mireds: float) -> tuple[int, int, int]:
    return kelvin_to_rgb(mireds_to_kelvin(mireds))


def hs_to_rgb(hue: float, saturation: float) -> tuple[int, int, int]:
    """Hue 0..1, saturation 0..1 at full value -> sRGB 0..255."""
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, max(0.0, min(1.0, saturation)), 1.0)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def rgb_to_hs(r: int, g: int, b: int) -> tuple[float, float]:
    h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return h, s


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def from_hex(text: str) -> tuple[int, int, int]:
    text = text.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        raise ValueError(f"not a valid hex color: {text!r}")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def mix(c1: str, c2: str, amount: float) -> str:
    """Blend two hex colours; amount 0 -> c1, 1 -> c2."""
    a, b = from_hex(c1), from_hex(c2)
    amount = max(0.0, min(1.0, amount))
    return to_hex(tuple(int(round(x + (y - x) * amount)) for x, y in zip(a, b)))  # type: ignore[arg-type]


def state_to_rgb(state) -> tuple[int, int, int]:
    """Display colour (full value) for a LampState or state dict."""
    get = state.get if isinstance(state, dict) else lambda k, d=None: getattr(state, k, d)
    if get("mireds") is not None:
        return mireds_to_rgb(get("mireds"))
    if get("xy") is not None:
        x, y = get("xy")
        return xy_to_rgb(x, y)
    return (255, 214, 170)


# Vivid colours, defined directly in xy so they sit near the edge of gamut C.
COLOR_PRESETS: list[tuple[str, tuple[float, float]]] = [
    ("Red", (0.6750, 0.3150)),
    ("Orange", (0.5900, 0.3850)),
    ("Gold", (0.5150, 0.4350)),
    ("Yellow", (0.4500, 0.4900)),
    ("Green", (0.2100, 0.6600)),
    ("Turquoise", (0.1600, 0.3000)),
    ("Sky blue", (0.1580, 0.1700)),
    ("Blue", (0.1550, 0.0600)),
    ("Violet", (0.2400, 0.0950)),
    ("Purple", (0.3000, 0.1500)),
    ("Magenta", (0.3900, 0.1700)),
    ("Pink", (0.4500, 0.2500)),
]

WHITE_PRESETS: list[tuple[str, int]] = [  # (label, kelvin)
    ("Candlelight", 2200),
    ("Warm", 2700),
    ("Soft", 3200),
    ("Neutral", 4000),
    ("Daylight", 5000),
    ("Cool", 6500),
]

COLOR_NAMES = {name.lower().replace(" ", ""): xy for name, xy in COLOR_PRESETS}
COLOR_NAMES.update({"cyan": COLOR_NAMES["turquoise"], "lightblue": COLOR_NAMES["skyblue"]})
# Dutch names still work (older shortcuts and scripts use them).
COLOR_NAMES.update({dutch: COLOR_NAMES[english] for dutch, english in {
    "rood": "red", "oranje": "orange", "goud": "gold", "geel": "yellow", "groen": "green",
    "turkoois": "turquoise", "hemelsblauw": "skyblue", "blauw": "blue", "paars": "purple", "roze": "pink",
}.items()})
WHITE_NAMES = {label.lower(): kelvin for label, kelvin in WHITE_PRESETS}
WHITE_NAMES.update({"kaarslicht": 2200, "zacht": 3200, "neutraal": 4000, "daglicht": 5000, "koel": 6500})


def parse_color(text: str) -> tuple[float, float]:
    """'red', 'sky blue', '#ff8800', 'ff8800' or '0.3,0.15' -> xy."""
    raw = text.strip().lower()
    if raw.replace(" ", "") in COLOR_NAMES:
        return COLOR_NAMES[raw.replace(" ", "")]
    match = re.fullmatch(r"\(?\s*([01]?\.\d+|[01])\s*[,; ]\s*([01]?\.\d+|[01])\s*\)?", raw)
    if match:
        return (float(match.group(1)), float(match.group(2)))
    return rgb_to_xy(*from_hex(raw))


def parse_white(text: str) -> int:
    """'2700k', '2700', '370' (mireds) or 'warm' -> mireds."""
    raw = text.strip().lower().replace(" ", "")
    if raw in WHITE_NAMES:
        return kelvin_to_mireds(WHITE_NAMES[raw])
    if raw.endswith("k"):
        return kelvin_to_mireds(float(raw[:-1]))
    value = float(raw)
    return kelvin_to_mireds(value) if value >= 1000 else int(round(value))
