"""Open the controller (demo mode by default), take screenshots of every view.

py tests/ui_shots.py [outdir] [--real] [--config file]

The window is placed off-screen and captured with PrintWindow, so only the
app itself is ever recorded (never whatever else is on the desktop) and the
person using the PC is not disturbed.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

import hue_config as config  # noqa: E402
import hue_controller as hc  # noqa: E402


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def capture(widget) -> Image.Image:
    """Render only this window (PW_RENDERFULLCONTENT), even when hidden or off-screen."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old = gdi32.SelectObject(memory_dc, bitmap)
    user32.PrintWindow(hwnd, memory_dc, 2)
    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    gdi32.SelectObject(memory_dc, old)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory_dc)
    user32.ReleaseDC(hwnd, window_dc)
    return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)


def ghost(win) -> None:
    """Make a window invisible, click-through and non-activating (it still paints)."""
    user32 = ctypes.windll.user32
    win.attributes("-alpha", 0.0)
    win.update_idletasks()
    hwnd = user32.GetParent(win.winfo_id())
    style = user32.GetWindowLongW(hwnd, -20)
    # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir", nargs="?", default="shots")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--flaky", action="store_true")
    parser.add_argument("--observe", action="store_true", help="with --real: only look, send nothing")
    parser.add_argument("--config", help="settings file to show instead of your own (e.g. for the README)")
    parser.add_argument("--readme", action="store_true",
                        help="make the README pictures (demo mode; use with --config with two lights)")
    opts = parser.parse_args()
    out = Path(opts.outdir)
    out.mkdir(parents=True, exist_ok=True)

    argv = [] if opts.real else (["--sim-flaky"] if opts.flaky else ["--sim"])
    args = hc.parse_args(argv + ["--port", str(hc.IPC_PORT + 7)] + (["--config", opts.config] if opts.config else []))

    styled = hc.style_toplevel

    def invisible(win, parent, title):         # dialogs never flash up on the desktop
        styled(win, parent, title)
        ghost(win)

    hc.style_toplevel = invisible
    hc.setup_logging(config.LOG_DIR / "ui_shots.log")
    cfg = config.load_config(args.config)
    root, app = hc.build(args, cfg)
    app.save_config = lambda: None          # never touch the real config from screenshots
    if opts.config:                         # pictures for the README: no "(demo)" in the title
        app.title_label.configure(text="Monitor Lights")
    root.geometry("+40+40")
    foreground = ctypes.windll.user32.GetForegroundWindow()
    ghost(root)
    root.deiconify()
    hc.dark_title_bar(root)
    root.update()
    ctypes.windll.user32.SetForegroundWindow(foreground)   # hand focus straight back

    shots = []

    def save(win, name):
        win.update()
        time.sleep(0.15)
        win.update()
        path = out / f"{name}.png"
        capture(win).save(path)
        shots.append(str(path))

    def shot(name):
        save(root, name)

    def other_shot(win, name):
        ghost(win)
        ctypes.windll.user32.SetForegroundWindow(foreground)
        save(win, name)

    first = cfg["lamps"][0]["id"] if cfg["lamps"] else "all"
    steps = [
        (3500, lambda: shot("1_color_both")),
        (100, lambda: (app.select_target(first), app.select_tab("white"))),
        (600, lambda: shot("2_white_light1")),
        (100, lambda: (app.select_target("all"), app.select_tab("effects"), app.pick_effect(1))),
        (1500, lambda: shot("3_effects")),
        (100, lambda: (app.pick_effect(0), app.select_tab("scenes"), app.toggle_log())),
        (1200, lambda: shot("4_scenes_log")),
        (100, lambda: (app.open_settings(), ghost(app.settings_window.win),
                       ctypes.windll.user32.SetForegroundWindow(foreground))),
        (900, lambda: other_shot(app.settings_window.win, "5_settings")),
        (100, lambda: (app.settings_window.close(), app.toggle_log(), app.select_tab("color"))),
        (100, lambda: app.set_power(None, False)),
        (1500, lambda: shot("6_off")),
        (100, lambda: app.apply_scene(config.find_scene(cfg, "Gaming"))),
        (1500, lambda: shot("7_gaming")),
    ]
    if opts.real and opts.observe:
        steps = [(9000, lambda: shot("live_observe")),
                 (100, lambda: (app.select_tab("scenes"), app.toggle_log())),
                 (500, lambda: shot("live_scenes_log")),
                 (300, app.close)]

    toasts = []

    def invisible_toast(*a, **kw):
        kw["alpha"] = 0.0
        toast = hc.Toast(*a, **kw)
        toasts.append(toast)
        return toast

    def toast_shot(name):
        if toasts and toasts[-1].win is not None:
            save(toasts[-1].win, name)

    app.toast_factory = invisible_toast
    if opts.readme:
        steps = readme_steps(app, cfg, shot, other_shot, toast_shot)
    elif opts.real and opts.observe:
        pass
    elif not opts.real:
        second = cfg["lamps"][1]["address"] if len(cfg["lamps"]) > 1 else None
        red = dict(hc.colors.COLOR_PRESETS)["Red"]

        def unplug(flag):
            def run(mgr):
                device = mgr.transport.devices[second]
                device.reachable = not flag
                if flag:
                    for link in mgr.transport.links(second):
                        link.drop()
            app.run_engine(run)

        steps += [(100, lambda: app.run_engine(lambda mgr: mgr.transport.set_radio("off"))),
                  (1500, lambda: shot("8_bluetooth_off")),
                  (100, lambda: app.run_engine(lambda mgr: mgr.transport.set_radio("on"))),
                  (2500, lambda: shot("9_back"))]
        if second:
            steps += [(100, lambda: unplug(True)),
                      (1500, lambda: (app.select_target(cfg["lamps"][1]["id"]), app.pick_xy(red))),
                      (900, lambda: shot("10_waiting_for_light")),
                      (100, app.close),                    # closes with a queued choice
                      (2300, lambda: toast_shot("11_toast_background")),
                      (100, lambda: unplug(False)),        # lamp comes back -> applied -> "Done"
                      (3000, lambda: toast_shot("12_toast_done"))]
        else:
            steps.append((300, app.close))
    else:
        steps += [(100, lambda: app.apply_scene(config.find_scene(cfg, config.DEFAULT_SCENE))),
                  (2000, lambda: shot("8_purple")),
                  (300, app.close)]

    def run(index):
        # Chain the steps: each one is scheduled only after the previous has
        # finished, so slow screenshots cannot reorder them.
        if index >= len(steps):
            return
        steps[index][1]()
        if index + 1 < len(steps):
            try:
                root.after(steps[index + 1][0], run, index + 1)
            except hc.tk.TclError:
                pass        # the app already closed itself

    root.after(steps[0][0], run, 0)
    root.mainloop()
    if opts.readme:
        shots.append(str(compose(out, ["readme_color", "readme_scenes", "readme_queued"], "screenshot")))
        for name, target in (("readme_find", "find-lights"), ("readme_toast", "notification")):
            image = rounded(Image.open(out / f"{name}.png").convert("RGBA"), 8)
            image.save(out / f"{target}.png", optimize=True)
            shots.append(str(out / f"{target}.png"))
    print("\n".join(shots))


def readme_steps(app, cfg, shot, other_shot, toast_shot):
    """Pretty states for the README: two scenes, a choice waiting for a light,
    the 'Find lights' window and the notification after closing."""
    lamps = cfg["lamps"]
    second = lamps[1]["address"]
    red = dict(hc.colors.COLOR_PRESETS)["Red"]
    scan = {}

    def unplug(flag):
        def run(mgr):
            mgr.transport.devices[second].reachable = not flag
            if flag:
                for link in mgr.transport.links(second):
                    link.drop()
        app.run_engine(run)

    def offer_new_lights(mgr):
        # Lights that are connected do not advertise, so only the new ones show up.
        for lamp in lamps:
            mgr.transport.devices[lamp["address"]].reachable = False
        for address, name in (("AA:BB:CC:00:00:03", "Hue color lamp"), ("AA:BB:CC:00:00:04", "Hue lightstrip")):
            mgr.transport.devices[address] = hc.SimDevice(address, name)

    def stop_offering(mgr):
        for lamp in lamps:
            mgr.transport.devices[lamp["address"]].reachable = True

    def open_scan():
        scan["window"] = hc.ScanWindow(app)
        ghost(scan["window"].win)

    return [
        (3500, lambda: app.apply_scene(config.find_scene(cfg, "Gaming"))),
        (1500, lambda: shot("readme_color")),
        (100, lambda: (app.apply_scene(config.find_scene(cfg, "Sunset")), app.select_tab("scenes"))),
        (1500, lambda: shot("readme_scenes")),
        (100, lambda: app.run_engine(offer_new_lights)),
        (300, open_scan),
        (900, lambda: (other_shot(scan["window"].win, "readme_find"), app.run_engine(stop_offering),
                       scan["window"].win.destroy())),
        (2000, lambda: (app.select_tab("color"), unplug(True))),
        (1500, lambda: (app.select_target(lamps[1]["id"]), app.pick_xy(red))),
        (900, lambda: shot("readme_queued")),
        (100, app.close),                          # closes with a queued choice -> notification
        (2300, lambda: toast_shot("readme_toast")),
        (100, lambda: unplug(False)),
        (3000, lambda: None),
    ]


def rounded(image: Image.Image, radius: int) -> Image.Image:
    from PIL import ImageDraw

    scale = 4
    mask = Image.new("L", (image.width * scale, image.height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1), radius * scale, fill=255)
    image.putalpha(mask.resize(image.size, Image.LANCZOS))
    return image


def compose(out: Path, names: list[str], target: str) -> Path:
    """Windows side by side with rounded corners and a soft shadow, on a
    transparent background (looks right in GitHub's light and dark theme)."""
    from PIL import ImageFilter

    images = [rounded(Image.open(out / f"{name}.png").convert("RGBA"), 8) for name in names]
    gap, pad = 36, 40
    width = sum(i.width for i in images) + gap * (len(images) - 1) + 2 * pad
    height = max(i.height for i in images) + 2 * pad
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    x = pad
    for image in images:
        shadow.paste((0, 0, 0, 110), (x, pad + 10), image.getchannel("A"))
        canvas.paste(image, (x, pad), image)
        x += image.width + gap
    result = Image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)), canvas)
    path = out / f"{target}.png"
    result.save(path, optimize=True)
    return path


if __name__ == "__main__":
    main()
