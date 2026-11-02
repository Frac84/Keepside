"""System-wide shortcuts through RegisterHotKey, and pasting into another window.

RegisterHotKey delivers WM_HOTKEY to one of our own windows; no keyboard hook
is installed and no other keystroke is observed.
"""
from __future__ import annotations
from .i18n import tr

import ctypes
import os

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN, VK_V = 0x11, 0x10, 0x12, 0x5B, 0x5C, 0x56
KEYEVENTF_KEYUP, INPUT_KEYBOARD = 0x2, 1

DEFAULT_QUICK_HOTKEY = "Alt+Shift+V"
DEFAULT_PIN_HOTKEY = "Alt+Shift+P"
DEFAULT_FULLSCREEN_HOTKEY = "Alt+Shift+S"
DEFAULT_REGION_HOTKEY = ""

_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_MODIFIER_ORDER = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))

# Windows virtual-key codes and the names used in saved settings.
VK_NAMES = {ord(letter): letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
VK_NAMES.update({ord(digit): digit for digit in "0123456789"})
VK_NAMES.update({0x6F + number: f"F{number}" for number in range(1, 25)})
VK_NAMES.update({0x60 + number: f"Num{number}" for number in range(10)})
VK_NAMES.update({
    0x20: "Space", 0x09: "Tab", 0x2D: "Insert", 0x2E: "Delete", 0x24: "Home", 0x23: "End",
    0x21: "PageUp", 0x22: "PageDown", 0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2C: "PrintScreen", 0x13: "Pause", 0x91: "ScrollLock",
    0xC0: "`", 0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]", 0xDC: "\\", 0xBA: ";", 0xDE: "'",
    0xBC: ",", 0xBE: ".", 0xBF: "/",
    0x6A: "NumMultiply", 0x6B: "NumAdd", 0x6D: "NumSubtract", 0x6E: "NumDecimal", 0x6F: "NumDivide",
})
_NAME_TO_VK = {name.casefold(): vk for vk, name in VK_NAMES.items()}
_NAME_TO_VK["prtsc"] = 0x2C
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
# Keys that do not type anything may be used alone; every other key needs a
# modifier, or the user could no longer type it anywhere.
STANDALONE_KEYS = {"PrintScreen", "Pause", "ScrollLock"} | {f"F{number}" for number in range(1, 25)}
_LABELS = {"PrintScreen": "PrtSc", "Space": "空格", "ScrollLock": "Scroll Lock", "PageUp": "Page Up",
           "PageDown": "Page Down", "Left": "←", "Right": "→", "Up": "↑", "Down": "↓"}


def format_hotkey(modifiers, vk):
    """Canonical saved text, e.g. "Ctrl+Alt+S"; raises ValueError for unusable keys."""
    if vk in MODIFIER_VKS:
        raise ValueError(tr('请再按一个非修饰键'))
    name = VK_NAMES.get(vk)
    if name is None:
        raise ValueError(tr('不支持这个按键，请换一个'))
    if not modifiers & (MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN) and name not in STANDALONE_KEYS:
        raise ValueError(tr('单独一个键只能是 F1–F24、PrtSc、Pause 或 Scroll Lock；其他键请配合 Ctrl、Alt、Shift 或 Win'))
    if modifiers == MOD_SHIFT and name not in STANDALONE_KEYS:
        raise ValueError(tr('Shift 加这个键会变成打字时的大写或符号，请再加上 Ctrl、Alt 或 Win'))
    parts = [label for flag, label in _MODIFIER_ORDER if modifiers & flag]
    return "+".join(parts + [name])


def parse_hotkey(text):
    """Return (modifiers, virtual key) for a shortcut such as "Alt+Shift+V"."""
    if not isinstance(text, str) or not text.strip():
        return None
    *modifier_names, key = [part.strip() for part in text.split("+")]
    modifiers = 0
    for name in modifier_names:
        if name.casefold() not in _MODIFIERS:
            raise ValueError(tr('未知的修饰键：{v0}', v0=name))
        modifiers |= _MODIFIERS[name.casefold()]
    vk = _NAME_TO_VK.get(key.casefold())
    if vk is None:
        raise ValueError(tr('不支持的按键：{v0}', v0=key))
    if not modifiers and VK_NAMES[vk] not in STANDALONE_KEYS:
        raise ValueError(tr('全局快捷键至少需要一个修饰键'))
    return modifiers, vk


def hotkey_label(text):
    """Readable form for menus and messages, e.g. "Ctrl + Alt + S" or "PrtSc"."""
    if not text:
        return tr('未设置')
    try:
        modifiers, vk = parse_hotkey(text)
    except ValueError:
        return text
    parts = [label for flag, label in _MODIFIER_ORDER if modifiers & flag]
    name = VK_NAMES[vk]
    return " + ".join(parts + [_LABELS.get(name, name)])


class GlobalHotkey:
    """One RegisterHotKey registration owned by a window handle."""

    def __init__(self, hotkey_id):
        self.hotkey_id = hotkey_id
        self.hwnd = 0
        self.text = ""
        self.error = ""

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user.RegisterHotKey.restype = wintypes.BOOL
        user.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user.UnregisterHotKey.restype = wintypes.BOOL
        return user

    def register(self, hwnd, text):
        """Replace any earlier registration; returns True when the shortcut is live."""
        self.unregister()
        self.text, self.error = text or "", ""
        if not text:
            return False
        try:
            modifiers, key = parse_hotkey(text)
        except ValueError as exc:
            self.error = str(exc)
            return False
        user = self._user32()
        if user is None:
            self.error = tr('全局快捷键仅支持 Windows。')
            return False
        if not user.RegisterHotKey(hwnd, self.hotkey_id, modifiers | MOD_NOREPEAT, key):
            self.error = tr('{v0} 已被其他程序占用，请在设置中更换。', v0=hotkey_label(text))
            return False
        self.hwnd = hwnd
        return True

    def unregister(self):
        if self.hwnd:
            user = self._user32()
            if user is not None:
                user.UnregisterHotKey(self.hwnd, self.hotkey_id)
            self.hwnd = 0

    @property
    def active(self):
        return bool(self.hwnd)


def hotkey_message_id(message):
    """The hotkey id when a native Windows message is WM_HOTKEY, else None."""
    # Qt supplies an unsized Shiboken VoidPtr here. Its truthiness can raise
    # IndexError even when its address is valid, so inspect the address only.
    if os.name != "nt" or message is None:
        return None
    address = int(message)
    if address == 0:
        return None
    from ctypes import wintypes
    msg = wintypes.MSG.from_address(address)
    return int(msg.wParam) if msg.message == WM_HOTKEY else None


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    # MOUSEINPUT is the largest member; reserve its size so SendInput accepts cbSize.
    _fields_ = [("ki", _KeyboardInput), ("padding", ctypes.c_byte * 32)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]


_SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                  "TopLevelWindowForOverflowXamlIsland", "Progman", "WorkerW"}


def foreground_window():
    """The window a paste should go back to, or 0 for the taskbar, desktop or 拾影 itself.

    Opening the palette from the tray leaves the taskbar in front; pasting
    there would do nothing useful, so such opens only copy.
    """
    if os.name != "nt":
        return 0
    from ctypes import wintypes
    user = ctypes.WinDLL("user32")
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    hwnd = user.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ctypes.create_unicode_buffer(128)
    user.GetClassNameW(hwnd, name, len(name))
    if pid.value == os.getpid() or name.value in _SHELL_CLASSES:
        return 0
    return int(hwnd)


def focus_window(hwnd):
    if os.name != "nt" or not hwnd:
        return False
    user = ctypes.WinDLL("user32")
    user.IsWindow.argtypes = user.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    return bool(user.IsWindow(hwnd) and user.SetForegroundWindow(hwnd))


def send_paste():
    """Press Ctrl+V once in the foreground window, releasing held modifiers first."""
    if os.name != "nt":
        return False
    user = ctypes.WinDLL("user32")
    user.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user.GetAsyncKeyState.restype = ctypes.c_short
    events = []
    # A shortcut key still held (e.g. Shift) would turn Ctrl+V into Ctrl+Shift+V.
    for key in (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN):
        if user.GetAsyncKeyState(key) & 0x8000:
            events.append((key, KEYEVENTF_KEYUP))
    events += [(VK_CONTROL, 0), (VK_V, 0), (VK_V, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)]
    inputs = (_Input * len(events))()
    for slot, (key, flags) in zip(inputs, events):
        slot.type = INPUT_KEYBOARD
        slot.union.ki = _KeyboardInput(key, 0, flags, 0, 0)
    user.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    user.SendInput.restype = ctypes.c_uint
    return user.SendInput(len(events), inputs, ctypes.sizeof(_Input)) == len(events)
