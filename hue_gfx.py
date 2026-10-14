"""Smooth (anti-aliased) shapes for the Tkinter UI.

Tk draws canvas ovals and rounded polygons without anti-aliasing, which looks
jagged. These helpers draw the shapes with Pillow at SS x the size, shrink the
shape mask with a high-quality filter and colour it afterwards (so edges never
get dark fringes), and hand the result to Tk as a transparent PhotoImage.
Every image is cached, so redrawing a widget costs almost nothing.
"""

from __future__ import annotations

import colorsys
import math
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

SS = 4                                   # supersampling factor
# Next to this file, or inside the unpacked bundle of the Windows .exe (PyInstaller).
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"


def _rgb(colour: str) -> tuple[int, int, int]:
    colour = colour.lstrip("#")
    return int(colour[0:2], 16), int(colour[2:4], 16), int(colour[4:6], 16)


def _paint(colour: str, mask: Image.Image, alpha: float = 1.0) -> Image.Image:
    """Solid colour everywhere, shape only through the alpha channel."""
    layer = Image.new("RGBA", mask.size, _rgb(colour) + (0,))
    if alpha < 1.0:
        mask = mask.point(lambda v: int(v * alpha))
    layer.putalpha(mask)
    return layer


def _shrink(mask: Image.Image, size: tuple[int, int]) -> Image.Image:
    return mask.resize(size, Image.LANCZOS)


@lru_cache(maxsize=2048)
def rounded_mask(w: int, h: int, r: float, inset: float = 0.0) -> Image.Image:
    big = Image.new("L", (w * SS, h * SS), 0)
    i = inset * SS
    ImageDraw.Draw(big).rounded_rectangle((i, i, w * SS - 1 - i, h * SS - 1 - i),
                                          radius=max(0.0, (r - inset) * SS), fill=255)
    return _shrink(big, (w, h))


@lru_cache(maxsize=2048)
def ellipse_mask(w: int, h: int, inset: float = 0.0) -> Image.Image:
    big = Image.new("L", (w * SS, h * SS), 0)
    i = inset * SS
    ImageDraw.Draw(big).ellipse((i, i, w * SS - 1 - i, h * SS - 1 - i), fill=255)
    return _shrink(big, (w, h))


@lru_cache(maxsize=1024)
def rounded_rect(w: int, h: int, r: float, fill: str | None, outline: str | None = None,
                 width: float = 1.0) -> Image.Image:
    w, h = max(1, w), max(1, h)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    outer = rounded_mask(w, h, r)
    if outline:
        inner = rounded_mask(w, h, r, width)
        out = Image.alpha_composite(out, _paint(outline, ImageChops.subtract(outer, inner)))
        if fill:
            out = Image.alpha_composite(out, _paint(fill, inner))
    elif fill:
        out = _paint(fill, outer)
    return out


@lru_cache(maxsize=1024)
def circle(d: int, fill: str | None, outline: str | None = None, width: float = 1.0) -> Image.Image:
    d = max(1, d)
    out = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    outer = ellipse_mask(d, d)
    if outline:
        inner = ellipse_mask(d, d, width)
        out = Image.alpha_composite(out, _paint(outline, ImageChops.subtract(outer, inner)))
        if fill:
            out = Image.alpha_composite(out, _paint(fill, inner))
    elif fill:
        out = _paint(fill, outer)
    return out


@lru_cache(maxsize=512)
def soft_shadow(d: int, blur: float, alpha: float = 0.45, offset: float = 0.0) -> Image.Image:
    """Blurred dark disc of diameter *d*, padded so the blur fits."""
    pad = int(math.ceil(blur * 3))
    size = d + 2 * pad
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((pad, pad + offset, pad + d, pad + d + offset), fill=int(255 * alpha))
    return _paint("#000000", mask.filter(ImageFilter.GaussianBlur(blur)))


def quantize(colour: str, step: int = 6) -> str:
    """Round a colour a little so near-identical shades share one cached image."""
    return "#%02x%02x%02x" % tuple(min(255, int(round(v / step) * step)) for v in _rgb(colour))


@lru_cache(maxsize=256)
def glow(size: int, colour: str, strength: float) -> Image.Image:
    """Soft light halo (used behind the lamp orbs); fades out inside its square."""
    mask = Image.new("L", (size, size), 0)
    r = size * 0.23
    c = size / 2
    ImageDraw.Draw(mask).ellipse((c - r, c - r, c + r, c + r), fill=int(255 * max(0.0, min(1.0, strength))))
    return _paint(colour, mask.filter(ImageFilter.GaussianBlur(size * 0.085)))


@lru_cache(maxsize=256)
def orb(d: int, colour: str, level: float) -> Image.Image:
    """A lit lamp: saturated colour with a soft highlight in the centre and a light rim."""
    shade = 0.62 + 0.38 * level
    mix = 0.06 + 0.14 * level
    edge = tuple(int(v * shade) for v in _rgb(colour))
    centre = tuple(int((v + (255 - v) * mix) * shade) for v in _rgb(colour))
    falloff = Image.radial_gradient("L").resize((d, d), Image.BICUBIC).point(
        lambda v: min(255, int(255 * (v / 255) ** 0.8)))
    img = Image.composite(Image.new("RGB", (d, d), edge), Image.new("RGB", (d, d), centre), falloff)
    img = img.convert("RGBA")
    img.putalpha(ellipse_mask(d, d))
    rim = circle(d, None, outline=_lighten(colour, 0.45), width=max(1.0, d / 26))
    return Image.alpha_composite(img, rim)


def _lighten(colour: str, amount: float) -> str:
    r, g, b = _rgb(colour)
    return "#%02x%02x%02x" % tuple(int(v + (255 - v) * amount) for v in (r, g, b))


@lru_cache(maxsize=256)
def pill_gradient(w: int, h: int, stops: tuple[str, ...]) -> Image.Image:
    """Horizontal colour gradient in a pill shape (temperature slider track)."""
    strip = Image.new("RGB", (w, 1))
    cols = [_rgb(s) for s in stops]
    for x in range(w):
        t = x / max(1, w - 1) * (len(cols) - 1)
        i = min(int(t), len(cols) - 2)
        f = t - i
        strip.putpixel((x, 0), tuple(int(cols[i][k] + (cols[i + 1][k] - cols[i][k]) * f) for k in range(3)))
    img = strip.resize((w, h)).convert("RGBA")
    img.putalpha(rounded_mask(w, h, h / 2))
    return img


@lru_cache(maxsize=64)
def toggle(w: int, h: int, on: bool, enabled: bool, on_colour: str, off_colour: str,
           disabled_colour: str, knob_colour: str) -> Image.Image:
    track = on_colour if on else off_colour
    if not enabled:
        track = disabled_colour
    k = h - max(4, round(h * 0.22))
    margin = (h - k) / 2
    x = w - margin - k if on else margin
    shadow = soft_shadow(k, max(1.0, h / 14), 0.35, offset=h / 24)
    pad = (shadow.width - k) // 2
    # Work on a padded canvas so the knob's shadow never needs negative offsets.
    canvas = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    canvas.alpha_composite(rounded_rect(w, h, h / 2, track), (pad, pad))
    knob_at = (pad + int(round(x)), pad + int(round(margin)))
    canvas.alpha_composite(shadow, (knob_at[0] - pad, knob_at[1] - pad))
    canvas.alpha_composite(circle(k, knob_colour if enabled else "#8b919c"), knob_at)
    return canvas.crop((pad, pad, pad + w, pad + h))


@lru_cache(maxsize=1)
def wheel(size: int, radius_inset: float = 1.0) -> Image.Image:
    """Hue/saturation colour wheel with a smooth edge."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    c = (size - 1) / 2
    radius = size / 2 - radius_inset
    for y in range(size):
        dy = c - y
        for x in range(size):
            dx = x - c
            r = math.hypot(dx, dy)
            hue = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
            rr, gg, bb = colorsys.hsv_to_rgb(hue, min(1.0, r / radius), 1.0)
            px[x, y] = (int(rr * 255), int(gg * 255), int(bb * 255))
    img = img.convert("RGBA")
    big = Image.new("L", (size * SS, size * SS), 0)
    inset = radius_inset * SS
    ImageDraw.Draw(big).ellipse((inset, inset, size * SS - 1 - inset, size * SS - 1 - inset), fill=255)
    img.putalpha(_shrink(big, (size, size)))
    return img


def marker(d: int, colour: str) -> Image.Image:
    """Picker handle: soft shadow, white ring, colour core."""
    return _marker(d, colour)


@lru_cache(maxsize=512)
def _marker(d: int, colour: str) -> Image.Image:
    shadow = soft_shadow(d, max(1.0, d / 9), 0.55)
    out = Image.new("RGBA", shadow.size, (0, 0, 0, 0))
    out.alpha_composite(shadow)
    pad = (shadow.width - d) // 2
    out.alpha_composite(circle(d, "#ffffff"), (pad, pad))
    inner = d - max(4, round(d * 0.36))
    off = pad + (d - inner) // 2
    out.alpha_composite(circle(inner, colour), (off, off))
    return out


def paste_clipped(base: Image.Image, overlay: Image.Image, x: int, y: int) -> None:
    """alpha_composite that also accepts offsets partly outside *base*."""
    left, top = max(0, -x), max(0, -y)
    right = min(overlay.width, base.width - x)
    bottom = min(overlay.height, base.height - y)
    if right <= left or bottom <= top:
        return
    base.alpha_composite(overlay.crop((left, top, right, bottom)), (x + left, y + top))


@lru_cache(maxsize=128)
def dashed_ring(d: int, colour: str, width: float, dashes: int = 14) -> Image.Image:
    big = Image.new("L", (d * SS, d * SS), 0)
    draw = ImageDraw.Draw(big)
    step = 360 / dashes
    inset = width * SS / 2
    for i in range(dashes):
        start = i * step
        draw.arc((inset, inset, d * SS - 1 - inset, d * SS - 1 - inset), start, start + step * 0.55,
                 fill=255, width=max(1, int(width * SS)))
    return _paint(colour, _shrink(big, (d, d)))


@lru_cache(maxsize=256)
def lamp_tile(w: int, h: int, radius: float, fill: str, border: str, border_width: float,
              centre: tuple[int, int], orb_d: int, colour: str | None, level: float,
              planned: bool, off_fill: str, off_ring: str) -> Image.Image:
    """Card + glow + lamp orb as one image, clipped to the card's rounded shape.

    colour None draws an 'off' lamp; planned=True adds a dashed ring (a colour
    that is queued but not on the lamp yet).
    """
    card = rounded_rect(w, h, radius, fill, border, border_width).copy()
    cx, cy = centre
    if colour is not None:
        g = int(orb_d * 2.8)
        paste_clipped(card, glow(g, colour, 0.40 + 0.60 * level if not planned else 0.30),
                      cx - g // 2, cy - g // 2)
        lit = orb(orb_d, colour, level if not planned else 0.35)
        if planned:
            lit = lit.copy()
            lit.putalpha(lit.getchannel("A").point(lambda v: int(v * 0.55)))
        paste_clipped(card, lit, cx - orb_d // 2, cy - orb_d // 2)
        if planned:
            ring_d = orb_d + max(6, orb_d // 5)
            paste_clipped(card, dashed_ring(ring_d, colour, max(1.5, orb_d / 22)),
                          cx - ring_d // 2, cy - ring_d // 2)
    else:
        paste_clipped(card, circle(orb_d, off_fill, outline=off_ring, width=max(1.0, orb_d / 26)),
                      cx - orb_d // 2, cy - orb_d // 2)
    # keep the glow inside the rounded card
    card.putalpha(ImageChops.multiply(card.getchannel("A"), rounded_mask(w, h, radius)))
    return card


@lru_cache(maxsize=16)
def app_icon(size: int) -> Image.Image:
    """The app icon: two overlapping lights (purple, and blue on top), each
    half white in the middle and fully coloured at the rim.
    packaging/make_icon.py turns it into assets/app.ico."""
    big = size * SS
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = max(1, round(0.6 * big))
    # radial_gradient: 0 in the centre, 255 at the rim -> 50 % white in the middle, none at the rim
    white = Image.radial_gradient("L").resize((d, d), Image.BICUBIC).point(lambda v: (255 - v) // 2)
    disc = Image.new("L", (d, d), 0)
    ImageDraw.Draw(disc).ellipse((0, 0, d - 1, d - 1), fill=255)
    for offset, colour in ((-0.16, "#8f7bff"), (0.16, "#4fc3ff")):
        light = Image.composite(Image.new("RGB", (d, d), (255, 255, 255)), Image.new("RGB", (d, d), _rgb(colour)),
                                white).convert("RGBA")
        light.putalpha(disc)
        out.alpha_composite(light, (round(big / 2 + offset * big - d / 2), round(big / 2 - d / 2)))
    # Shrink with premultiplied alpha, so the edges do not get dark fringes.
    return out.convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")


class ImageCache:
    """Recently used Tk PhotoImages, keyed by their Pillow image.

    The Pillow image is stored next to its PhotoImage so its id() cannot be
    reused while the entry exists. Widgets keep their own reference to the
    PhotoImages they show, so dropping an old entry here never blanks a widget.
    """

    def __init__(self, limit: int = 800) -> None:
        from collections import OrderedDict

        self._entries: "OrderedDict[int, tuple[Image.Image, ImageTk.PhotoImage]]" = OrderedDict()
        self._limit = limit

    def tk(self, image: Image.Image) -> ImageTk.PhotoImage:
        key = id(image)
        entry = self._entries.get(key)
        if entry is not None and entry[0] is image:
            self._entries.move_to_end(key)
            return entry[1]
        photo = ImageTk.PhotoImage(image)
        self._entries[key] = (image, photo)
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return photo

    def clear(self) -> None:
        self._entries.clear()


_cache = ImageCache()


def tk_image(image: Image.Image) -> ImageTk.PhotoImage:
    """PhotoImage for a (cached) Pillow image; cheap to call on every redraw."""
    return _cache.tk(image)


def reset_tk_images() -> None:
    """Forget PhotoImages of a previous Tk root (they die with their interpreter)."""
    _cache.clear()
