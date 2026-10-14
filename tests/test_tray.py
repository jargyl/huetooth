"""The real tray icon code (without putting an icon in the notification area) and
Start with Windows (on a throw-away registry key, never the real Run key).

Run:  py -m unittest tests.test_tray -v
"""

import ctypes
import sys
import time
import tkinter as tk
import unittest
import winreg
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hue_gfx as gfx  # noqa: E402
import hue_tray as tray  # noqa: E402

TEST_KEY = r"Software\HuetoothTest"
RUN, APPROVED = TEST_KEY + r"\Run", TEST_KEY + r"\StartupApproved"


class TrayIconTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.opened, self.actions = [], []
        menu = [("Open Huetooth", "open", "default"), ("2 of 2 lights connected", "", "disabled"), None,
                ("Scenes", [("Purple", "scene:0"), ("Focus", "scene:1")]), ("No scenes", []), ("Quit", "quit")]
        self.icon = tray.TrayIcon(gfx.ASSETS / "app.ico", "Huetooth", on_open=lambda: self.opened.append(True),
                                  menu=lambda: menu, on_action=self.actions.append)
        self.assertTrue(self.icon.start(show=False))

    def tearDown(self):
        self.icon.stop()
        self.root.destroy()

    def pump(self, until, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not until():
            self.root.update()
            time.sleep(0.01)
        return until()

    def test_clicks_arrive_through_the_tk_event_loop(self):
        # Windows posts these to the icon's window; Tk's own loop must deliver them.
        for event in (tray.NIN_SELECT, tray.NIN_KEYSELECT, tray.NIN_BALLOONUSERCLICK):
            tray.user32.PostMessageW(self.icon.hwnd, tray.WM_TRAY, 0, event | (1 << 16))
        self.assertTrue(self.pump(lambda: len(self.opened) == 3), self.opened)

    def test_menu(self):
        actions = {}
        menu = self.icon._build_menu(self.icon.menu(), actions)
        user32 = ctypes.WinDLL("user32")
        user32.GetMenuItemCount.argtypes, user32.GetMenuItemCount.restype = [wintypes.HMENU], ctypes.c_int
        try:
            self.assertEqual(user32.GetMenuItemCount(menu), 6)
            self.assertEqual(sorted(actions.values()), ["", "open", "quit", "scene:0", "scene:1"])
        finally:
            tray.user32.DestroyMenu(menu)

    def test_without_an_icon_nothing_is_shown(self):
        self.assertFalse(self.icon.visible)
        self.assertFalse(self.icon.notify("Huetooth", "hello"))
        self.icon.set_tooltip("Huetooth\n1 of 2 lights connected")
        self.assertEqual(self.icon.tooltip, "Huetooth\n1 of 2 lights connected")

    def test_stop_twice_is_fine(self):
        self.icon.stop()
        self.icon.stop()
        self.assertIsNone(self.icon.hwnd)


class AutostartTests(unittest.TestCase):
    def tearDown(self):
        for key in (RUN, APPROVED, TEST_KEY):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass

    def test_on_and_off(self):
        self.assertFalse(tray.autostart_enabled(RUN, APPROVED))
        tray.set_autostart(True, RUN, APPROVED)
        self.assertTrue(tray.autostart_enabled(RUN, APPROVED))
        self.assertEqual(tray._read(RUN, tray.RUN_VALUE), tray.autostart_command())
        tray.set_autostart(False, RUN, APPROVED)
        self.assertFalse(tray.autostart_enabled(RUN, APPROVED))
        self.assertIsNone(tray._read(RUN, tray.RUN_VALUE))

    def test_switched_off_in_windows_settings(self):
        tray.set_autostart(True, RUN, APPROVED)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APPROVED) as handle:   # what Settings > Startup writes
            winreg.SetValueEx(handle, tray.RUN_VALUE, 0, winreg.REG_BINARY, bytes([3]) + bytes(11))
        self.assertFalse(tray.autostart_enabled(RUN, APPROVED))
        tray.set_autostart(True, RUN, APPROVED)            # switching it on in Huetooth wins
        self.assertTrue(tray.autostart_enabled(RUN, APPROVED))

    def test_follows_a_moved_program(self):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN) as handle:
            winreg.SetValueEx(handle, tray.RUN_VALUE, 0, winreg.REG_SZ, r'"C:\old place\Huetooth.exe" --tray')
        tray.refresh_autostart(RUN, APPROVED)
        self.assertEqual(tray._read(RUN, tray.RUN_VALUE), tray.autostart_command())

    def test_command_starts_hidden_in_the_tray(self):
        command = tray.autostart_command()
        self.assertTrue(command.endswith(" --tray"))
        self.assertIn("Huetooth.pyw", command)
        self.assertIn("pythonw.exe", command.lower())


class BusyTests(unittest.TestCase):
    def test_user_busy_is_a_yes_or_no(self):
        self.assertIn(tray.user_busy(), (True, False))


if __name__ == "__main__":
    unittest.main()
