"""Drives the real Tk interface (demo lamps) with synthetic mouse/keyboard events.

The window is invisible, click-through and never takes focus.
Run:  py -m unittest tests.test_ui -v
"""

import ctypes
import logging
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hue_colors as colors  # noqa: E402
import hue_config as config  # noqa: E402
import hue_controller as hc  # noqa: E402
from hue_engine import LampStatus  # noqa: E402

LAMPS = [{"id": "L1", "name": "Left", "address": "AA:BB:CC:00:00:01"},
         {"id": "L2", "name": "Right", "address": "AA:BB:CC:00:00:02"}]


def ghost(win):
    user32 = ctypes.windll.user32
    win.attributes("-alpha", 0.0)
    win.update_idletasks()
    hwnd = user32.GetParent(win.winfo_id())
    style = user32.GetWindowLongW(hwnd, -20)
    user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)


class FakeTray:
    """Stands in for the tray icon (no icon appears during tests) and records
    what the app shows there; tests call on_open / on_action like a click would."""

    def __init__(self, icon_path, tooltip, on_open, menu, on_action):
        self.tooltip, self.on_open, self.menu, self.on_action = tooltip, on_open, menu, on_action
        self.visible, self.hwnd, self.notes, self.stopped = False, 1, [], False

    def start(self, show=True):
        self.visible = show
        return True

    def add(self):
        self.visible = True
        return True

    def set_tooltip(self, text):
        self.tooltip = text

    def notify(self, title, text):
        self.notes.append(text)
        return True

    def stop(self):
        self.visible, self.stopped = False, True


class UiTest(unittest.TestCase):
    def setUp(self):
        # Every dialog the app opens is made invisible straight away.
        self._style = hc.style_toplevel
        self._tray_factory = hc.TRAY_FACTORY
        hc.TRAY_FACTORY = FakeTray
        # Whatever runs on this PC right now (a full-screen game...) must not change the results.
        self._user_busy = hc.tray.user_busy
        hc.tray.user_busy = lambda: False

        def invisible(win, parent, title):
            self._style(win, parent, title)
            ghost(win)

        hc.style_toplevel = invisible
        self.errors = []
        handler = logging.Handler(level=logging.ERROR)
        handler.emit = lambda record: self.errors.append(record.getMessage())
        self.handler = handler
        logging.getLogger("hue").addHandler(handler)
        cfg = {"lamps": [dict(l) for l in LAMPS], "settings": dict(config.DEFAULT_SETTINGS),
               "scenes": [], "ui": {}}
        args = hc.parse_args(["--sim", "--port", str(hc.IPC_PORT + 9)])
        self.foreground = foreground = ctypes.windll.user32.GetForegroundWindow()
        self.root, self.app = hc.build(args, cfg)
        self.app.save_config = lambda: None
        self.root.geometry("+40+40")
        ghost(self.root)
        self.root.deiconify()
        self.root.update()
        ctypes.windll.user32.SetForegroundWindow(foreground)
        self.devices = self.app.engine.manager.transport.devices

    def tearDown(self):
        hc.style_toplevel = self._style
        hc.TRAY_FACTORY = self._tray_factory
        hc.tray.user_busy = self._user_busy
        logging.getLogger("hue").removeHandler(self.handler)
        try:
            self.app.quit()
        except Exception:
            pass

    def answer_dialog(self, text, delay=300):
        """Type *text* into the next ask_text dialog and click OK.

        (A mouse click, not Enter: key events need keyboard focus, which the
        invisible test windows never take.) Gives up - closing every dialog -
        after 10 s so a broken dialog can never hang the test run.
        """
        deadline = time.monotonic() + 10

        def fill():
            for win in self.root.winfo_children():
                if not isinstance(win, hc.tk.Toplevel) or not win.winfo_children():
                    continue
                body = win.winfo_children()[0]
                entries = [w for w in body.winfo_children() if isinstance(w, hc.tk.Entry)]
                buttons = [b for row in body.winfo_children() for b in row.winfo_children()
                           if isinstance(b, hc.PillButton) and b.text == "OK"]
                if entries and buttons:
                    entries[0].delete(0, "end")
                    entries[0].insert(0, text)
                    self.click(buttons[0])
                    return
            if time.monotonic() > deadline:
                for win in self.root.winfo_children():
                    if isinstance(win, hc.tk.Toplevel):
                        win.destroy()
                return
            self.root.after(100, fill)

        self.root.after(delay, fill)

    # -- helpers ---------------------------------------------------------------

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def wait(self, predicate, timeout=4.0, what=""):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timeout waiting for {what}")

    def dev(self, index):
        return self.devices[LAMPS[index]["address"]]

    def click(self, widget, x=None, y=None):
        widget.update_idletasks()
        x = widget.winfo_width() // 2 if x is None else x
        y = widget.winfo_height() // 2 if y is None else y
        widget.event_generate("<Enter>", x=x, y=y)
        widget.event_generate("<ButtonPress-1>", x=x, y=y)
        widget.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.root.update()

    def drag(self, slider, fraction):
        slider.update_idletasks()
        y = slider.winfo_height() // 2
        x0 = slider.x0
        x1 = slider.x0 + fraction * (slider.x1 - slider.x0)
        slider.event_generate("<ButtonPress-1>", x=int(x0), y=y)
        for i in range(1, 11):
            slider.event_generate("<B1-Motion>", x=int(x0 + (x1 - x0) * i / 10), y=y)
            self.pump(0.02)
        slider.event_generate("<ButtonRelease-1>", x=int(x1), y=y)
        self.root.update()

    def segment_click(self, index):
        seg = self.app.segment
        width = seg.winfo_width()
        self.click(seg, x=int(width * (index + 0.5) / len(seg.options)))

    def tab(self, key):
        bar = self.app.tabbar
        bar.update_idletasks()
        x0, x1, _ = next(b for b in bar._boxes if b[2] == key)
        self.click(bar, x=int((x0 + x1) / 2))

    # -- the test ----------------------------------------------------------------

    def test_full_walkthrough(self):
        app = self.app
        self.wait(lambda: all(i.connected for i in app.infos.values()) and len(app.infos) == 2,
                  timeout=10, what="both demo lamps connected")
        self.assertIn("2 of 2", app.subtitle.cget("text"))

        # Lamp 1 only: red swatch must not touch lamp 2.
        self.segment_click(1)
        self.assertEqual(app.target, "L1")
        red = dict(colors.COLOR_PRESETS)["Red"]
        swatch = next(s for s, xy in app.swatches if xy == red)
        self.click(swatch)
        self.wait(lambda: self.dev(0).state["xy"] and abs(self.dev(0).state["xy"][0] - red[0]) < 0.01,
                  what="lamp 1 red")
        self.assertAlmostEqual(self.dev(1).state["xy"][0], 0.3, places=2)

        # Both: brightness drag to ~25 %.
        self.segment_click(0)
        self.assertEqual(app.target, "all")
        self.drag(app.bri, 0.25)
        expected = hc.bri_of(app.bri.value)
        self.wait(lambda: self.dev(0).state["brightness"] == expected == self.dev(1).state["brightness"],
                  what="brightness on both")
        self.assertLess(abs(app.bri.value - 25), 3)

        # Off / on buttons.
        self.click(app.btn_off)
        self.wait(lambda: not self.dev(0).state["power"] and not self.dev(1).state["power"], what="off")
        self.wait(lambda: app.tile_widgets["L1"].info.state.power is False, what="tile shows off")
        self.click(app.btn_on)
        self.wait(lambda: self.dev(0).state["power"] and self.dev(1).state["power"], what="on")

        # Effects tab: candle, then stop.
        self.tab("effects")
        self.assertEqual(app.tab, "effects")
        self.click(app.effect_buttons[1])
        self.wait(lambda: self.dev(0).state["effect"] == 1 == self.dev(1).state["effect"], what="candle")
        stop = [w for w in app.effect_buttons[1].master.winfo_children()
                if isinstance(w, hc.PillButton) and w.text == "Stop"][0]
        self.click(stop)
        self.wait(lambda: self.dev(0).state["effect"] == 0 == self.dev(1).state["effect"], what="effect off")

        # White tab: cool preset.
        self.tab("white")
        cool = next(b for b, k in app.white_buttons if k == 6500)
        self.click(cool)
        self.wait(lambda: self.dev(0).state["mireds"] == colors.kelvin_to_mireds(6500), what="cool white")

        # Scenes tab: two-tone scene.
        self.tab("scenes")
        gamen = [w for w in app.scene_inner.winfo_children()
                 if isinstance(w, hc.PillButton) and w.text == "Gaming"][0]
        self.click(gamen)
        self.wait(lambda: self.dev(0).state["xy"] and abs(self.dev(0).state["xy"][0] - 0.16) < 0.01
                  and self.dev(1).state["xy"] and abs(self.dev(1).state["xy"][0] - 0.39) < 0.01,
                  what="scene Gaming")
        self.pump(0.7)
        self.assertTrue(app.btn_on.selected)

        # Keyboard shortcuts are bound on the main window and do what they promise.
        # (Their handlers are called directly: synthetic key events need keyboard
        # focus, which this invisible test window deliberately never takes.)
        bound = self.root.bind()
        for sequence in ("<Key-space>", "b", "2", "<Key-Up>", "<Key-Down>", "<Control-Key-r>"):
            self.assertIn(sequence, bound)
        app.toggle_power()
        self.wait(lambda: not self.dev(0).state["power"] and not self.dev(1).state["power"], what="space off")
        self.wait(lambda: not app.tile_widgets["L2"].toggle.value, what="tile switch shows off")
        app._select_index(2)
        self.assertEqual(app.target, "L2")
        app.select_target("all")
        self.assertEqual(app.target, "all")

        # Per-lamp switch on the tile.
        tile = app.tile_widgets["L2"]
        self.click(tile.toggle)
        self.wait(lambda: self.dev(1).state["power"] is True, what="tile switch on")
        self.assertFalse(self.dev(0).state["power"])

        # Settings window opens and closes cleanly.
        app.open_settings()
        ghost(app.settings_window.win)
        self.pump(0.3)
        app.settings_window.close()

        # Bluetooth off -> banner + status; back on -> reconnects.
        app.run_engine(lambda mgr: mgr.transport.set_radio("off"))
        self.wait(lambda: app.banner.key == "bt", what="bluetooth banner")
        self.assertTrue(all(i.status is LampStatus.BT_OFF for i in app.infos.values()))
        app.run_engine(lambda mgr: mgr.transport.set_radio("on"))
        self.wait(lambda: all(i.connected for i in app.infos.values()), what="reconnect after BT on")
        self.wait(lambda: app.banner.key is None, what="banner hidden")

        # A command while a lamp is unreachable waits and is applied when it returns.
        self.dev(0).reachable = False
        app.run_engine(lambda mgr: [link.drop() for link in mgr.transport.links(LAMPS[0]["address"])])
        self.wait(lambda: not app.infos["L1"].connected, what="lamp 1 lost")
        self.segment_click(1)
        self.click(app.btn_on)
        self.wait(lambda: app.infos["L1"].pending, what="pending badge")
        self.dev(0).reachable = True
        self.wait(lambda: self.dev(0).state["power"] is True and not app.infos["L1"].pending,
                  timeout=8, what="queued command applied after reconnect")

        self.assertEqual(self.errors, [])

    def test_close_with_queued_choice_finishes_in_background(self):
        app = self.app
        app.cfg["settings"]["close_to_tray"] = False     # "When I close the window: Quit Huetooth"
        toasts = []

        class FakeToast:
            def __init__(self, root, text, *, on_close=None, on_click=None, duration=0, **_kw):
                toasts.append(text)
                if on_close:
                    root.after(50, on_close)

        app.toast_factory = FakeToast
        self.wait(lambda: len(app.infos) == 2 and all(i.connected for i in app.infos.values()),
                  timeout=10, what="connected")
        address = LAMPS[1]["address"]
        self.dev(1).reachable = False
        app.run_engine(lambda mgr: [link.drop() for link in mgr.transport.links(address)])
        self.wait(lambda: not app.infos["L2"].connected, what="lamp 2 gone")

        # Pick a colour for the unreachable lamp: the tile shows it as queued.
        app.select_target("L2")
        red = dict(colors.COLOR_PRESETS)["Red"]
        self.click(next(s for s, xy in app.swatches if xy == red))
        self.wait(lambda: app.infos["L2"].pending and app.infos["L2"].pending_state.xy is not None,
                  what="queued choice")
        self.assertAlmostEqual(app.infos["L2"].target.xy[0], red[0], places=2)

        app.close()                      # window gone, but the choice is still waiting
        self.pump(2.5)
        self.assertTrue(self.root.winfo_exists())
        self.assertFalse(self.root.winfo_viewable())
        self.assertTrue(any("is applied as soon as" in t for t in toasts), toasts)

        self.dev(1).reachable = True     # the lamp comes back: applied, then the app stops by itself
        destroyed = False
        end = time.monotonic() + 15
        while time.monotonic() < end:
            try:
                self.root.update()
                self.root.winfo_exists()      # raises once the app destroyed its window
            except hc.tk.TclError:
                destroyed = True
                break
            time.sleep(0.02)
        self.assertTrue(destroyed, "app did not stop after applying the queued choice")
        self.assertFalse(self.app.engine._thread.is_alive())
        self.assertTrue(any(t.startswith("Done") for t in toasts), toasts)
        self.assertAlmostEqual(self.dev(1).state["xy"][0], red[0], places=2)
        self.assertEqual(self.errors, [])

    def test_no_popups_during_full_screen_games(self):
        app = self.app
        app._quit_when_done = True                   # (pop-ups are only used when the app quits after)
        shown, after = [], []
        app.toast_factory = lambda *a, **kw: shown.append(a)
        hc.tray.user_busy = lambda: True
        app._notify("Done: your lights are updated.", then=lambda: after.append(True))
        self.assertEqual(shown, [])                  # held back...
        self.assertEqual(after, [True])              # ...but what comes next still happens
        hc.tray.user_busy = lambda: False
        app._notify("Done: your lights are updated.")
        self.assertEqual(len(shown), 1)

    def test_tray_close_menu_and_quit(self):
        app = self.app
        tray = app.tray
        self.assertTrue(app.tray_ready)
        self.wait(lambda: len(app.infos) == 2 and all(i.connected for i in app.infos.values()),
                  timeout=10, what="connected")
        self.wait(lambda: "2 of 2 lights connected" in tray.tooltip, what="tooltip")

        # The close button hides Huetooth in the tray: still running and connected.
        app.close()
        self.pump(0.5)
        self.assertTrue(self.root.winfo_exists())
        self.assertFalse(self.root.winfo_viewable())
        self.assertFalse(app._visible())
        self.assertTrue(app.engine._thread.is_alive())
        self.assertEqual(len(tray.notes), 1, tray.notes)               # the one-time hint
        self.assertIn("Right-click", tray.notes[0])
        self.assertTrue(app.cfg["ui"]["tray_hint_shown"])

        # The right-click menu works while the window is hidden.
        labels = [item[0] for item in tray.menu() if item]
        self.assertEqual(labels[0], "Open Huetooth")
        self.assertIn("2 of 2 lights connected", labels)
        scenes = next(item[1] for item in tray.menu() if item and item[0] == "Scenes")
        tray.on_action("off")
        self.wait(lambda: not self.dev(0).state["power"] and not self.dev(1).state["power"], what="all off")
        gaming = next(key for name, key in scenes if name == "Gaming")
        tray.on_action(gaming)
        self.wait(lambda: self.dev(0).state["power"] and abs(self.dev(0).state["xy"][0] - 0.16) < 0.01
                  and abs(self.dev(1).state["xy"][0] - 0.39) < 0.01, what="scene from the tray")

        # A click on the icon brings the window back.
        tray.on_open()
        ctypes.windll.user32.SetForegroundWindow(self.foreground)    # (it is invisible: give focus back)
        self.pump(0.3)
        self.assertTrue(app._visible())

        # Closing with a choice that still waits: Windows notifications, and Huetooth stays.
        address = LAMPS[1]["address"]
        self.dev(1).reachable = False
        app.run_engine(lambda mgr: [link.drop() for link in mgr.transport.links(address)])
        self.wait(lambda: not app.infos["L2"].connected, what="lamp 2 gone")
        app.select_target("L2")
        red = dict(colors.COLOR_PRESETS)["Red"]
        self.click(next(s for s, xy in app.swatches if xy == red))
        self.wait(lambda: app.infos["L2"].pending, what="queued choice")
        app.close()
        self.wait(lambda: any("is applied as soon as" in t for t in tray.notes), timeout=4, what="queued note")
        self.dev(1).reachable = True
        self.wait(lambda: any(t.startswith("Done") for t in tray.notes), timeout=15, what="done note")
        self.assertTrue(self.root.winfo_exists())
        self.assertFalse(app._closing)
        self.assertAlmostEqual(self.dev(1).state["xy"][0], red[0], places=2)

        # "Quit Huetooth" in the menu really stops it.
        tray.on_action("quit")
        with self.assertRaises(hc.tk.TclError):
            self.root.winfo_exists()
        self.assertTrue(tray.stopped)
        self.assertFalse(app.engine._thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_many_lights(self):
        """Six lights: compact tiles, a light menu instead of tabs, and it still fits."""
        app = self.app
        transport = app.engine.manager.transport
        for n in range(3, 7):
            address = f"AA:BB:CC:00:00:{n:02X}"
            transport.devices[address] = hc.SimDevice(address, f"Hue light {n}")
            app.add_lamp(address, f"Extra {n}")
        self.wait(lambda: len(app.infos) == 6 and all(i.connected for i in app.infos.values()),
                  timeout=15, what="six lights connected")
        self.assertTrue(all(tile.compact for tile in app.tile_widgets.values()))
        self.root.update_idletasks()
        self.assertFalse(app.segment.winfo_ismapped())
        self.assertTrue(app.picker.winfo_ismapped())
        self.assertIn("1–6/B", app.hint.cget("text"))
        self.assertLess(self.root.winfo_reqheight(), 950)              # fits a 1080p screen

        app._select_index(5)                                          # key 5
        self.assertEqual(app.picker.text, "Extra 5  ▾")
        self.click(app.tile_widgets["L6"])                            # or click a tile
        self.assertEqual(app.target, "L6")
        app.select_target("all")
        self.assertTrue(app.picker.text.startswith("All lights"))

        app.apply_scene(config.find_scene(app.cfg, "Focus"))          # scenes reach every light
        self.wait(lambda: all(transport.devices[l["address"]].state["mireds"] == 233 for l in app.cfg["lamps"]),
                  what="scene on all six")
        ids = [l["id"] for l in app.cfg["lamps"]]
        self.assertEqual(app.describe_lights(ids), "all lights")
        self.assertEqual(app.describe_lights(ids[:4]), "4 lights")
        self.assertEqual(app.describe_lights(ids[:2]), "Left and Right")

        # Back to two lights: tabs again ("Both", "Light 1", "Light 2").
        for lamp_id in ids[2:]:
            original = hc.messagebox.askyesno
            hc.messagebox.askyesno = lambda *a, **kw: True
            try:
                app.remove_lamp(lamp_id)
            finally:
                hc.messagebox.askyesno = original
        self.root.update_idletasks()
        self.assertTrue(app.segment.winfo_ismapped())
        self.assertEqual([label for _, label in app.segment.options], ["Both", "Light 1", "Light 2"])
        self.assertFalse(any(tile.compact for tile in app.tile_widgets.values()))
        self.assertEqual(self.errors, [])

    def test_delete_and_restore_scenes(self):
        app = self.app
        self.tab("scenes")
        self.click(app.edit_button)                                   # Edit: every scene gets a delete button
        self.assertTrue(app.scene_edit)
        movie = next(b for b in app.scene_buttons if b.text == "Movie")
        self.click(movie)                                             # a built-in scene: hidden, no question
        self.assertEqual(app.cfg["hidden_scenes"], ["Movie"])
        self.assertNotIn("Movie", [b.text for b in app.scene_buttons])
        tray_scenes = next(item[1] for item in app._tray_menu() if item and item[0] == "Scenes")
        self.assertNotIn("Movie", [name for name, _ in tray_scenes])

        # One of your own scenes is confirmed first, then gone for good.
        app.cfg["scenes"].append({"name": "Mine", "states": [{"power": True, "brightness": 100}]})
        app._render_scenes()
        asked = []
        original = hc.messagebox.askyesno
        hc.messagebox.askyesno = lambda *a, **kw: asked.append(a) or True
        try:
            self.click(next(b for b in app.scene_buttons if b.text == "Mine"))
        finally:
            hc.messagebox.askyesno = original
        self.assertEqual(len(asked), 1)
        self.assertEqual(app.cfg["scenes"], [])

        self.click(app.restore_button)                                # built-ins come back
        self.assertEqual(app.cfg["hidden_scenes"], [])
        self.assertIn("Movie", [b.text for b in app.scene_buttons])
        self.click(app.edit_button)                                   # Done
        self.assertFalse(app.scene_edit)
        self.click(next(b for b in app.scene_buttons if b.text == "Movie"))   # applies again
        self.wait(lambda: self.dev(0).state["mireds"] == 455, what="Movie scene applied")
        self.assertEqual(self.errors, [])

    def fill_schedule_editor(self, fill):
        """Run *fill(editor)* on the next schedule editor (it is modal), then click Save."""
        deadline = time.monotonic() + 10

        def attempt():
            editor = getattr(self.app, "schedule_editor", None)
            if editor is None:
                if time.monotonic() < deadline:
                    self.root.after(50, attempt)
                return
            fill(editor)
            self.click(editor.save_button)

        self.root.after(100, attempt)

    def test_schedule_runs_catches_up_and_respects_changes(self):
        app = self.app
        red = dict(colors.COLOR_PRESETS)["Red"]
        blue = dict(colors.COLOR_PRESETS)["Blue"]
        clock = {"now": hc.datetime(2026, 10, 14, 21, 0)}
        app.now = lambda: clock["now"]
        self.wait(lambda: len(app.infos) == 2 and all(i.connected for i in app.infos.values()),
                  timeout=10, what="connected")

        def tick(when):
            clock["now"] = when
            app._schedule_tick()
            self.pump(0.6)

        # Create "22:00 every day, red, catch up until 04:00" in the editor.
        app.open_schedule()
        window = app.schedule_window

        def make_red(editor):
            editor.time_entry.delete(0, "end")
            editor.time_entry.insert(0, "22.00")
            editor._pick_xy(red)

        self.fill_schedule_editor(make_red)
        window.add()
        rule = app.cfg["schedules"][0]
        self.assertEqual((rule["time"], rule["until"], rule["lights"], rule["action"]["type"]),
                         ("22:00", "04:00", "all", "color"))
        self.assertAlmostEqual(self.dev(0).state["xy"][0], 0.3, places=2)    # 21:00: nothing yet

        tick(hc.datetime(2026, 10, 14, 22, 0, 5))                           # its time comes while running
        self.wait(lambda: abs(self.dev(0).state["xy"][0] - red[0]) < 0.01 and
                  abs(self.dev(1).state["xy"][0] - red[0]) < 0.01, what="red at 22:00")
        self.assertEqual(rule["last_run"], "2026-10-14T22:00")

        app.run_engine(lambda mgr: mgr.command(None, xy=blue))              # changed by hand afterwards...
        self.wait(lambda: abs(self.dev(0).state["xy"][0] - blue[0]) < 0.01, what="blue by hand")
        tick(hc.datetime(2026, 10, 14, 22, 30))                             # ...and that sticks
        self.assertAlmostEqual(self.dev(0).state["xy"][0], blue[0], places=2)

        tick(hc.datetime(2026, 10, 15, 23, 30))   # next evening the app was closed at 22:00: catches up
        self.wait(lambda: abs(self.dev(0).state["xy"][0] - red[0]) < 0.01, what="caught up")
        app.run_engine(lambda mgr: mgr.command(None, xy=blue))
        self.wait(lambda: abs(self.dev(0).state["xy"][0] - blue[0]) < 0.01, what="blue again")
        tick(hc.datetime(2026, 10, 17, 5, 0))                               # opened after 'until': nothing
        self.assertAlmostEqual(self.dev(0).state["xy"][0], blue[0], places=2)

        # Change it: only light 2, turn off.
        def make_off(editor):
            editor._set_lights("L2")
            editor._set_kind("off")

        self.fill_schedule_editor(make_off)
        window.edit(rule)
        self.assertEqual((rule["lights"], rule["action"]), (["L2"], {"type": "off"}))
        tick(hc.datetime(2026, 10, 17, 22, 0, 2))
        self.wait(lambda: self.dev(1).state["power"] is False, what="light 2 off")
        self.assertTrue(self.dev(0).state["power"])

        window._enable(rule, False)                                         # switched off: nothing happens
        self.dev(1).state["power"] = True
        tick(hc.datetime(2026, 10, 18, 22, 0, 2))
        self.assertTrue(self.dev(1).state["power"])
        window._delete(rule)
        self.assertEqual(app.cfg["schedules"], [])
        window.close()
        self.assertEqual(self.errors, [])

    def test_dialogs_scenes_and_lamp_management(self):
        app = self.app
        self.wait(lambda: len(app.infos) == 2 and all(i.connected for i in app.infos.values()),
                  timeout=10, what="connected")
        transport = app.engine.manager.transport

        # Give the lamps distinct states, then save them as a scene via the name dialog.
        app.run_engine(lambda mgr: mgr.command(["L1"], power=True, brightness=40, xy=(0.6, 0.38)))
        app.run_engine(lambda mgr: mgr.command(["L2"], power=True, brightness=220, mireds=300))
        self.wait(lambda: app.infos["L1"].state.brightness == 40 and app.infos["L2"].state.mireds == 300,
                  what="distinct states")
        self.answer_dialog("Café scene")
        app.save_scene()
        self.assertEqual([s["name"] for s in app.cfg["scenes"]], ["Café scene"])
        buttons = [w.text for w in app.scene_inner.winfo_children() if isinstance(w, hc.PillButton)]
        self.assertIn("Café scene", buttons)

        # Change everything, then apply the saved scene: each lamp gets its own state back.
        app.run_engine(lambda mgr: mgr.command(None, power=False))
        self.wait(lambda: not self.dev(0).state["power"] and not self.dev(1).state["power"], what="off")
        app.select_target("all")
        app.apply_scene(app.cfg["scenes"][0])
        self.wait(lambda: self.dev(0).state["brightness"] == 40 and self.dev(1).state["mireds"] == 300
                  and self.dev(0).state["power"] and self.dev(1).state["power"], what="scene restored")

        app.delete_scene(app.cfg["scenes"][0], ask=False)
        self.assertEqual(app.cfg["scenes"], [])

        # Rename a lamp through the dialog.
        self.answer_dialog("Desk left")
        app.rename_lamp("L1")
        self.assertEqual(app.cfg["lamps"][0]["name"], "Desk left")
        self.wait(lambda: app.infos["L1"].name == "Desk left", what="engine rename")
        self.assertEqual(app.tile_widgets["L1"].info.name, "Desk left")

        # Scan finds the known lamps plus a new one, which can be added.
        new_address = "AA:BB:CC:00:00:03"
        transport.devices[new_address] = hc.SimDevice(new_address, "New light")
        transport.devices[new_address].paired = False
        app.open_scan()
        scan = next(w for w in self.root.winfo_children() if isinstance(w, hc.tk.Toplevel))
        self.wait(lambda: any(isinstance(w, hc.tk.Frame) for w in scan.winfo_children()[0].winfo_children()[2]
                              .winfo_children()), what="scan results")
        rows = scan.winfo_children()[0].winfo_children()[2].winfo_children()
        add = [w for row in rows for w in row.winfo_children() if isinstance(w, hc.PillButton)]
        self.assertEqual(len(add), 1)            # only the new lamp has an "add" button
        self.click(add[0])
        self.wait(lambda: len(app.cfg["lamps"]) == 3, what="lamp added")
        self.assertEqual(app.cfg["lamps"][2]["name"], "New light")
        self.wait(lambda: "L3" in app.infos and app.infos["L3"].connected, what="new lamp connected")
        self.assertEqual(len(app.tile_widgets), 3)
        self.assertEqual(len(app.segment.options), 4)    # Alle + 3 lamps
        scan.destroy()

        # Remove it again (confirmation answered with yes).
        original = hc.messagebox.askyesno
        hc.messagebox.askyesno = lambda *a, **k: True
        try:
            self.assertTrue(app.remove_lamp("L3"))
        finally:
            hc.messagebox.askyesno = original
        self.assertEqual(len(app.cfg["lamps"]), 2)
        self.wait(lambda: "L3" not in app.engine.manager.lamps, what="engine removed lamp")
        self.assertEqual(len(app.tile_widgets), 2)

        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
