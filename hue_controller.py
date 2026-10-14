"""Monitor Lights - desktop controller for Philips Hue Bluetooth lamps.

Start:  py hue_controller.py            (or double-click "Monitor Lights.pyw")
        py hue_controller.py --sim      demo with simulated lamps
        py hue_controller.py --sim-flaky  demo with badly behaving lamps

Bluetooth runs on a background thread (hue_engine.EngineThread); this file
is only the Tkinter user interface.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import math
import os
import queue
import sys
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import colorchooser, messagebox
from tkinter import font as tkfont

import hue_colors as colors
import hue_config as config
import hue_gfx as gfx
from hue_engine import (
    EFFECT_NAMES, EFFECTS, IPC_PORT, EngineThread, HueManager, IpcServer, LampInfo, LampState,
    LampStatus, PROJECT_URL, SimDevice, SimTransport, Timing, __version__, controller_running, ipc_request,
    quit_controller, setup_logging, state_fields,
)

log = logging.getLogger("hue")

# ---------------------------------------------------------------------------
# Look and feel
# ---------------------------------------------------------------------------

BG = "#0e1014"
SURFACE = "#161920"
SURFACE_2 = "#1d2129"
SURFACE_3 = "#262b35"
SURFACE_4 = "#323845"
BORDER = "#252a34"
TEXT = "#eceef3"
MUTED = "#9098a8"
FAINT = "#5d6574"
ACCENT = "#8f7bff"
ACCENT_DIM = "#2d2752"
GREEN = "#43d17f"
AMBER = "#f4b553"
RED = "#ff6b6b"
GREY = "#6b7280"

ICON_POWER = ""
ICON_SETTINGS = ""
ICON_REFRESH = ""
ICON_BLUETOOTH = ""
ICON_BRIGHT = ""
ICON_ADD = ""
ICON_DELETE = ""
ICON_WARNING = ""
ICON_SEARCH = ""
ICON_STOP = ""
ICON_FOLDER = ""
ICON_PALETTE = ""
ICON_WEB = ""

TRANSITIONS = [(0, "Instant"), (2, "Fast"), (4, "Normal"), (10, "Slow")]
IDLE_CHOICES = [(0, "Never"), (5, "5 min"), (15, "15 min"), (30, "30 min"), (60, "60 min")]
# How long a choice waits for a lamp that is not connected (minutes; 0 = until it connects).
KEEP_CHOICES = [(1, "1 min"), (10, "10 min"), (60, "1 hour"), (0, "Until connected")]
BACKGROUND_LIMIT = 3600      # an invisible app gives up waiting after an hour
BACKGROUND_START_GRACE = 25  # --background: time for the CLI to hand over its command
EFFECT_BASE = {1: {"mireds": 455}, 2: {"xy": (0.58, 0.39)}}   # warm base for candle/fireplace

SCALE = 1.0
F: "Fonts"


def px(value: float) -> int:
    return int(round(value * SCALE))


class Fonts:
    def __init__(self, root: tk.Misc):
        families = set(tkfont.families(root))

        def pick(*names: str) -> str:
            return next((n for n in names if n in families), names[-1])

        display = pick("Segoe UI Variable Display", "Segoe UI")
        text = pick("Segoe UI Variable Text", "Segoe UI")
        icons = pick("Segoe Fluent Icons", "Segoe MDL2 Assets")
        mono = pick("Cascadia Mono", "Consolas")
        self.title = tkfont.Font(root, family=display, size=16, weight="bold")
        self.h2 = tkfont.Font(root, family=display, size=11, weight="bold")
        self.body = tkfont.Font(root, family=text, size=10)
        self.body_bold = tkfont.Font(root, family=text, size=10, weight="bold")
        self.small = tkfont.Font(root, family=text, size=9)
        self.tiny = tkfont.Font(root, family=text, size=8)
        self.icon = tkfont.Font(root, family=icons, size=12)
        self.icon_small = tkfont.Font(root, family=icons, size=9)
        self.icon_large = tkfont.Font(root, family=icons, size=15)
        self.mono = tkfont.Font(root, family=mono, size=9)


def fit_text(font: tkfont.Font, text: str, width: int) -> str:
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


def gradient_at(stops: list[str], t: float) -> str:
    t = max(0.0, min(1.0, t))
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    return colors.mix(stops[i], stops[i + 1], pos - i)


def lamp_hex(state: LampState) -> str:
    return colors.to_hex(colors.state_to_rgb(state))


def pct_of(brightness: int | None) -> int:
    return max(1, round((brightness or 1) * 100 / 254))


def bri_of(pct: float) -> int:
    return max(1, min(254, round(pct * 254 / 100)))


# ---------------------------------------------------------------------------
# Widgets (every round shape is an anti-aliased image from hue_gfx)
# ---------------------------------------------------------------------------


class Tooltip:
    def __init__(self, widget: tk.Widget, text):
        self.widget, self.text = widget, text
        self.tip: tk.Toplevel | None = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._hide()
        self.job = self.widget.after(450, self._show)

    def _show(self):
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.configure(bg=BORDER)
        tk.Label(self.tip, text=text, bg=SURFACE_3, fg=TEXT, font=F.small, justify="left",
                 padx=px(8), pady=px(5), wraplength=px(300)).pack(padx=1, pady=1)
        x = self.widget.winfo_rootx() + px(8)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(4)
        self.tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _e=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class SmoothCanvas(tk.Canvas):
    """Canvas that shows anti-aliased Pillow images and keeps them alive."""

    def __init__(self, master, **kw):
        kw.setdefault("bg", master.cget("bg"))
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(master, **kw)
        self._refs: dict[str, list] = {}

    def draw_image(self, x, y, image, *, anchor="nw", tag="art"):
        photo = gfx.tk_image(image)
        self._refs.setdefault(tag, []).append(photo)
        return self.create_image(int(round(x)), int(round(y)), image=photo, anchor=anchor, tags=(tag,))

    def clear(self, tag="art"):
        self.delete(tag)
        self._refs.pop(tag, None)


class PillButton(SmoothCanvas):
    """Rounded, flat button."""

    def __init__(self, master, text="", *, icon=None, command=None, width=None, height=34,
                 bg=SURFACE_2, hover=SURFACE_3, fg=TEXT, font=None, radius=None, dot=None,
                 dots=None, selected=False, tooltip=None, icon_color=None):
        self.text, self.icon, self.command = text, icon, command
        self.base_bg, self.hover_bg, self.fg = bg, hover, fg
        self.icon_color = icon_color
        self.font = font or F.body
        self.dots = dots or ([dot] if dot else [])
        self.selected = selected
        self.enabled = True
        h = px(height)
        if width is None:
            w = self.font.measure(text) + px(30) if text else h
            if icon and text:
                w += F.icon.measure(icon) + px(6)
            w += len(self.dots) * px(14)
        else:
            w = px(width)
        self.bw, self.bh = w, h
        self.radius = px(radius) if radius is not None else min(h / 2, px(10))
        super().__init__(master, width=w, height=h, cursor="hand2")
        self.bhover = self._pressed = False
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", self._resized)
        if tooltip:
            Tooltip(self, tooltip)
        self._draw()

    def _resized(self, e):
        if (e.width, e.height) != (self.bw, self.bh):
            self.bw, self.bh = e.width, e.height
            self._draw()

    def _set(self, hover=None, pressed=None):
        if hover is not None:
            self.bhover = hover
        if pressed is not None:
            self._pressed = pressed
        self._draw()

    def _release(self, e):
        inside = 0 <= e.x <= self.bw and 0 <= e.y <= self.bh
        self._pressed = False
        self._draw()
        if inside and self.enabled and self.command:
            self.command()

    def set_selected(self, flag: bool):
        if flag != self.selected:
            self.selected = flag
            self._draw()

    def set_enabled(self, flag: bool):
        if flag != self.enabled:
            self.enabled = flag
            self.configure(cursor="hand2" if flag else "arrow")
            self._draw()

    def set_text(self, text: str):
        self.text = text
        self._draw()

    def set_dots(self, dots):
        self.dots = list(dots)
        self._draw()

    def _draw(self):
        self.clear()
        w, h = self.bw, self.bh
        fill, outline, width = self.base_bg, None, 1.0
        if self.selected:
            fill, outline, width = ACCENT_DIM, ACCENT, px(1.5)
        elif self._pressed and self.enabled:
            fill = SURFACE_4
        elif self.bhover and self.enabled:
            fill = self.hover_bg
        fg = self.fg if self.enabled else FAINT
        self.draw_image(0, 0, gfx.rounded_rect(w, h, self.radius, fill, outline, width))
        parts = []
        dot_d = px(10)
        for colour in self.dots:
            parts.append(("dot", colour, dot_d + px(4)))
        if self.icon:
            parts.append(("icon", self.icon, F.icon.measure(self.icon) + (px(6) if self.text else 0)))
        if self.text:
            parts.append(("text", self.text, self.font.measure(self.text)))
        total = sum(p[2] for p in parts)
        x = (w - total) / 2
        cy = h / 2
        for kind, value, part_w in parts:
            if kind == "dot":
                self.draw_image(x, cy, gfx.circle(dot_d, value), anchor="w")
            elif kind == "icon":
                self.create_text(x, cy, text=value, anchor="w", font=F.icon, tags=("art",),
                                 fill=self.icon_color if (self.icon_color and self.enabled) else fg)
            else:
                avail = w - x - px(10)
                self.create_text(x, cy, text=fit_text(self.font, value, int(avail)), anchor="w",
                                 font=self.font, fill=fg, tags=("art",))
            x += part_w


class Slider(SmoothCanvas):
    """Horizontal slider; optional colour gradient for the track."""

    def __init__(self, master, *, from_=0, to=100, value=50, on_change=None, gradient=None,
                 height=30, track=8, knob=10, step=1):
        super().__init__(master, height=px(height), width=px(200), cursor="hand2", takefocus=1)
        self.from_, self.to, self.step = from_, to, step
        self.value = value
        self.on_change = on_change
        self.gradient = tuple(gradient) if gradient else None
        self.track, self.knob = px(track), px(knob)
        self.sw, self.sh = px(200), px(height)
        self._dragging = False
        self.bind("<Configure>", self._resized)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", lambda e: self._nudge(1 if e.delta > 0 else -1))
        self.bind("<Left>", lambda e: self._nudge(-1))
        self.bind("<Right>", lambda e: self._nudge(1))
        self._draw()

    @property
    def x0(self):
        return self.knob + px(3)

    @property
    def x1(self):
        return max(self.x0 + 1, self.sw - self.knob - px(3))

    def _frac(self, value):
        return (value - self.from_) / (self.to - self.from_)

    def _resized(self, e):
        if (e.width, e.height) != (self.sw, self.sh):
            self.sw, self.sh = e.width, e.height
            self._draw()

    def _draw(self):
        self.clear("track")
        t, cy = self.track, self.sh / 2
        length = int(round(self.x1 - self.x0 + t))
        if self.gradient:
            image = gfx.pill_gradient(length, t, self.gradient)
        else:
            image = gfx.rounded_rect(length, t, t / 2, SURFACE_4)
        self.draw_image(self.x0 - t / 2, cy - t / 2, image, tag="track")
        self._place()

    def _place(self):
        self.clear("fill")
        self.clear("knob")
        frac = self._frac(self.value)
        xk = self.x0 + frac * (self.x1 - self.x0)
        t, cy = self.track, self.sh / 2
        if not self.gradient:
            length = max(t, int(round(xk - self.x0 + t)))
            self.draw_image(self.x0 - t / 2, cy - t / 2, gfx.rounded_rect(length, t, t / 2, ACCENT), tag="fill")
        core = gfx.quantize(gradient_at(list(self.gradient), frac), 4) if self.gradient else "#ffffff"
        self.draw_image(xk, cy, gfx.marker(2 * self.knob, core), anchor="center", tag="knob")

    def set(self, value):
        value = max(self.from_, min(self.to, value))
        if value != self.value:
            self.value = value
            self._place()

    def _value_at(self, x):
        frac = (x - self.x0) / max(1, self.x1 - self.x0)
        value = self.from_ + max(0.0, min(1.0, frac)) * (self.to - self.from_)
        return round(value / self.step) * self.step

    def _press(self, e):
        self.focus_set()
        self._dragging = True
        self._update(e.x, final=False)

    def _motion(self, e):
        if self._dragging:
            self._update(e.x, final=False)

    def _release(self, e):
        if self._dragging:
            self._dragging = False
            self._update(e.x, final=True)

    def _update(self, x, final):
        value = self._value_at(x)
        changed = value != self.value
        self.value = value
        self._place()
        if self.on_change and (changed or final):
            self.on_change(value, final)

    def _nudge(self, direction):
        self.set(self.value + direction * max(self.step, (self.to - self.from_) / 50))
        if self.on_change:
            self.on_change(self.value, True)


class Toggle(SmoothCanvas):
    def __init__(self, master, value=False, command=None, width=44, height=24, bg=None):
        super().__init__(master, width=px(width), height=px(height), bg=bg or master.cget("bg"),
                         cursor="hand2")
        self.value, self.command, self.enabled = bool(value), command, True
        self.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        self.clear()
        w, h = int(self["width"]), int(self["height"])
        self.draw_image(0, 0, gfx.toggle(w, h, self.value, self.enabled, ACCENT, SURFACE_4, SURFACE_3, "#ffffff"))

    def set(self, value, enabled=True):
        value = bool(value)
        if value != self.value or enabled != self.enabled:
            self.value, self.enabled = value, enabled
            self._draw()

    def _click(self, _e):
        if self.enabled:
            self.value = not self.value
            self._draw()
            if self.command:
                self.command(self.value)
        return "break"


class Segmented(SmoothCanvas):
    def __init__(self, master, options, value, command, height=36):
        super().__init__(master, height=px(height), cursor="hand2")
        self.options, self.value, self.command = list(options), value, command
        self._hover = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._motion(None))

    def set_options(self, options, value=None):
        self.options = list(options)
        if value is not None:
            self.value = value
        self._draw()

    def set(self, value):
        if value != self.value:
            self.value = value
            self._draw()

    def _index_at(self, x):
        w = self.winfo_width()
        pad = px(3)
        seg = (w - 2 * pad) / max(1, len(self.options))
        return int(max(0, min(len(self.options) - 1, (x - pad) // seg)))

    def _motion(self, e):
        hover = None if e is None else self._index_at(e.x)
        if hover != self._hover:
            self._hover = hover
            self._draw()

    def _click(self, e):
        key = self.options[self._index_at(e.x)][0]
        self.set(key)
        if self.command:
            self.command(key)

    def _draw(self):
        self.clear()
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or not self.options:
            return
        self.draw_image(0, 0, gfx.rounded_rect(w, h, h / 2, SURFACE_2, BORDER, 1.0))
        pad = px(3)
        seg = (w - 2 * pad) / len(self.options)
        inner_h = h - 2 * pad
        for i, (key, label) in enumerate(self.options):
            x0 = pad + i * seg
            selected = key == self.value
            if selected or i == self._hover:
                pill = gfx.rounded_rect(int(round(seg)), inner_h, inner_h / 2, SURFACE_4 if selected else SURFACE_3)
                self.draw_image(x0, pad, pill)
            font = F.body_bold if selected else F.body
            self.create_text(x0 + seg / 2, h / 2, text=fit_text(font, label, int(seg - px(10))),
                             fill=TEXT if selected else MUTED, font=font, tags=("art",))


class TabBar(SmoothCanvas):
    def __init__(self, master, options, value, command):
        super().__init__(master, height=px(34), cursor="hand2")
        self.options, self.value, self.command = options, value, command
        self._boxes: list[tuple[float, float, str]] = []
        self._hover = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._motion(None))

    def _key_at(self, x):
        for x0, x1, key in self._boxes:
            if x0 - px(8) <= x <= x1 + px(8):
                return key
        return None

    def _motion(self, e):
        hover = None if e is None else self._key_at(e.x)
        if hover != self._hover:
            self._hover = hover
            self._draw()

    def _click(self, e):
        key = self._key_at(e.x)
        if key and key != self.value:
            self.value = key
            self._draw()
            self.command(key)

    def _draw(self):
        self.clear()
        h = self.winfo_height()
        self._boxes = []
        x = px(2)
        self.create_line(0, h - 1, self.winfo_width(), h - 1, fill=BORDER, tags=("art",))
        for key, label in self.options:
            selected = key == self.value
            font = F.body_bold
            width = font.measure(label)
            colour = TEXT if selected or key == self._hover else MUTED
            self.create_text(x, h / 2 - px(2), text=label, anchor="w", font=font, fill=colour, tags=("art",))
            if selected:
                self.draw_image(x, h - px(3), gfx.rounded_rect(width, px(3), px(1.5), ACCENT))
            self._boxes.append((x, x + width, key))
            x += width + px(24)


class ColorWheel(SmoothCanvas):
    def __init__(self, master, size, on_pick):
        self.size = px(size)
        super().__init__(master, width=self.size, height=self.size, cursor="crosshair")
        self.on_pick = on_pick
        self.radius = self.size / 2 - 1.0
        self.draw_image(0, 0, gfx.wheel(self.size), tag="wheel")
        self.place_hs(0.75, 0.6)
        self.bind("<ButtonPress-1>", lambda e: self._pick(e, False))
        self.bind("<B1-Motion>", lambda e: self._pick(e, False))
        self.bind("<ButtonRelease-1>", lambda e: self._pick(e, True))

    def place_hs(self, hue, sat):
        self.clear("marker")
        c = self.size / 2
        angle = hue * 2 * math.pi
        x = c + math.cos(angle) * sat * self.radius
        y = c - math.sin(angle) * sat * self.radius
        colour = gfx.quantize(colors.to_hex(colors.hs_to_rgb(hue, sat)), 4)
        self.draw_image(x, y, gfx.marker(px(22), colour), anchor="center", tag="marker")

    def set_xy(self, xy):
        hue, sat = colors.rgb_to_hs(*colors.xy_to_rgb(*xy))
        self.place_hs(hue, sat)

    def _pick(self, e, final):
        c = self.size / 2
        dx, dy = e.x - c, c - e.y
        sat = min(1.0, math.hypot(dx, dy) / self.radius)
        hue = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
        self.place_hs(hue, sat)
        rgb = colors.hs_to_rgb(hue, sat)
        self.on_pick(colors.rgb_to_xy(*rgb), final)


class Swatch(SmoothCanvas):
    def __init__(self, master, colour, command, size=36, tooltip=None):
        self.s = px(size)
        super().__init__(master, width=self.s, height=self.s, cursor="hand2")
        self.colour, self.command = colour, command
        self.selected = self._hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: command())
        if tooltip:
            Tooltip(self, tooltip)
        self._draw()

    def _set_hover(self, flag):
        self._hover = flag
        self._draw()

    def set_selected(self, flag):
        if flag != self.selected:
            self.selected = flag
            self._draw()

    def _draw(self):
        self.clear()
        s = self.s
        if self.selected or self._hover:
            ring = "#ffffff" if self.selected else SURFACE_4
            self.draw_image(0, 0, gfx.circle(s, None, outline=ring, width=px(2)))
        m = px(5)
        self.draw_image(m, m, gfx.circle(s - 2 * m, self.colour))


class Card(SmoothCanvas):
    """Rounded panel; put widgets in ``card.body``."""

    def __init__(self, master, height, pad=14, radius=16, fill=SURFACE):
        super().__init__(master, height=px(height))
        self.pad, self.radius, self.fill = px(pad), px(radius), fill
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(self.pad, self.pad, window=self.body, anchor="nw")
        self.bind("<Configure>", self._layout)

    def _layout(self, e):
        self.clear("bgshape")
        item = self.draw_image(0, 0, gfx.rounded_rect(e.width, e.height, self.radius, self.fill, BORDER, 1.0),
                               tag="bgshape")
        self.tag_lower(item)
        self.itemconfigure(self._win, width=e.width - 2 * self.pad, height=e.height - 2 * self.pad)


class Banner(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.inner = tk.Frame(self, bg="#3a1f25", padx=px(12), pady=px(9))
        self.inner.pack(fill="x")
        self.icon = tk.Label(self.inner, text=ICON_WARNING, font=F.icon, bg="#3a1f25", fg=RED)
        self.icon.pack(side="left", padx=(0, px(10)))
        self.label = tk.Label(self.inner, text="", font=F.small, bg="#3a1f25", fg=TEXT, justify="left",
                              anchor="w", wraplength=px(300))
        self.label.pack(side="left", fill="x", expand=True)
        self.button: PillButton | None = None
        self.key = None

    def show(self, key, text, tone="error", action=None):
        bg, fg = ("#3a1f25", RED) if tone == "error" else ("#3a311d", AMBER)
        if key == self.key and self.winfo_ismapped():
            self.label.configure(text=text)
            return
        self.key = key
        for widget in (self.inner, self.icon, self.label):
            widget.configure(bg=bg)
        self.icon.configure(fg=fg)
        # Keep the banner inside the window width (a long line would widen the window).
        self.label.configure(text=text, wraplength=px(215) if action else px(370))
        if self.button is not None:
            self.button.destroy()
            self.button = None
        if action:
            label, command = action
            self.button = PillButton(self.inner, label, command=command, height=30, bg=SURFACE_3,
                                     hover=SURFACE_4, font=F.small)
            self.button.pack(side="right", padx=(px(10), 0))

    def hide(self):
        self.key = None


class LampTile(SmoothCanvas):
    def __init__(self, master, app: "App", lamp_id: str, height=128):
        super().__init__(master, height=px(height), width=px(200), cursor="hand2")
        self.app, self.lamp_id = app, lamp_id
        self.info: LampInfo | None = None
        self.selected = False
        self.blink = False
        self.toggle = Toggle(self, bg=SURFACE, command=lambda v: app.set_power([lamp_id], v))
        self._toggle_win = self.create_window(0, 0, window=self.toggle, anchor="se")
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", lambda e: app.select_target(lamp_id))
        self.bind("<Double-Button-1>", lambda e: app.identify(lamp_id))
        self.bind("<Button-3>", self._menu)
        Tooltip(self, self._tooltip)

    def _tooltip(self):
        info = self.info
        if info is None:
            return ""
        lines = [f"{info.name}  ({info.address})", info.detail or ""]
        if info.pending and not info.connected:
            lines.append("Your choice is queued and is applied as soon as the light connects.")
        if info.reported_name and info.reported_name != info.name:
            lines.append(f"Bluetooth name: {info.reported_name}")
        if info.rssi is not None:
            lines.append(f"Signal strength: {info.rssi} dBm")
        if info.latency_ms is not None:
            lines.append(f"Last command: {info.latency_ms:.0f} ms")
        lines.append("Click = select · double-click = blink · right-click = menu")
        return "\n".join(l for l in lines if l)

    def _menu(self, e):
        menu = tk.Menu(self, tearoff=0, bg=SURFACE_2, fg=TEXT, activebackground=ACCENT_DIM,
                       activeforeground=TEXT, bd=0, relief="flat", font=F.body)
        menu.add_command(label="Blink (which light is this?)", command=lambda: self.app.identify(self.lamp_id))
        menu.add_command(label="Reconnect", command=lambda: self.app.reconnect([self.lamp_id]))
        menu.add_command(label="Release connection", command=lambda: self.app.release([self.lamp_id]))
        menu.add_separator()
        menu.add_command(label="Rename…", command=lambda: self.app.rename_lamp(self.lamp_id))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def update_info(self, info: LampInfo, selected: bool):
        self.info, self.selected = info, selected
        self.redraw()

    def redraw(self):
        self.clear()
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20:
            return
        info = self.info
        # A colour that is queued for a lamp that is not connected yet is shown
        # dimmed with a dashed ring: "this is what it will become".
        planned = bool(info and info.pending and not info.connected)
        state = info.target if info else LampState()
        cx, cy, orb_d = px(42), px(50), px(40)
        colour, level = None, 0.0
        if state.power:
            colour = gfx.quantize(lamp_hex(state))
            level = round((state.brightness or 1) / 254 * 20) / 20
        art = gfx.lamp_tile(w, h, px(16), SURFACE, ACCENT if self.selected else BORDER,
                            px(2) if self.selected else 1.0, (cx, cy), orb_d, colour, level,
                            planned, SURFACE_2, SURFACE_4)
        self.draw_image(0, 0, art)
        if not state.power:
            self.create_text(cx, cy, text=ICON_POWER, font=F.icon, tags=("art",),
                             fill=AMBER if planned else (FAINT if state.power is False else SURFACE_4))
        x = px(80)
        name = info.name if info else self.lamp_id
        avail = w - x - px(14)
        self.create_text(x, px(30), text=fit_text(F.h2, name, avail), anchor="w", font=F.h2, fill=TEXT,
                         tags=("art",))
        dot, text = status_look(info, time.monotonic())
        if info and info.status is LampStatus.CONNECTING and self.blink:
            dot = SURFACE_4
        self.draw_image(x, px(54), gfx.circle(px(8), dot), anchor="w")
        self.create_text(x + px(14), px(54), text=fit_text(F.small, text, avail - px(14)), anchor="w",
                         font=F.small, fill=MUTED, tags=("art",))
        bottom = h - px(24)
        if state.power is None:
            label = "—"
        elif not state.power:
            label = "Off"
        else:
            label = f"{pct_of(state.brightness)}%"
        self.create_text(px(18), bottom, text=label, anchor="w", font=F.body_bold, fill=TEXT, tags=("art",))
        extra_x = px(18) + F.body_bold.measure(label) + px(10)
        extra_w = w - px(66) - extra_x                 # stop before the on/off switch
        extra = None
        if planned:
            extra = ("queued", AMBER)
        elif state.effect:
            extra = (f"✦ {EFFECT_NAMES.get(state.effect, 'Effect')}", AMBER)
        elif state.mode == "ct" and state.mireds and state.power:
            extra = (f"{colors.mireds_to_kelvin(state.mireds)} K", MUTED)
        if extra and extra_w > px(20):
            self.create_text(extra_x, bottom, text=fit_text(F.small, extra[0], extra_w), anchor="w",
                             font=F.small, fill=extra[1], tags=("art",))
        self.coords(self._toggle_win, w - px(14), h - px(12))
        self.toggle.set(bool(state.power), enabled=not (info and info.status is LampStatus.STOPPED))


def status_look(info: LampInfo | None, now: float) -> tuple[str, str]:
    """(dot colour, short Dutch status line)."""
    if info is None:
        return GREY, "Starting…"
    s = info.status
    if s is LampStatus.CONNECTED:
        return GREEN, "Connected"
    # Short texts: the tile is narrow. The tooltip has the full explanation.
    if s is LampStatus.CONNECTING:
        return AMBER, "Connecting…" if info.attempt <= 1 else f"Attempt {info.attempt}…"
    if s is LampStatus.WAITING and info.error_kind == "cancelled":
        # The lamp answers, Windows is still tearing down an old link: not an error for the user.
        return AMBER, "One moment…"
    if s is LampStatus.WAITING and info.error_kind == "busy":
        return RED, "In use by another app"
    if s is LampStatus.WAITING:
        base = "No signal" if info.visible is False else "Unreachable"
        if info.retry_at is not None:
            secs = math.ceil(info.retry_at - now)
            return RED, f"{base} · {secs} s" if secs > 0 else f"{base} · now…"
        return RED, base
    if s is LampStatus.BT_OFF:
        return RED, "Bluetooth is off"
    if s is LampStatus.NO_ADAPTER:
        return RED, "No Bluetooth adapter"
    if s is LampStatus.NOT_PAIRED:
        return RED, "Not paired with Windows"
    if s is LampStatus.RELEASED:
        return GREY, "Released"
    if s is LampStatus.STARTING:
        return GREY, "Starting…"
    return GREY, "Stopped"


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


def style_toplevel(win: tk.Toplevel, parent: tk.Misc, title: str):
    win.title(title)
    win.configure(bg=BG)
    win.transient(parent)
    win.resizable(False, False)
    dark_title_bar(win)


def ask_text(parent, title, prompt, initial="") -> str | None:
    win = tk.Toplevel(parent)
    style_toplevel(win, parent, title)
    result = {"value": None}
    frame = tk.Frame(win, bg=BG, padx=px(18), pady=px(16))
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text=prompt, font=F.body, bg=BG, fg=TEXT).pack(anchor="w")
    entry = tk.Entry(frame, font=F.body, bg=SURFACE_2, fg=TEXT, insertbackground=TEXT, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT, width=32)
    entry.pack(fill="x", pady=(px(8), px(14)), ipady=px(5))
    entry.insert(0, initial)
    entry.select_range(0, "end")
    buttons = tk.Frame(frame, bg=BG)
    buttons.pack(fill="x")

    def done(value):
        result["value"] = value
        win.destroy()

    PillButton(buttons, "Cancel", command=lambda: done(None)).pack(side="right")
    PillButton(buttons, "OK", command=lambda: done(entry.get().strip() or None), bg=ACCENT_DIM,
               hover=SURFACE_4).pack(side="right", padx=(0, px(8)))
    entry.bind("<Return>", lambda e: done(entry.get().strip() or None))
    entry.bind("<Escape>", lambda e: done(None))
    center_on(win, parent)
    entry.focus_set()
    win.grab_set()
    parent.wait_window(win)
    return result["value"]


def center_on(win: tk.Toplevel, parent: tk.Misc):
    win.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_reqwidth()) // 2
    y = parent.winfo_rooty() + px(80)
    win.geometry(f"+{max(0, x)}+{max(0, y)}")


def section(master, title):
    frame = tk.Frame(master, bg=BG)
    frame.pack(fill="x", pady=(px(14), 0))
    tk.Label(frame, text=title, font=F.h2, bg=BG, fg=TEXT).pack(anchor="w")
    body = tk.Frame(frame, bg=BG)
    body.pack(fill="x", pady=(px(6), 0))
    return body


def note(master, text, colour=MUTED):
    label = tk.Label(master, text=text, font=F.small, bg=master.cget("bg"), fg=colour, justify="left",
                     anchor="w", wraplength=px(440))
    label.pack(fill="x", pady=(px(4), 0))
    return label


class SettingsWindow:
    def __init__(self, app: "App"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        style_toplevel(self.win, app.root, "Settings")
        outer = tk.Frame(self.win, bg=BG, padx=px(20), pady=px(14))
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Settings", font=F.title, bg=BG, fg=TEXT).pack(anchor="w")

        body = section(outer, "Lights")
        self.lamp_rows = tk.Frame(body, bg=BG)
        self.lamp_rows.pack(fill="x")
        self._build_lamp_rows()
        PillButton(body, "Find and add lights…", icon=ICON_SEARCH, command=app.open_scan,
                   height=32).pack(anchor="w", pady=(px(8), 0))

        body = section(outer, "Connection")
        tk.Label(body, text="Release the lights after inactivity", font=F.body, bg=BG, fg=TEXT).pack(anchor="w")
        current = app.cfg["settings"]["idle_release_minutes"]
        self.idle = Segmented(body, [(str(v), l) for v, l in IDLE_CHOICES], str(current),
                              lambda key: app.set_idle_release(int(key)), height=32)
        self.idle.pack(fill="x", pady=(px(6), 0))
        note(body, "Your phone can control the lights too while this app is connected. Releasing them is "
                   "only needed if your phone still cannot connect; the app reconnects by itself on "
                   "your next command.")
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(px(8), 0))
        PillButton(row, "Release now", icon=ICON_BLUETOOTH, command=lambda: app.release(None),
                   height=32).pack(side="left")
        PillButton(row, "Reconnect", icon=ICON_REFRESH, command=lambda: app.reconnect(None),
                   height=32).pack(side="left", padx=(px(8), 0))

        body = section(outer, "Before a light is connected")
        tk.Label(body, text="Keep your choice until the light is reachable", font=F.body, bg=BG,
                 fg=TEXT).pack(anchor="w")
        current = app.cfg["settings"]["pending_keep_minutes"]
        Segmented(body, [(str(v), l) for v, l in KEEP_CHOICES], str(current),
                  lambda key: app.set_pending_keep(int(key)), height=32).pack(fill="x", pady=(px(6), 0))
        note(body, "Pick a color or scene while a light is still connecting or switched off: the app "
                   "applies it as soon as the light is reachable. If you close the window first, the app "
                   "keeps working invisibly until everything is applied (up to an hour).")

        body = section(outer, "Fade for buttons and scenes")
        current = app.cfg["settings"]["transition"]
        Segmented(body, [(str(v), l) for v, l in TRANSITIONS], str(current),
                  lambda key: app.set_transition(int(key)), height=32).pack(fill="x")

        body = section(outer, "About")
        note(body, f"Monitor Lights {__version__} · settings and logs in %APPDATA%\\Monitor Lights")
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(px(8), 0))
        PillButton(row, "Help & updates", icon=ICON_WEB, command=app.open_project_page,
                   height=32).pack(side="left")
        PillButton(row, "Open log folder", icon=ICON_FOLDER, command=app.open_logs,
                   height=32).pack(side="left", padx=(px(8), 0))
        note(body, "Tips for flaky connections: keep the Bluetooth adapter away from USB 3 ports and metal "
                   "(a short USB extension cable often helps) and in sight of the lights. The app retries "
                   "failed connections by itself; the log shows what happens.")

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(px(16), 0))
        PillButton(footer, "Close", command=self.close, width=110).pack(side="right")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())
        center_on(self.win, app.root)

    def _build_lamp_rows(self):
        for child in self.lamp_rows.winfo_children():
            child.destroy()
        if not self.app.cfg["lamps"]:
            note(self.lamp_rows, "No lights added yet.")
        for lamp in self.app.cfg["lamps"]:
            row = tk.Frame(self.lamp_rows, bg=SURFACE, padx=px(10), pady=px(8))
            row.pack(fill="x", pady=(0, px(6)))
            entry = tk.Entry(row, font=F.body, bg=SURFACE_2, fg=TEXT, insertbackground=TEXT, relief="flat",
                             highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT, width=20)
            entry.insert(0, lamp["name"])
            entry.pack(side="left", ipady=px(4))
            save = lambda e, lid=lamp["id"], ent=entry: self.app.rename_lamp(lid, ent.get())  # noqa: E731
            entry.bind("<Return>", save)
            entry.bind("<FocusOut>", save)
            tk.Label(row, text=lamp["address"], font=F.small, bg=SURFACE, fg=FAINT).pack(side="left", padx=px(10))
            PillButton(row, "", icon=ICON_DELETE, tooltip="Remove", height=30, bg=SURFACE_2,
                       command=lambda lid=lamp["id"]: self._remove(lid)).pack(side="right")
            PillButton(row, "Blink", height=30, bg=SURFACE_2, font=F.small,
                       command=lambda lid=lamp["id"]: self.app.identify(lid)).pack(side="right", padx=(0, px(6)))

    def _remove(self, lamp_id):
        if self.app.remove_lamp(lamp_id):
            self._build_lamp_rows()

    def refresh(self):
        if self.win.winfo_exists():
            self._build_lamp_rows()

    def close(self):
        self.app.settings_window = None
        self.win.destroy()


class ScanWindow:
    def __init__(self, app: "App"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        style_toplevel(self.win, app.root, "Find lights")
        outer = tk.Frame(self.win, bg=BG, padx=px(20), pady=px(14))
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Find lights", font=F.title, bg=BG, fg=TEXT).pack(anchor="w")
        self.status = note(outer, "")
        self.rows = tk.Frame(outer, bg=BG, width=px(460))
        self.rows.pack(fill="x", pady=(px(10), 0))
        note(outer, "To add a light it must be in pairing mode. In the Philips Hue app open Settings → "
                    "Voice assistants → Amazon Alexa (or Google Home) → Make discoverable. A new or "
                    "factory-reset light is already in pairing mode. Lights that are already paired "
                    "with Windows connect right away.")
        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(px(14), 0))
        PillButton(footer, "Close", command=self.win.destroy, width=110).pack(side="right")
        self.again = PillButton(footer, "Search again", icon=ICON_SEARCH, command=self.scan)
        self.again.pack(side="right", padx=(0, px(8)))
        center_on(self.win, app.root)
        self.scan()

    def scan(self):
        self.again.set_enabled(False)
        self.status.configure(text="Searching for Hue lights nearby… (about 8 seconds)", fg=MUTED)
        for child in self.rows.winfo_children():
            child.destroy()
        self.app.run_engine(lambda mgr: mgr.discover(8.0), self._found)

    def _found(self, future):
        if not self.win.winfo_exists():
            return
        self.again.set_enabled(True)
        try:
            found = future.result()
        except Exception as exc:
            self.status.configure(text=f"Search failed: {exc}", fg=RED)
            return
        known = {lamp["address"] for lamp in self.app.cfg["lamps"]}
        self.status.configure(text=f"Found {len(found)} Hue light(s)." if found else
                              "No Hue lights heard. Are they switched on and nearby?", fg=MUTED)
        for seen in found:
            row = tk.Frame(self.rows, bg=SURFACE, padx=px(10), pady=px(8))
            row.pack(fill="x", pady=(0, px(6)))
            label = f"{seen.name or 'Hue light'}   {seen.address}   {seen.rssi} dBm"
            tk.Label(row, text=label, font=F.body, bg=SURFACE, fg=TEXT).pack(side="left")
            if seen.address in known:
                tk.Label(row, text="already added", font=F.small, bg=SURFACE, fg=FAINT).pack(side="right")
            else:
                button = PillButton(row, "Add", icon=ICON_ADD, height=30, bg=SURFACE_2)
                button.command = lambda s=seen, b=button: self._add(s, b)
                button.pack(side="right")

    def _add(self, seen, button):
        button.set_enabled(False)
        button.set_text("Pairing…")

        def done(future):
            try:
                name = future.result()
            except Exception as exc:
                button.set_enabled(True)
                button.set_text("Try again")
                self.status.configure(text=f"Pairing failed: {exc}. Put the light in pairing mode (see below) and try again, or pair "
                                                f"it in Windows Settings → Bluetooth & devices.", fg=RED)
                return
            self.app.add_lamp(seen.address, name or seen.name)
            button.set_text("Added")
            self.status.configure(text=f"{name or seen.address} was added.", fg=GREEN)

        self.app.run_engine(lambda mgr: mgr.pair(seen.address), done)


# ---------------------------------------------------------------------------
# Windows niceties
# ---------------------------------------------------------------------------


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_app_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HueControl.MonitorLights")
    except Exception:
        pass


def dark_title_bar(window: tk.Misc):
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        on = ctypes.c_int(1)
        for attribute in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        r, g, b = colors.from_hex(BG)
        caption = ctypes.c_int(r | (g << 8) | (b << 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
    except Exception:
        pass


def work_area() -> tuple[int, int, int, int]:
    """Usable desktop rectangle of the main screen (without the taskbar)."""
    import ctypes.wintypes

    rect = ctypes.wintypes.RECT()
    try:
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):   # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1280, 720


class Toast:
    """Small notification bottom-right that never steals the keyboard focus."""

    def __init__(self, root: tk.Misc, text: str, *, duration: int = 5000, on_close=None, on_click=None,
                 alpha: float = 1.0):
        self.on_close, self.on_click = on_close, on_click
        previous = ctypes.windll.user32.GetForegroundWindow()
        self.win = win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", alpha)        # tests use 0: rendered, but invisible
        win.configure(bg=BORDER)
        body = tk.Frame(win, bg=SURFACE_2, padx=px(14), pady=px(12))
        body.pack(padx=1, pady=1)
        widgets = [body]
        texts = tk.Frame(body, bg=SURFACE_2)
        texts.pack(side="left")
        widgets += [texts,
                    tk.Label(texts, text="Monitor Lights", font=F.body_bold, bg=SURFACE_2, fg=TEXT),
                    tk.Label(texts, text=text, font=F.small, bg=SURFACE_2, fg=MUTED, justify="left",
                             wraplength=px(270))]
        widgets[-2].pack(anchor="w")
        widgets[-1].pack(anchor="w")
        for widget in widgets:
            widget.bind("<Button-1>", self._clicked)
        win.update_idletasks()
        left, top, right, bottom = work_area()
        win.geometry(f"+{right - win.winfo_reqwidth() - px(16)}+{bottom - win.winfo_reqheight() - px(16)}")
        try:
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST (+ click-through when invisible)
            style |= 0x08000000 | 0x80 | 0x8 | (0x20 if alpha == 0 else 0)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)
        except Exception:
            pass
        win.deiconify()
        win.update_idletasks()
        if previous and ctypes.windll.user32.GetForegroundWindow() != previous:
            ctypes.windll.user32.SetForegroundWindow(previous)   # give the focus straight back
        self._job = win.after(duration, self.close)

    def _clicked(self, _e=None):
        if self.on_click:
            self.on_click()
        self.close()

    def close(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
        self.win = None
        if self.on_close:
            callback, self.on_close = self.on_close, None
            callback()


class QueueLogHandler(logging.Handler):
    def __init__(self, events: queue.Queue):
        super().__init__(logging.INFO)
        self.events = events

    def emit(self, record):
        self.events.put(("log", record))


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk, cfg: dict, args):
        self.root, self.cfg, self.args = root, cfg, args
        self.sim = args.sim or args.sim_flaky
        self.events: queue.Queue = queue.Queue()
        self.infos: dict[str, LampInfo] = {}
        ui = cfg.setdefault("ui", {})
        self.target = ui.get("target", "all")
        self.tab = ui.get("tab", "color")
        self.log_open = bool(ui.get("log_open", False))
        self._quiet_until = 0.0     # don't overwrite controls with lamp reports before this
        self._drag_fields = None
        self._drag_job = None
        self._blink = False
        self.settings_window: SettingsWindow | None = None
        self.engine: EngineThread | None = None
        self.ipc: IpcServer | None = None
        # Closing while choices still wait for a lamp keeps the app running invisibly.
        self._closing = False
        self._background = False
        self._started_hidden = False
        self._seen_waiting = False
        self._announced = False
        self._ever_shown = False
        self._close_started = 0.0
        self._close_job = None
        self._dropped_before: dict[str, int] = {}
        self.toast_factory = Toast
        if self.target != "all" and self.target not in {l["id"] for l in cfg["lamps"]}:
            self.target = "all"

        self._log_handler = QueueLogHandler(self.events)
        log.addHandler(self._log_handler)
        self._build()
        self._start_engine()
        self._poll_job = self.root.after(40, self._poll)
        self._tick_job = self.root.after(500, self._tick)

    # -- layout ------------------------------------------------------------------

    def _build(self):
        root = self.root
        root.configure(bg=BG)
        outer = tk.Frame(root, bg=BG, padx=px(18), pady=px(14))
        outer.pack(fill="both", expand=True)
        self.outer = outer

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        self.title_label = tk.Label(header, text="Monitor Lights" + ("  (demo)" if self.sim else ""),
                                    font=F.title, bg=BG, fg=TEXT)
        self.title_label.grid(row=0, column=0, sticky="w")
        self.subtitle = tk.Label(header, text="Starting…", font=F.small, bg=BG, fg=MUTED)
        self.subtitle.grid(row=1, column=0, sticky="w")
        buttons = tk.Frame(header, bg=BG)
        buttons.grid(row=0, column=1, rowspan=2, sticky="e")
        PillButton(buttons, icon=ICON_REFRESH, command=lambda: self.reconnect(None), height=36,
                   bg=BG, hover=SURFACE_2, tooltip="Reconnect (Ctrl+R)").pack(side="left")
        PillButton(buttons, icon=ICON_SETTINGS, command=self.open_settings, height=36, bg=BG,
                   hover=SURFACE_2, tooltip="Settings").pack(side="left", padx=(px(4), 0))

        self.banner = Banner(outer)
        self.banner_anchor = tk.Frame(outer, bg=BG, height=0)
        self.banner_anchor.pack(fill="x")

        self.tiles = tk.Frame(outer, bg=BG)
        self.tiles.pack(fill="x", pady=(px(12), 0))
        self.tile_widgets: dict[str, LampTile] = {}

        self.segment = Segmented(outer, [], self.target, self.select_target)
        self.segment.pack(fill="x", pady=(px(12), 0))

        power = tk.Frame(outer, bg=BG)
        power.pack(fill="x", pady=(px(12), 0))
        power.columnconfigure((0, 1), weight=1, uniform="power")
        self.btn_on = PillButton(power, "On", icon=ICON_POWER, height=42, font=F.body_bold,
                                 command=lambda: self.set_power(None, True))
        self.btn_on.grid(row=0, column=0, sticky="ew", padx=(0, px(5)))
        self.btn_off = PillButton(power, "Off", height=42, font=F.body_bold,
                                  command=lambda: self.set_power(None, False))
        self.btn_off.grid(row=0, column=1, sticky="ew", padx=(px(5), 0))

        bri = tk.Frame(outer, bg=BG)
        bri.pack(fill="x", pady=(px(14), 0))
        top = tk.Frame(bri, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text=ICON_BRIGHT, font=F.icon_small, bg=BG, fg=MUTED).pack(side="left")
        tk.Label(top, text="Brightness", font=F.body, bg=BG, fg=MUTED).pack(side="left", padx=(px(6), 0))
        self.bri_label = tk.Label(top, text="—", font=F.body_bold, bg=BG, fg=TEXT)
        self.bri_label.pack(side="right")
        self.bri = Slider(bri, from_=1, to=100, value=78, on_change=self._on_brightness, height=32)
        self.bri.pack(fill="x", pady=(px(4), 0))

        self.tabbar = TabBar(outer, [("color", "Color"), ("white", "White"), ("effects", "Effects"),
                                     ("scenes", "Scenes")], self.tab, self.show_tab)
        self.tabbar.pack(fill="x", pady=(px(14), 0))
        self.card = Card(outer, height=262)
        self.card.pack(fill="x", pady=(px(10), 0))
        self.tab_frames = {
            "color": self._build_color_tab(self.card.body),
            "white": self._build_white_tab(self.card.body),
            "effects": self._build_effects_tab(self.card.body),
            "scenes": self._build_scenes_tab(self.card.body),
        }
        self.show_tab(self.tab)

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(px(10), 0))
        self.log_toggle = tk.Label(footer, text="", font=F.small, bg=BG, fg=MUTED, cursor="hand2")
        self.log_toggle.pack(side="left")
        self.log_toggle.bind("<Button-1>", lambda e: self.toggle_log())
        hint = tk.Label(footer, text="Space on/off · 1/2/B select · ↑↓ brightness", font=F.tiny,
                        bg=BG, fg=FAINT)
        hint.pack(side="right")
        self.log_frame = tk.Frame(outer, bg=BG)
        self.log_text = tk.Text(self.log_frame, height=8, width=1, bg=SURFACE, fg=MUTED, font=F.mono, relief="flat",
                                bd=0, padx=px(10), pady=px(8), wrap="word", highlightthickness=1,
                                highlightbackground=BORDER, state="disabled", cursor="arrow")
        self.log_text.pack(fill="both", expand=True, pady=(px(6), 0))
        self.log_text.tag_configure("warn", foreground=AMBER)
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("ok", foreground=GREEN)
        self.log_text.tag_configure("time", foreground=FAINT)
        self._render_log_toggle()
        if self.log_open:
            self.log_frame.pack(fill="both", expand=True)

        self._rebuild_lamps()
        self._bind_keys()

    def _build_color_tab(self, parent):
        frame = tk.Frame(parent, bg=SURFACE)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(2, weight=1)
        self.wheel = ColorWheel(frame, 206, self._on_wheel)
        self.wheel.grid(row=0, column=0, sticky="w")
        right = tk.Frame(frame, bg=SURFACE)
        right.grid(row=0, column=2, sticky="e")
        tk.Label(right, text="Presets", font=F.small, bg=SURFACE, fg=MUTED).grid(row=0, column=0, columnspan=4,
                                                                                    sticky="w", pady=(0, px(4)))
        self.swatches = []
        for i, (name, xy) in enumerate(colors.COLOR_PRESETS):
            swatch = Swatch(right, colors.to_hex(colors.xy_to_rgb(*xy)), lambda xy=xy: self.pick_xy(xy),
                            size=42, tooltip=name)
            swatch.grid(row=1 + i // 4, column=i % 4, padx=px(1), pady=px(1))
            self.swatches.append((swatch, xy))
        PillButton(right, "Custom color…", icon=ICON_PALETTE, command=self.custom_colour, height=30,
                   font=F.small).grid(row=5, column=0, columnspan=4, sticky="ew", pady=(px(10), 0))
        return frame

    def _build_white_tab(self, parent):
        frame = tk.Frame(parent, bg=SURFACE)
        top = tk.Frame(frame, bg=SURFACE)
        top.pack(fill="x")
        tk.Label(top, text="Color temperature", font=F.body, bg=SURFACE, fg=MUTED).pack(side="left")
        self.temp_label = tk.Label(top, text="2700 K", font=F.body_bold, bg=SURFACE, fg=TEXT)
        self.temp_label.pack(side="right")
        stops = [colors.to_hex(colors.kelvin_to_rgb(k)) for k in range(colors.KELVIN_MIN, colors.KELVIN_MAX + 1, 500)]
        self.temp = Slider(frame, from_=colors.KELVIN_MIN, to=colors.KELVIN_MAX, value=2700, step=50,
                           gradient=stops, on_change=self._on_temp, height=40, track=16, knob=12)
        self.temp.pack(fill="x", pady=(px(6), px(4)))
        scale = tk.Frame(frame, bg=SURFACE)
        scale.pack(fill="x")
        tk.Label(scale, text="warm", font=F.tiny, bg=SURFACE, fg=FAINT).pack(side="left")
        tk.Label(scale, text="cool", font=F.tiny, bg=SURFACE, fg=FAINT).pack(side="right")
        grid = tk.Frame(frame, bg=SURFACE)
        grid.pack(fill="x", pady=(px(14), 0))
        grid.columnconfigure((0, 1, 2), weight=1, uniform="white")
        self.white_buttons = []
        for i, (label, kelvin) in enumerate(colors.WHITE_PRESETS):
            button = PillButton(grid, label, dot=colors.to_hex(colors.kelvin_to_rgb(kelvin)), height=36,
                                command=lambda k=kelvin: self.pick_kelvin(k), tooltip=f"{kelvin} K")
            button.grid(row=i // 3, column=i % 3, sticky="ew", padx=px(3), pady=px(3))
            self.white_buttons.append((button, kelvin))
        return frame

    def _build_effects_tab(self, parent):
        frame = tk.Frame(parent, bg=SURFACE)
        grid = tk.Frame(frame, bg=SURFACE)
        grid.pack(fill="x")
        grid.columnconfigure((0, 1, 2), weight=1, uniform="fx")
        self.effect_buttons = {}
        for i, (eid, name, _alias) in enumerate(EFFECTS):
            button = PillButton(grid, name, height=34, command=lambda e=eid: self.pick_effect(e))
            button.grid(row=i // 3, column=i % 3, sticky="ew", padx=px(3), pady=px(3))
            self.effect_buttons[eid] = button
        stop = PillButton(grid, "Stop", icon=ICON_STOP, height=34, command=lambda: self.pick_effect(0))
        stop.grid(row=3, column=1, columnspan=2, sticky="ew", padx=px(3), pady=px(3))
        speed = tk.Frame(frame, bg=SURFACE)
        speed.pack(fill="x", pady=(px(12), 0))
        tk.Label(speed, text="Speed", font=F.body, bg=SURFACE, fg=MUTED).pack(side="left")
        self.speed_label = tk.Label(speed, text="50%", font=F.body_bold, bg=SURFACE, fg=TEXT)
        self.speed_label.pack(side="right")
        self.speed = Slider(frame, from_=1, to=100, value=50, on_change=self._on_speed, height=30)
        self.speed.pack(fill="x", pady=(px(2), 0))
        return frame

    def _build_scenes_tab(self, parent):
        frame = tk.Frame(parent, bg=SURFACE)
        self.scene_canvas = tk.Canvas(frame, bg=SURFACE, highlightthickness=0, bd=0)
        self.scene_canvas.pack(fill="both", expand=True)
        self.scene_inner = tk.Frame(self.scene_canvas, bg=SURFACE)
        self._scene_win = self.scene_canvas.create_window(0, 0, window=self.scene_inner, anchor="nw")
        self.scene_canvas.bind("<Configure>", lambda e: self.scene_canvas.itemconfigure(self._scene_win, width=e.width))
        self.scene_inner.bind("<Configure>", lambda e: self.scene_canvas.configure(
            scrollregion=(0, 0, e.width, e.height)))
        for widget in (self.scene_canvas, self.scene_inner):
            widget.bind("<MouseWheel>", self._scroll_scenes)
        self._render_scenes()
        return frame

    def _scroll_scenes(self, e):
        if self.scene_inner.winfo_height() > self.scene_canvas.winfo_height():
            self.scene_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def _render_scenes(self):
        for child in self.scene_inner.winfo_children():
            child.destroy()
        grid = self.scene_inner
        grid.columnconfigure((0, 1), weight=1, uniform="scene")
        lamp_ids = [l["id"] for l in self.cfg["lamps"]] or ["L1", "L2"]
        scenes = config.all_scenes(self.cfg)
        builtin = len(config.BUILTIN_SCENES)
        for i, scene in enumerate(scenes):
            # One dot per light, in the colour that light gets.
            per_lamp = config.scene_fields(scene, lamp_ids[:2])
            dots = [SURFACE_4 if f.get("power") is False else colors.to_hex(colors.state_to_rgb(f))
                    for f in per_lamp.values()]
            button = PillButton(grid, scene["name"], dots=dots, height=36,
                                command=lambda s=scene: self.apply_scene(s))
            button.grid(row=i // 2, column=i % 2, sticky="ew", padx=px(3), pady=px(3))
            button.bind("<MouseWheel>", self._scroll_scenes)
            if i >= builtin:
                button.bind("<Button-3>", lambda e, s=scene: self._scene_menu(e, s))
                Tooltip(button, "Right-click to delete")
        add = PillButton(grid, "Save current state", icon=ICON_ADD, height=36, bg=SURFACE,
                         hover=SURFACE_3, command=self.save_scene)
        add.grid(row=len(scenes) // 2 + 1, column=0, columnspan=2, sticky="ew", padx=px(3), pady=(px(8), px(3)))
        add.bind("<MouseWheel>", self._scroll_scenes)

    def _scene_menu(self, e, scene):
        menu = tk.Menu(self.root, tearoff=0, bg=SURFACE_2, fg=TEXT, activebackground=ACCENT_DIM,
                       activeforeground=TEXT, bd=0, font=F.body)
        menu.add_command(label=f"Delete '{scene['name']}'", command=lambda: self.delete_scene(scene))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _rebuild_lamps(self):
        for child in self.tiles.winfo_children():
            child.destroy()
        self.tile_widgets.clear()
        lamps = self.cfg["lamps"]
        columns = 2 if len(lamps) != 1 else 1
        for c in range(columns):
            self.tiles.columnconfigure(c, weight=1, uniform="tiles")
        for i, lamp in enumerate(lamps):
            tile = LampTile(self.tiles, self, lamp["id"])
            tile.grid(row=i // columns, column=i % columns, sticky="ew",
                      padx=(0 if i % columns == 0 else px(5), px(5) if i % columns == 0 and columns == 2 else 0),
                      pady=(0, px(10) if i + columns < len(lamps) else 0))
            self.tile_widgets[lamp["id"]] = tile
            if lamp["id"] in self.infos:
                tile.update_info(self.infos[lamp["id"]], self._is_selected(lamp["id"]))
        options = [("all", "Both" if len(lamps) == 2 else "All")]
        options += [(lamp["id"], f"Light {i + 1}") for i, lamp in enumerate(lamps)]
        if self.target not in {k for k, _ in options}:
            self.target = "all"
        self.segment.set_options(options if len(lamps) > 1 else options[:1], self.target)
        self._refresh_status()

    def _bind_keys(self):
        def guarded(handler):
            def run(e):
                if isinstance(e.widget, tk.Entry):
                    return None
                handler()
                return "break"
            return run

        self.root.bind("<space>", guarded(self.toggle_power))
        self.root.bind("<Key-b>", guarded(lambda: self.select_target("all")))
        self.root.bind("<Key-0>", guarded(lambda: self.select_target("all")))
        for i in range(1, 10):
            self.root.bind(f"<Key-{i}>", guarded(lambda n=i: self._select_index(n)))
        self.root.bind("<Up>", guarded(lambda: self._nudge_brightness(10)))
        self.root.bind("<Down>", guarded(lambda: self._nudge_brightness(-10)))
        self.root.bind("<Control-r>", guarded(lambda: self.reconnect(None)))

    # -- engine --------------------------------------------------------------------

    def _start_engine(self):
        specs = config.lamp_specs(self.cfg)
        idle = self.cfg["settings"]["idle_release_minutes"] * 60
        keep = self.cfg["settings"]["pending_keep_minutes"] * 60
        on_change = lambda info: self.events.put(("lamp", info))  # noqa: E731

        def factory():
            transport = None
            if self.sim:
                devices = [SimDevice(address, name) for _id, name, address in specs]
                if self.args.sim_flaky:
                    transport = SimTransport(devices, latency=(0.05, 0.25), connect_delay=(0.3, 2.5),
                                             connect_fail_rate=0.35, write_fail_rate=0.05, drop_rate=0.004)
                else:
                    transport = SimTransport(devices, latency=(0.06, 0.15), connect_delay=(0.3, 1.5))
            return HueManager(specs, transport=transport, timing=Timing(), keep_connected=True,
                              idle_release=idle, pending_ttl=keep, on_change=on_change)

        async def started(manager):
            if self.sim:
                return
            self.ipc = IpcServer(manager, on_show=lambda: self.events.put(("show", None)),
                                 on_quit=lambda: self.events.put(("quit", None)), port=self.args.port)
            await self.ipc.start()

        self.engine = EngineThread(factory, started)
        self.engine.start()

    def run_engine(self, fn, on_done=None):
        """Run fn(manager) on the engine loop; on_done(future) runs on the UI thread."""
        if self.engine is None or self.engine.manager is None:
            return
        future = self.engine.call(fn, self.engine.manager)

        def finished(f):
            if on_done is not None:
                self.events.put(("call", lambda: on_done(f)))
            elif not f.cancelled() and f.exception() is not None:
                self.events.put(("call", lambda: log.error("Command failed: %s", f.exception())))

        future.add_done_callback(finished)

    def _targets(self, targets=None):
        if targets is not None:
            return targets
        return None if self.target == "all" else [self.target]

    def _target_ids(self):
        return [l["id"] for l in self.cfg["lamps"]] if self.target == "all" else [self.target]

    def _quiet(self, seconds):
        self._quiet_until = max(self._quiet_until, time.monotonic() + seconds)

    def send(self, targets=None, *, final=True, **fields):
        self._quiet(0.5 if final else 1.6)
        if final and targets is None:
            self._show_fields(fields)
        fields.setdefault("transition", self.cfg["settings"]["transition"] if final else 1)
        chosen = self._targets(targets)
        self.run_engine(lambda mgr: mgr.command(chosen, **fields))

    def _send_drag(self, **fields):
        """Rate-limit continuous input (sliders, wheel) to ~25 updates/s."""
        self._quiet(1.6)
        self._drag_fields = fields
        if self._drag_job is None:
            self._flush_drag()

    def _flush_drag(self):
        if self._drag_fields is not None:
            fields, self._drag_fields = self._drag_fields, None
            self.send(final=False, **fields)
            self._drag_job = self.root.after(40, self._flush_drag)
        else:
            self._drag_job = None

    def _send_final(self, **fields):
        if self._drag_job is not None:
            self.root.after_cancel(self._drag_job)
            self._drag_job = None
        self._drag_fields = None
        self.send(final=True, **fields)

    # -- user actions ----------------------------------------------------------------

    def select_target(self, target):
        self.target = target
        self.segment.set(target)
        for lamp_id, tile in self.tile_widgets.items():
            if lamp_id in self.infos:
                tile.update_info(self.infos[lamp_id], self._is_selected(lamp_id))
            else:
                tile.selected = self._is_selected(lamp_id)
                tile.redraw()
        self._sync_controls(force=True)

    def _select_index(self, n):
        lamps = self.cfg["lamps"]
        if 1 <= n <= len(lamps):
            self.select_target(lamps[n - 1]["id"])

    def _is_selected(self, lamp_id):
        return self.target == lamp_id

    def set_power(self, targets, on):
        self.send(targets, power=on)

    def toggle_power(self):
        ids = self._target_ids()
        # Queued choices count: pressing twice before a lamp connects turns it on, then off again.
        any_on = any(self.infos.get(i) and self.infos[i].target.power for i in ids)
        self.set_power(None, not any_on)

    def _nudge_brightness(self, delta):
        value = max(1, min(100, self.bri.value + delta))
        self.bri.set(value)
        self._on_brightness(value, True)

    def _on_brightness(self, value, final):
        self.bri_label.configure(text=f"{int(value)}%")
        fields = {"power": True, "brightness": bri_of(value)}
        self._send_final(**fields) if final else self._send_drag(**fields)

    def _on_wheel(self, xy, final):
        fields = {"power": True, "xy": xy}
        self._mark_colour(xy=xy)
        self._send_final(**fields) if final else self._send_drag(**fields)

    def pick_xy(self, xy):
        self.wheel.set_xy(xy)
        self._mark_colour(xy=xy)
        self.send(power=True, xy=xy)

    def custom_colour(self):
        state = self._reference_state()
        initial = lamp_hex(state) if state else "#8f7bff"
        chosen = colorchooser.askcolor(color=initial, parent=self.root, title="Pick a color")
        if chosen and chosen[0]:
            r, g, b = (int(v) for v in chosen[0])
            self.pick_xy(colors.rgb_to_xy(r, g, b))

    def _on_temp(self, kelvin, final):
        self.temp_label.configure(text=f"{int(kelvin)} K")
        fields = {"power": True, "mireds": colors.kelvin_to_mireds(kelvin)}
        self._mark_colour(kelvin=kelvin)
        self._send_final(**fields) if final else self._send_drag(**fields)

    def pick_kelvin(self, kelvin):
        self.temp.set(kelvin)
        self.temp_label.configure(text=f"{kelvin} K")
        self._mark_colour(kelvin=kelvin)
        self.send(power=True, mireds=colors.kelvin_to_mireds(kelvin))

    def pick_effect(self, effect_id):
        for eid, button in self.effect_buttons.items():
            button.set_selected(eid == effect_id)
        if effect_id == 0:
            self.send(effect=0)
            return
        fields = {"power": True, "effect": effect_id, "effect_speed": bri_of(self.speed.value)}
        fields.update(EFFECT_BASE.get(effect_id, {}))
        self.send(**fields)

    def _on_speed(self, value, final):
        self._quiet(1.6)
        self.speed_label.configure(text=f"{int(value)}%")
        state = self._reference_state()
        if state and state.effect and final:
            self.send(effect_speed=bri_of(value))

    def apply_scene(self, scene):
        ids = self._target_ids()
        all_ids = [l["id"] for l in self.cfg["lamps"]]
        per_lamp = config.scene_fields(scene, all_ids)
        transition = self.cfg["settings"]["transition"]
        self._quiet(0.5)
        shown = next((per_lamp[i] for i in ids if i in per_lamp), None)
        if shown:
            self._show_fields(shown)
        for lamp_id, fields in per_lamp.items():
            if lamp_id in ids:
                self.run_engine(lambda mgr, l=lamp_id, f=fields: mgr.command([l], transition=transition, **f))
        log.info("Scene '%s' applied to %s", scene["name"], "all lights" if self.target == "all" else
                 self._lamp_name(self.target))

    def save_scene(self):
        if not self.infos:
            return
        name = ask_text(self.root, "Save scene", "Scene name:", "My scene")
        if not name:
            return
        states = {lid: state_fields(info.state) for lid, info in self.infos.items()
                  if info.state.power is not None}
        for fields in states.values():
            if "xy" in fields:
                fields["xy"] = [round(fields["xy"][0], 4), round(fields["xy"][1], 4)]
        self.cfg["scenes"] = [s for s in self.cfg["scenes"] if s["name"].lower() != name.lower()]
        self.cfg["scenes"].append({"name": name, "states": states})
        self.save_config()
        self._render_scenes()
        log.info("Scene '%s' saved", name)

    def delete_scene(self, scene):
        self.cfg["scenes"] = [s for s in self.cfg["scenes"] if s is not scene]
        self.save_config()
        self._render_scenes()

    def identify(self, lamp_id):
        self.run_engine(lambda mgr: mgr.identify([lamp_id]))

    def reconnect(self, targets):
        self.run_engine(lambda mgr: mgr.reconnect(targets))
        log.info("Reconnecting…")

    def release(self, targets):
        self.run_engine(lambda mgr: mgr.set_connected(targets, False))
        log.info("Lights released; the next command reconnects")

    def rename_lamp(self, lamp_id, name=None):
        lamp = next((l for l in self.cfg["lamps"] if l["id"] == lamp_id), None)
        if lamp is None:
            return
        if name is None:
            name = ask_text(self.root, "Rename light", "New name for this light:", lamp["name"])
        name = (name or "").strip()
        if not name or name == lamp["name"]:
            return
        lamp["name"] = name
        self.save_config()
        self.run_engine(lambda mgr: mgr.rename(lamp_id, name))

    def add_lamp(self, address, name):
        lamps = self.cfg["lamps"]
        lamp_id = config.next_lamp_id(lamps)
        lamps.append({"id": lamp_id, "name": name or f"Light {len(lamps) + 1}", "address": address})
        self.save_config()
        entry = lamps[-1]
        self.run_engine(lambda mgr: mgr.add_lamp(entry["id"], entry["name"], entry["address"]))
        self._rebuild_lamps()
        self._render_scenes()
        if self.settings_window:
            self.settings_window.refresh()

    def remove_lamp(self, lamp_id) -> bool:
        lamp = next((l for l in self.cfg["lamps"] if l["id"] == lamp_id), None)
        if lamp is None:
            return False
        if not messagebox.askyesno("Remove light", f"Remove '{lamp['name']}' from this app?\n"
                                   "(It stays paired with Windows.)", parent=self.root):
            return False
        self.cfg["lamps"].remove(lamp)
        self.infos.pop(lamp_id, None)
        self.save_config()
        self.run_engine(lambda mgr: mgr.remove_lamp(lamp_id))
        self._rebuild_lamps()
        self._render_scenes()
        return True

    def set_idle_release(self, minutes):
        self.cfg["settings"]["idle_release_minutes"] = minutes
        self.save_config()

        def apply(mgr):
            mgr.idle_release = minutes * 60

        self.run_engine(apply)

    def set_transition(self, value):
        self.cfg["settings"]["transition"] = value
        self.save_config()

    def set_pending_keep(self, minutes):
        self.cfg["settings"]["pending_keep_minutes"] = minutes
        self.save_config()

        def apply(mgr):
            mgr.pending_ttl = minutes * 60

        self.run_engine(apply)

    def open_settings(self):
        if self.settings_window and self.settings_window.win.winfo_exists():
            self.settings_window.win.lift()
            return
        self.settings_window = SettingsWindow(self)

    def open_scan(self):
        ScanWindow(self)

    def open_project_page(self):
        import webbrowser

        webbrowser.open(PROJECT_URL)

    def open_logs(self):
        try:
            os.startfile(config.LOG_DIR)
        except OSError as exc:
            log.error("Cannot open the log folder: %s", exc)

    def toggle_log(self):
        self.log_open = not self.log_open
        if self.log_open:
            self.log_frame.pack(fill="both", expand=True)
        else:
            self.log_frame.pack_forget()
        self._render_log_toggle()

    def _render_log_toggle(self):
        arrow = "▾" if self.log_open else "▸"
        self.log_toggle.configure(text=f"{arrow} Activity")

    def show_tab(self, key):
        self.tab = key
        for name, frame in self.tab_frames.items():
            if name == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

    def select_tab(self, key):
        self.tabbar.value = key
        self.tabbar._draw()
        self.show_tab(key)

    # -- state -> view ----------------------------------------------------------------

    def _reference_state(self) -> LampState | None:
        states = [self.infos[i].state for i in self._target_ids()
                  if i in self.infos and self.infos[i].state.power is not None]
        if not states:
            return None
        if len(states) == 1:
            return states[0]
        lit = [s for s in states if s.power] or states
        brightness = round(sum(s.brightness or 0 for s in lit) / len(lit))
        return replace(lit[0], power=any(s.power for s in states), brightness=brightness)

    def _mark_colour(self, xy=None, kelvin=None):
        for swatch, preset in self.swatches:
            swatch.set_selected(xy is not None and abs(preset[0] - xy[0]) < 0.006 and abs(preset[1] - xy[1]) < 0.006)
        for button, preset in self.white_buttons:
            button.set_selected(kelvin is not None and abs(preset - kelvin) <= 60)

    def _show_fields(self, fields):
        """Show what was just asked for, before the lamps confirm it."""
        if "power" in fields:
            self.btn_on.set_selected(bool(fields["power"]))
            self.btn_off.set_selected(not fields["power"])
        if fields.get("brightness"):
            pct = pct_of(fields["brightness"])
            self.bri.set(pct)
            self.bri_label.configure(text=f"{pct}%")
        if fields.get("xy"):
            self.wheel.set_xy(fields["xy"])
            self._mark_colour(xy=fields["xy"])
        elif fields.get("mireds"):
            kelvin = colors.mireds_to_kelvin(fields["mireds"])
            self.temp.set(kelvin)
            self.temp_label.configure(text=f"{kelvin} K")
            self._mark_colour(kelvin=kelvin)
        if "effect" in fields:
            for eid, button in self.effect_buttons.items():
                button.set_selected(eid == fields["effect"])

    def _sync_controls(self, force=False):
        if not force and time.monotonic() < self._quiet_until:
            return
        ids = self._target_ids()
        if not force and any(self.infos.get(i) and self.infos[i].pending for i in ids):
            return
        state = self._reference_state()
        if state is None:
            return
        self.btn_on.set_selected(bool(state.power))
        self.btn_off.set_selected(state.power is False)
        if state.brightness:
            pct = pct_of(state.brightness)
            self.bri.set(pct)
            self.bri_label.configure(text=f"{pct}%")
        if state.mode == "xy" and state.xy:
            self.wheel.set_xy(state.xy)
            self._mark_colour(xy=state.xy)
        elif state.mode == "ct" and state.mireds:
            kelvin = colors.mireds_to_kelvin(state.mireds)
            self.temp.set(kelvin)
            self.temp_label.configure(text=f"{kelvin} K")
            self._mark_colour(kelvin=kelvin)
        for eid, button in self.effect_buttons.items():
            button.set_selected(eid == state.effect)
        if state.effect and state.effect_speed:
            pct = pct_of(state.effect_speed)
            self.speed.set(pct)
            self.speed_label.configure(text=f"{pct}%")

    def _lamp_name(self, lamp_id):
        lamp = next((l for l in self.cfg["lamps"] if l["id"] == lamp_id), None)
        return lamp["name"] if lamp else lamp_id

    def _refresh_status(self):
        infos = [self.infos.get(l["id"]) for l in self.cfg["lamps"]]
        total = len(infos)
        connected = sum(1 for i in infos if i and i.connected)
        if total == 0:
            self.subtitle.configure(text="No lights yet", fg=MUTED)
        elif connected == total:
            latencies = [i.latency_ms for i in infos if i and i.latency_ms is not None]
            speed = f" · response {max(latencies):.0f} ms" if latencies else ""
            self.subtitle.configure(text=f"{connected} of {total} lights connected{speed}", fg=MUTED)
        else:
            busy = [i for i in infos if i and i.status in (LampStatus.CONNECTING, LampStatus.WAITING)]
            extra = " · reconnecting…" if busy else ""
            self.subtitle.configure(text=f"{connected} of {total} lights connected{extra}",
                                    fg=AMBER if busy else MUTED)
        statuses = {i.status for i in infos if i}
        if total == 0:
            self.banner.show("none", "No lights yet. Find your Hue lights to get started.", "warn",
                             ("Find lights", self.open_scan))
        elif statuses == {LampStatus.BT_OFF}:
            self.banner.show("bt", "Bluetooth is off. Turn it on and the lights connect by themselves.",
                             "error", ("Bluetooth settings", lambda: os.startfile("ms-settings:bluetooth")))
        elif statuses == {LampStatus.NO_ADAPTER}:
            self.banner.show("adapter", "No Bluetooth adapter found. Is the USB adapter plugged in?", "error")
        elif LampStatus.NOT_PAIRED in statuses:
            names = ", ".join(i.name for i in infos if i and i.status is LampStatus.NOT_PAIRED)
            self.banner.show("pair", f"{names}: not paired with Windows (any more). Pair again with "
                                     f"'Find lights'.", "warn", ("Find lights", self.open_scan))
        else:
            self.banner.hide()
        visible = self.banner.key is not None
        if visible and not self.banner.winfo_ismapped():
            self.banner.pack(fill="x", pady=(px(10), 0), after=self.banner_anchor)
        elif not visible and self.banner.winfo_ismapped():
            self.banner.pack_forget()

    def _append_log(self, record: logging.LogRecord):
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        message = record.getMessage()
        tag = "error" if record.levelno >= logging.ERROR else "warn" if record.levelno >= logging.WARNING else ""
        if not tag and ("connected in" in message or "applied" in message):
            tag = "ok"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", stamp + "  ", ("time",))
        self.log_text.insert("end", message + "\n", (tag,) if tag else ())
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 400:
            self.log_text.delete("1.0", f"{lines - 400}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _maybe_adopt_name(self, info: LampInfo):
        lamp = next((l for l in self.cfg["lamps"] if l["id"] == info.id), None)
        if lamp and info.reported_name and lamp["name"].split(" ")[0] in ("Lamp", "Light") and lamp["name"].split(" ")[-1].isdigit():
            lamp["name"] = info.reported_name
            self.save_config()
            self.run_engine(lambda mgr: mgr.rename(info.id, info.reported_name))
            if self.settings_window:
                self.settings_window.refresh()

    def _poll(self):
        latest: dict[str, LampInfo] = {}
        try:
            for _ in range(1000):
                kind, payload = self.events.get_nowait()
                if kind == "lamp":
                    latest[payload.id] = payload
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "show":
                    self.show_window()
                elif kind == "call":
                    payload()
                elif kind == "quit":
                    self._shutdown()
                    return
        except queue.Empty:
            pass
        if latest:
            known = {l["id"] for l in self.cfg["lamps"]}
            for lamp_id, info in latest.items():
                if lamp_id not in known:
                    continue
                self.infos[lamp_id] = info
                self._maybe_adopt_name(info)
                tile = self.tile_widgets.get(lamp_id)
                if tile:
                    tile.update_info(info, self._is_selected(lamp_id))
            self._refresh_status()
            self._sync_controls()
        self._poll_job = self.root.after(40, self._poll)

    def _tick(self):
        self._blink = not self._blink
        for lamp_id, tile in self.tile_widgets.items():
            info = self.infos.get(lamp_id)
            if info and info.status in (LampStatus.CONNECTING, LampStatus.WAITING):
                tile.blink = self._blink
                tile.redraw()
        # Catch up with the lamps once the user has stopped touching the controls.
        self._sync_controls()
        self._tick_job = self.root.after(500, self._tick)

    def show_window(self):
        if self._closing:
            # Reopened while finishing in the background: become a normal window again.
            self._closing = self._background = self._started_hidden = False
            if self._close_job is not None:
                self.root.after_cancel(self._close_job)
                self._close_job = None
        if not self._ever_shown:
            restore_position(self.root, self.cfg)
        self.root.deiconify()
        if not self._ever_shown:
            dark_title_bar(self.root)
            self._ever_shown = True
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    # -- persistence / shutdown -----------------------------------------------------------

    def save_config(self):
        if self.sim:
            return
        try:
            config.save_config(self.cfg, self.args.config)
        except OSError as exc:
            log.error("Saving settings failed: %s", exc)

    def close(self):
        """Hide at once. Choices still waiting for a lamp keep the app running invisibly
        until they are applied; otherwise it stops right away."""
        if self._closing:
            return
        ui = self.cfg.setdefault("ui", {})
        ui.update({"target": self.target, "tab": self.tab, "log_open": self.log_open})
        if self._ever_shown:
            ui["position"] = [self.root.winfo_x(), self.root.winfo_y()]
        self.save_config()
        if self.settings_window is not None:
            self.settings_window.close()
        self.root.withdraw()
        self._closing = True
        self._close_started = time.monotonic()
        self._close_check()

    def start_in_background(self):
        """--background (used by hue_cli.py): run invisibly until handed-over choices are applied."""
        self._closing = self._background = self._started_hidden = True
        self._close_started = time.monotonic()
        self._close_check()

    def _waiting_lamps(self) -> list[str]:
        """Names of lamps with a choice that is not on the lamp yet (asked from the engine)."""
        if self.engine is None or self.engine.manager is None:
            return []
        try:
            ids = self.engine.call(lambda: self.engine.manager.pending_lamps()).result(2)
        except Exception:
            return []
        return [self._lamp_name(lamp_id) for lamp_id in ids]

    def _toast(self, text, **kw):
        try:
            self.toast_factory(self.root, text, on_click=self.show_window, **kw)
        except Exception:
            log.debug("showing a notification failed", exc_info=True)
            if kw.get("on_close"):
                kw["on_close"]()

    def _close_check(self):
        self._close_job = None
        if not self._closing:
            return
        waiting = self._waiting_lamps()
        elapsed = time.monotonic() - self._close_started
        if waiting:
            self._seen_waiting = True
            if not self._background and elapsed < 1.5:
                # probably just being written; give it a moment before going to the background
                self._close_job = self.root.after(150, self._close_check)
                return
            names = " and ".join(waiting)
            if not self._announced:
                self._announced = True
                self._background = True
                self._dropped_before = {i: info.dropped for i, info in self.infos.items()}
                log.info("Continuing in the background until %s is updated", names)
                one = len(waiting) == 1
                self._toast(f"Your choice for {names} is applied as soon as {'that light' if one else 'the lights'} "
                            f"{'connects' if one else 'connect'}. Click here to open the app.", duration=7000)
            if elapsed > BACKGROUND_LIMIT:
                log.warning("Stopped waiting: %s stayed unreachable", names)
                self._toast(f"Stopped: {names} could not be reached for an hour.", on_close=self._shutdown_unless_reopened)
                return
            self._close_job = self.root.after(400, self._close_check)
            return
        if self._started_hidden and not self._seen_waiting and elapsed < BACKGROUND_START_GRACE:
            self._close_job = self.root.after(300, self._close_check)   # hand-over still on its way
            return
        if self._announced:
            failed = [info.name for i, info in self.infos.items() if info.dropped > self._dropped_before.get(i, 0)]
            if failed:
                text = f"Not applied for {' and '.join(failed)}: the light was unreachable for too long."
            else:
                text = "Done: your lights are updated."
            self._toast(text, duration=3500, on_close=self._shutdown_unless_reopened)
            return
        self._shutdown()

    def _shutdown_unless_reopened(self):
        if self._closing:          # show_window() clears this when the user reopens the app
            self._shutdown()

    def _shutdown(self):
        for job in (self._poll_job, self._tick_job, self._drag_job, self._close_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        log.removeHandler(self._log_handler)
        if self.engine is not None:
            if self.ipc is not None:
                try:
                    self.engine.call(self.ipc.close).result(2)
                except Exception:
                    pass
            self.engine.stop(timeout=7.0)
        self.root.destroy()


def on_a_monitor(x: int, y: int) -> bool:
    """True if the point lies on any connected monitor (also left/above the main one)."""
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    try:
        return bool(ctypes.windll.user32.MonitorFromPoint(POINT(x, y), 0))   # MONITOR_DEFAULTTONULL
    except Exception:
        return True


def restore_position(root: tk.Tk, cfg: dict):
    position = cfg.get("ui", {}).get("position")
    root.update_idletasks()
    width, height = root.winfo_reqwidth(), root.winfo_reqheight()
    if isinstance(position, list) and len(position) == 2:
        x, y = int(position[0]), int(position[1])
        # Reuse the old spot only if the title bar is still on a connected monitor.
        if on_a_monitor(x + px(40), y + px(10)) and on_a_monitor(x + width - px(40), y + px(10)):
            root.geometry(f"+{x}+{y}")
            return
    x = (root.winfo_screenwidth() - width) // 2
    y = max(0, (root.winfo_screenheight() - height) // 2 - px(30))
    root.geometry(f"+{x}+{y}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Monitor Lights - Hue Bluetooth controller")
    parser.add_argument("--sim", action="store_true", help="demo with simulated lights")
    parser.add_argument("--sim-flaky", action="store_true", help="demo with badly behaving simulated lights")
    parser.add_argument("--background", "--achtergrond", dest="background", action="store_true",
                        help="start invisibly and quit once handed-over commands are applied")
    parser.add_argument("--port", type=int, default=IPC_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, default=config.CONFIG_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--quit", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def selftest() -> int:
    """Checks a build without showing anything: Bluetooth support loads, the
    assets are there and the interface starts and stops with demo lights.
    Exit code 0 = fine. Details go to the log."""
    import asyncio
    import importlib

    from hue_engine import BleakTransport

    for module in ("bleak", "bleak.backends.winrt.client", "bleak.backends.winrt.scanner",
                   "winrt.windows.devices.bluetooth", "winrt.windows.devices.radios"):
        importlib.import_module(module)
    log.info("selftest: Bluetooth radio is %s", asyncio.run(BleakTransport().radio_state()))
    missing = [name for name in ["app.ico"] if not (gfx.ASSETS / name).exists()]
    if missing:
        log.error("selftest: missing assets %s", missing)
        return 1
    cfg = {"lamps": [{"id": "L1", "name": "Test light 1", "address": "AA:BB:CC:00:00:01"},
                     {"id": "L2", "name": "Test light 2", "address": "AA:BB:CC:00:00:02"}],
           "settings": dict(config.DEFAULT_SETTINGS), "scenes": [], "ui": {}}
    args = parse_args(["--sim", "--port", str(IPC_PORT + 11)])
    root, app = build(args, cfg)
    result = {"connected": 0}

    def check():
        result["connected"] = sum(1 for info in app.infos.values() if info.connected)
        app.close()

    root.after(4000, check)
    root.mainloop()
    log.info("selftest: %d demo lights connected", result["connected"])
    return 0 if result["connected"] == 2 else 1


def build(args, cfg) -> tuple[tk.Tk, App]:
    """Create the (still hidden) main window and the app."""
    global SCALE, F
    enable_dpi_awareness()
    set_app_id()
    gfx.reset_tk_images()
    root = tk.Tk()
    root.withdraw()
    SCALE = root.winfo_fpixels("1i") / 96.0
    F = Fonts(root)
    root.title("Monitor Lights")
    root.configure(bg=BG)
    ico = gfx.ASSETS / "app.ico"
    try:
        if ico.exists():
            root.iconbitmap(default=str(ico))     # crisp at every size, for all windows
        else:
            root.iconphoto(True, gfx.tk_image(gfx.app_icon(64)))
    except tk.TclError:
        pass
    root.resizable(False, True)

    def report(exc_type, exc, tb):
        log.error("Error in the interface", exc_info=(exc_type, exc, tb))

    root.report_callback_exception = report
    app = App(root, cfg, args)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.minsize(px(470), px(560))
    return root, app


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.quit:                          # the installer closes a running copy this way
        return 0 if quit_controller(args.port) else 1
    if args.selftest:
        setup_logging(config.LOG_DIR / "selftest.log")
        try:
            return selftest()
        except Exception:
            log.exception("selftest failed")
            return 1
    sim = args.sim or args.sim_flaky
    if not sim and controller_running(args.port):
        if not args.background:           # an app is already running: just show it
            ipc_request({"op": "show"}, port=args.port)
        return 0

    setup_logging(config.LOG_DIR / ("controller-sim.log" if sim else "controller.log"))
    log.info("Monitor Lights %s started%s", __version__,
             " (demo)" if sim else " (background)" if args.background else "")
    cfg = config.load_config(args.config)
    try:
        root, app = build(args, cfg)
    except Exception as exc:
        log.exception("Starting failed")
        messagebox.showerror("Monitor Lights", f"Could not start:\n{exc}")
        return 1
    if args.background:
        app.start_in_background()
    else:
        app.show_window()
        if not cfg["lamps"]:
            root.after(700, app.open_scan)     # first start: help to find the lights right away
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
