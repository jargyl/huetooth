"""Huetooth in the Windows notification area ("tray"), straight on the Win32 API.

* TrayIcon: the icon with its tooltip, click and right-click menu, and Windows
  notifications. Its hidden window is created on the Tk thread: Tk's event loop
  hands Windows messages to every window of its thread, so clicks reach the
  app at once and callbacks may use Tk directly.
* user_busy(): a game or presentation is running full screen.
* Start with Windows: a value under HKCU\\...\\CurrentVersion\\Run.
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import sys
import winreg
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Union

log = logging.getLogger("hue")

# Own DLL handles, so these argtypes never leak into other ctypes.windll users.
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = wintypes.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_NULL, WM_DESTROY, WM_CONTEXTMENU, WM_APP = 0x0000, 0x0002, 0x007B, 0x8000
WM_TRAY = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x10, 0x80
NOTIFYICON_VERSION_4 = 4
NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK = 0x0400, 0x0401, 0x0405
NIIF_USER, NIIF_LARGE_ICON, NIIF_RESPECT_QUIET_TIME = 0x04, 0x20, 0x80
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXICON, SM_CXSMICON = 11, 49
MF_STRING, MF_GRAYED, MF_CHECKED, MF_POPUP, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x10, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),   # union with uTimeout
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


def _signatures() -> None:
    S = [
        (user32.RegisterClassExW, [ctypes.POINTER(WNDCLASSEXW)], wintypes.ATOM),
        (user32.UnregisterClassW, [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
        (user32.CreateWindowExW, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND),
        (user32.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
        (user32.DefWindowProcW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], LRESULT),
        (user32.RegisterWindowMessageW, [wintypes.LPCWSTR], wintypes.UINT),
        (user32.LoadImageW, [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                             wintypes.UINT], wintypes.HANDLE),
        (user32.DestroyIcon, [wintypes.HICON], wintypes.BOOL),
        (user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int),
        (user32.CreatePopupMenu, [], wintypes.HMENU),
        (user32.AppendMenuW, [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
        (user32.SetMenuDefaultItem, [wintypes.HMENU, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        (user32.TrackPopupMenuEx, [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.LPVOID], ctypes.c_int),
        (user32.DestroyMenu, [wintypes.HMENU], wintypes.BOOL),
        (user32.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL),
        (user32.PostMessageW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        (user32.GetCursorPos, [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        (shell32.Shell_NotifyIconW, [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], wintypes.BOOL),
        (shell32.SHQueryUserNotificationState, [ctypes.POINTER(ctypes.c_int)], ctypes.c_long),
        (kernel32.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HMODULE),
    ]
    for function, argtypes, restype in S:
        function.argtypes, function.restype = argtypes, restype


_signatures()

# A menu entry: (label, action key or submenu list, *flags) with flags "default",
# "checked" and "disabled"; None is a separator line.
MenuItem = Union[None, tuple]
_class_ids = itertools.count(1)


def _dark_menus(hwnd) -> None:
    """Let our menus follow Windows' dark mode (uxtheme's unnamed helpers, as used by
    Explorer and many apps; harmless where they don't exist)."""
    try:
        uxtheme = ctypes.WinDLL("uxtheme")
        allow_for_window = uxtheme[133]            # AllowDarkModeForWindow(hwnd, allow)
        allow_for_window.argtypes, allow_for_window.restype = [wintypes.HWND, wintypes.BOOL], wintypes.BOOL
        allow_for_window(hwnd, True)
        set_preferred_app_mode = uxtheme[135]      # SetPreferredAppMode(AllowDark)
        set_preferred_app_mode.argtypes, set_preferred_app_mode.restype = [ctypes.c_int], ctypes.c_int
        set_preferred_app_mode(1)
        flush = uxtheme[136]                       # FlushMenuThemes
        flush.argtypes, flush.restype = [], None
        flush()
    except Exception:
        pass


class TrayIcon:
    """Icon in the notification area. Create, start() and use it on the Tk thread."""

    def __init__(self, icon_path: Path, tooltip: str, on_open: Callable[[], None],
                 menu: Callable[[], list[MenuItem]], on_action: Callable[[str], None]):
        self.icon_path, self.tooltip = Path(icon_path), tooltip
        self.on_open, self.menu, self.on_action = on_open, menu, on_action
        self.visible = False            # the icon is in the notification area right now
        self.hwnd = None
        self._icon = self._big_icon = None
        self._class = f"HuetoothTray{next(_class_ids)}"
        self._hinstance = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._proc)        # keep a reference: Windows calls it later
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    def start(self, show: bool = True) -> bool:
        """Create the hidden window, load the icons and (with show) add the icon.
        False if any of it failed; add() can be retried later."""
        cls = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc,
                          hInstance=self._hinstance, lpszClassName=self._class)
        if not user32.RegisterClassExW(ctypes.byref(cls)):
            log.warning("Tray icon: window class failed (%d)", ctypes.get_last_error())
            return False
        self.hwnd = user32.CreateWindowExW(0, self._class, "Huetooth", 0, 0, 0, 0, 0, None, None,
                                           self._hinstance, None)
        if not self.hwnd:
            log.warning("Tray icon: window failed (%d)", ctypes.get_last_error())
            return False
        small = user32.GetSystemMetrics(SM_CXSMICON) or 16
        big = user32.GetSystemMetrics(SM_CXICON) or 32
        self._icon = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, small, small, LR_LOADFROMFILE)
        self._big_icon = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, big, big, LR_LOADFROMFILE)
        if not self._icon:
            log.warning("Tray icon: cannot load %s", self.icon_path)
            return False
        _dark_menus(self.hwnd)
        return self.add() if show else True

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1, uFlags=flags)
        return data

    def add(self) -> bool:
        if not self.hwnd:
            return False
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        data.uCallbackMessage, data.hIcon, data.szTip = WM_TRAY, self._icon, self.tooltip[:127]
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))     # after an Explorer restart
        self.visible = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))
        if self.visible:
            data.uVersion = NOTIFYICON_VERSION_4
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
        return self.visible

    def set_tooltip(self, text: str) -> None:
        if text == self.tooltip:
            return
        self.tooltip = text
        if self.visible:
            data = self._data(NIF_TIP | NIF_SHOWTIP)
            data.szTip = text[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def notify(self, title: str, text: str) -> bool:
        """A Windows notification. Windows itself holds it back while a game runs full
        screen or Do not disturb is on (it then waits in the notification center)."""
        if not self.visible:
            return False
        data = self._data(NIF_INFO)
        data.szInfoTitle, data.szInfo = title[:63], text[:255]
        data.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON | NIIF_RESPECT_QUIET_TIME
        data.hBalloonIcon = self._big_icon or self._icon
        return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))

    def stop(self) -> None:
        if self.visible:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
            self.visible = False
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
            user32.UnregisterClassW(self._class, self._hinstance)
        for icon in (self._icon, self._big_icon):
            if icon:
                user32.DestroyIcon(icon)
        self._icon = self._big_icon = None

    # -- Windows calls these -----------------------------------------------------------

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK):
                    self.on_open()
                elif event == WM_CONTEXTMENU:
                    x, y = ctypes.c_short(wparam & 0xFFFF).value, ctypes.c_short((wparam >> 16) & 0xFFFF).value
                    self._show_menu(x, y)
                return 0
            if msg == self._taskbar_created and self._taskbar_created:
                self.add()                       # Explorer (re)started: put the icon back
                return 0
        except Exception:
            log.exception("Tray icon")
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, x: int, y: int) -> None:
        actions: dict[int, str] = {}
        menu = self._build_menu(self.menu(), actions)
        try:
            user32.SetForegroundWindow(self.hwnd)    # so the menu closes when clicking elsewhere
            chosen = user32.TrackPopupMenuEx(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                             x, y, self.hwnd, None)
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)                 # also destroys the submenus
        if chosen in actions:
            self.on_action(actions[chosen])

    def _build_menu(self, items: list[MenuItem], actions: dict[int, str]):
        menu = user32.CreatePopupMenu()
        for item in items:
            if item is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            label, target, *flags = item
            if isinstance(target, list):
                submenu = self._build_menu(target, actions)
                user32.AppendMenuW(menu, MF_POPUP | (0 if target else MF_GRAYED), submenu, label)
                continue
            command = len(actions) + 1
            actions[command] = target
            user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if "checked" in flags else 0)
                               | (MF_GRAYED if "disabled" in flags else 0), command, label)
            if "default" in flags:
                user32.SetMenuDefaultItem(menu, command, 0)
        return menu


def user_busy() -> bool:
    """A full-screen game, a presentation or 'busy' mode is running: don't pop up."""
    state = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except Exception:
        return False
    return state.value in (2, 3, 4)     # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE


# -- Start with Windows -----------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_VALUE = "Huetooth"


def autostart_command() -> str:
    """How Windows starts Huetooth at sign-in: hidden, in the tray."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    here = Path(__file__).resolve().parent
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{here / "Huetooth.pyw"}" --tray'


def _read(key: str, value: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, value)[0]
    except OSError:
        return None


def autostart_enabled(run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY) -> bool:
    if _read(run_key, RUN_VALUE) is None:
        return False
    approved = _read(approved_key, RUN_VALUE)
    # Settings > Apps > Startup can switch an entry off; an odd first byte means "off".
    return not (isinstance(approved, bytes) and approved and approved[0] & 1)


def set_autostart(enabled: bool, run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY) -> None:
    if enabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key) as handle:
            winreg.SetValueEx(handle, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
    for key in ((approved_key,) if enabled else (run_key, approved_key)):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, RUN_VALUE)
        except OSError:
            pass


def refresh_autostart(run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY) -> None:
    """Keep an existing entry pointing at this copy of Huetooth (it may have moved)."""
    current = _read(run_key, RUN_VALUE)
    if current is not None and current != autostart_command():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key) as handle:
            winreg.SetValueEx(handle, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
