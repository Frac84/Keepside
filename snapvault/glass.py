"""Opt-in native glass for a non-layered Qt top-level window.

Call after the HWND exists (normally after show()), and again after a theme,
native-handle or composition change. This helper never changes widget flags,
system settings, opacity, or application painting. The caller owns a readable
opaque fallback when ``enabled`` is false.

Keep windowOpacity at 1.0 and use a non-layered native HWND. On Qt 6.11, the
native-frame alpha path can use Tool | CustomizeWindowHint |
ExpandedClientAreaHint | NoTitleBarBackgroundHint, without FramelessWindowHint,
and WA_TranslucentBackground=True before any winId()/show(). The Qt attribute
requests an alpha backing store, while the retained native frame avoids Qt's
frameless-alpha WS_EX_LAYERED path. The actual extended native style, rather
than the Qt attribute alone, is the rejection criterion below. The caller must
verify this combination visually on its supported Qt/Windows version.

Use autoFillBackground=False, a transparent root stylesheet, and translucent
child surface tints; an opaque root/card hides the material. A circular launcher
can use layered Qt transparency, but should not request this native backdrop.

Prepared application windows use Accent acrylic with a DWM alpha region. Qt's
raster backing store on the supported Windows build otherwise paints the
documented Desktop Acrylic request as solid despite successful readback.
The Accent entry point is undocumented and is isolated behind capability and
policy checks; failure uses the opaque stylesheet. Recent-file panels retain
their existing documented Desktop Acrylic path.

``enabled`` confirms the native request and alpha setup;
it does not claim pixel-level proof of blur. Windows can still substitute a
solid material when the window deactivates or system conditions change.

References:
https://learn.microsoft.com/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type
https://learn.microsoft.com/windows/win32/api/dwmapi/nf-dwmapi-dwmextendframeintoclientarea
https://learn.microsoft.com/windows/win32/dwm/customframe
https://learn.microsoft.com/windows/apps/design/style/acrylic
https://doc.qt.io/qt-6/qwidget.html#creating-translucent-windows
https://github.com/qt/qtbase/blob/dev/src/plugins/platforms/windows/qwindowswindow.cpp
https://github.com/zhiyiYo/PyQt-Frameless-Window/blob/master/qframelesswindow/windows/window_effect.py
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
from weakref import WeakKeyDictionary

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_CAPTION_COLOR = 35
DWMWA_COLOR_DEFAULT = -1
DWMSBT_NONE = 1
DWMSBT_TRANSIENTWINDOW = 3
DWMWCP_ROUND = 2
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
SM_REMOTESESSION = 0x1000
SPI_GETHIGHCONTRAST = 0x0042
SPI_GETDISABLEOVERLAPPEDCONTENT = 0x1040
HCF_HIGHCONTRASTON = 1
_active = WeakKeyDictionary()
_accent_active = WeakKeyDictionary()


class _MaterialLifecycle(QObject):
    """Reapply a window's material after native-handle/theme/state changes."""

    def __init__(self, widget):
        super().__init__(widget)
        self.widget = widget
        self.root = None
        self.dark = False
        self.requested = True
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        widget.installEventFilter(self)

    def schedule(self):
        try:
            if self.widget.isVisible():
                self.timer.start(0)
        except RuntimeError:
            # Native handle notifications can arrive during QWidget teardown.
            pass

    def refresh(self):
        try:
            if self.widget.isVisible():
                refresh_window_glass(self.widget, self.dark, self.root, enabled=self.requested)
                self.keep_caption_visible()
        except RuntimeError:
            pass

    def keep_caption_visible(self):
        # Qt's expanded native client area can change its frame margins during
        # the first DPI/layout pass. Keep the restored caption reachable without
        # fighting Windows' maximized/minimized or multi-monitor geometry.
        widget = self.widget
        if (_qt_platform() != "windows" or not widget.isVisible()
                or widget.isMaximized() or widget.isMinimized()):
            return
        screen = widget.screen()
        if screen is not None:
            available = screen.availableGeometry()
            frame = widget.frameGeometry()
            if frame.top() < available.top():
                widget.move(widget.x(), widget.y() + available.top() - frame.top())

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange,
                            QEvent.Type.WindowStateChange, QEvent.Type.ThemeChange,
                            QEvent.Type.ApplicationPaletteChange):
            self.schedule()
        elif event.type() == QEvent.Type.Hide:
            try:
                self.timer.stop()
            except RuntimeError:
                pass
        return False


def configure_window_glass(widget, *, integrated_titlebar=False):
    """Prepare a normal app/dialog window before its first HWND is created.

    Native caption buttons, taskbar presence, resizing and window type stay
    intact. This is never applied to screenshot overlays or image pins.
    """
    if getattr(widget, "_window_material_controller", None) is not None:
        return
    widget.setProperty("appGlass", True)
    widget.setProperty("nativeGlass", False)
    widget.glass_state = dict(enabled=False, method="fallback", reason="not shown")
    widget.setAutoFillBackground(False)
    if os.name == "nt" and hasattr(Qt.WindowType, "ExpandedClientAreaHint"):
        if not integrated_titlebar:
            widget.setWindowFlags(widget.windowFlags() | Qt.WindowType.ExpandedClientAreaHint |
                                  Qt.WindowType.NoTitleBarBackgroundHint)
        # The main window supplies its own non-client layout. Keeping the real
        # native frame (without Qt's expanded-area caption child) lets it use
        # the same non-layered alpha backing store as ordinary glass dialogs.
        widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if hasattr(Qt.WidgetAttribute, "WA_ContentsMarginsRespectsSafeArea"):
            widget.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea,
                                not integrated_titlebar)
    widget._window_material_controller = _MaterialLifecycle(widget)


def refresh_window_glass(widget, dark=False, root=None, *, enabled=True):
    """Apply native material and activate only this window's glass QSS rules."""
    controller = getattr(widget, "_window_material_controller", None)
    if controller is None:
        configure_window_glass(widget)
        controller = widget._window_material_controller
    controller.dark, controller.root, controller.requested = bool(dark), root, bool(enabled)
    previous = bool(widget.property("nativeGlass"))
    state = apply_glass(widget, bool(dark)) if enabled else disable_glass(widget)
    enabled = bool(state.get("enabled"))
    if enabled and state.get("method") == "dwm-desktop-acrylic":
        try:
            # native_titlebar supplies a solid fallback color. Once Acrylic is
            # active, let Windows extend that material through its caption too.
            dwm = _load_api()[1]
            _set_attribute(dwm, wintypes.HWND(int(widget.winId())),
                           DWMWA_CAPTION_COLOR, DWMWA_COLOR_DEFAULT)
        except (OSError, AttributeError, RuntimeError, ValueError, ctypes.ArgumentError):
            pass
    widget.glass_state = state
    widget.setProperty("nativeGlass", enabled)
    root_changed = root is not None and bool(root.property("nativeGlass")) != enabled
    if root is not None:
        root.setProperty("nativeGlass", enabled)
    if enabled != previous or root_changed:
        # Ancestor property selectors affect descendants, including settings
        # widgets with their own local stylesheets. No pixels/data are changed.
        for control in [widget, *widget.findChildren(QWidget)]:
            control.style().unpolish(control)
            control.style().polish(control)
            control.update()
    return state


def disable_glass(widget):
    """Clear a previously requested material when selecting an opaque theme."""
    api = None
    if widget in _active and os.name == "nt" and _qt_platform() == "windows":
        try:
            api = _load_api()
        except (OSError, AttributeError):
            pass
    return _fallback(widget, "theme-disabled", api)


class _Margins(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in
                ("cxLeftWidth", "cxRightWidth", "cyTopHeight", "cyBottomHeight")]


class _AccentPolicy(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in ("state", "flags", "color", "animation")]


class _CompositionData(ctypes.Structure):
    _fields_ = [("attribute", ctypes.c_int32), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]


class _BlurBehind(ctypes.Structure):
    _fields_ = [("flags", wintypes.DWORD), ("enable", wintypes.BOOL),
                ("region", wintypes.HANDLE), ("transition", wintypes.BOOL)]


def _load_acrylic_api():
    user = ctypes.WinDLL("user32.dll", winmode=0x00000800)
    dwm = ctypes.WinDLL("dwmapi.dll", winmode=0x00000800)
    gdi = ctypes.WinDLL("gdi32.dll", winmode=0x00000800)
    _bind(user.SetWindowCompositionAttribute, [wintypes.HWND, ctypes.POINTER(_CompositionData)], wintypes.BOOL)
    _bind(dwm.DwmEnableBlurBehindWindow, [wintypes.HWND, ctypes.POINTER(_BlurBehind)], ctypes.c_int32)
    _bind(gdi.CreateRectRgn, [ctypes.c_int] * 4, wintypes.HANDLE)
    _bind(gdi.DeleteObject, [wintypes.HANDLE], wintypes.BOOL)
    return user, dwm, gdi


def _accent(user, hwnd, state, dark=False):
    # ABGR. A neutral tint restrains wallpaper color; the reading surfaces
    # supply the remaining gray hierarchy and blue stays an action accent.
    policy = _AccentPolicy(state, 0, 0x99202020 if dark else 0x99F3F3F3, 0)
    data = _CompositionData(19, ctypes.addressof(policy), ctypes.sizeof(policy))
    return bool(user.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))


def _clear_acrylic(hwnd):
    user, dwm, _gdi = _load_acrylic_api()
    _accent(user, hwnd, 0)
    blur = _BlurBehind(1, False, None, False)
    dwm.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(blur))


def _apply_raster_acrylic(widget, dark, api, hwnd, handle):
    user, dwm, gdi = _load_acrylic_api()
    _set_attribute(api[1], hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
    _set_attribute(api[1], hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    _set_attribute(api[1], hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, int(bool(dark)))
    region = gdi.CreateRectRgn(0, 0, -1, -1)
    if not region:
        return _fallback(widget, "acrylic-alpha-region-unavailable", api)
    # Register before native calls so an exception after a partial request can
    # still clear both Accent and DWM alpha state through the common fallback.
    _active[widget] = handle
    _accent_active[widget] = handle
    try:
        accent_ok = _accent(user, hwnd, 4, dark)
        blur = _BlurBehind(3, True, region, False)
        alpha_ok = int(dwm.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(blur))) == 0
    finally:
        # DWM copies the region. The GDI object belongs to this call.
        gdi.DeleteObject(region)
    if not accent_ok or not alpha_ok:
        _clear_acrylic(hwnd)
        _clear(api[1], hwnd)
        return _fallback(widget, "acrylic-request-failed", api)
    return dict(enabled=True, method="accent-acrylic", reason="qt-raster-alpha-region; native request accepted")


class _HighContrast(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD),
                ("lpszDefaultScheme", wintypes.LPWSTR)]


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def _bind(function, arguments, result):
    function.argtypes = arguments
    function.restype = result
    return function


def _load_api():
    # System DLL search only. No bundled or working-directory DLL is loaded.
    user = ctypes.WinDLL("user32.dll", winmode=0x00000800)
    dwm = ctypes.WinDLL("dwmapi.dll", winmode=0x00000800)
    kernel = ctypes.WinDLL("kernel32.dll", winmode=0x00000800)
    hwnd, dword, pointer = wintypes.HWND, wintypes.DWORD, ctypes.c_void_p
    _bind(dwm.DwmSetWindowAttribute, [hwnd, dword, pointer, dword], ctypes.c_int32)
    _bind(dwm.DwmGetWindowAttribute, [hwnd, dword, pointer, dword], ctypes.c_int32)
    _bind(dwm.DwmExtendFrameIntoClientArea, [hwnd, ctypes.POINTER(_Margins)], ctypes.c_int32)
    _bind(dwm.DwmIsCompositionEnabled, [ctypes.POINTER(wintypes.BOOL)], ctypes.c_int32)
    _bind(user.GetSystemMetrics, [ctypes.c_int], ctypes.c_int)
    _bind(user.SystemParametersInfoW, [wintypes.UINT, wintypes.UINT, pointer, wintypes.UINT], wintypes.BOOL)
    get_style = user.GetWindowLongPtrW if ctypes.sizeof(pointer) == 8 else user.GetWindowLongW
    _bind(get_style, [hwnd, ctypes.c_int], ctypes.c_ssize_t)
    _bind(kernel.GetSystemPowerStatus, [ctypes.POINTER(_PowerStatus)], wintypes.BOOL)
    return user, dwm, kernel, get_style


def _build():
    return sys.getwindowsversion().build


def _qt_platform():
    application = QGuiApplication.instance()
    return application.platformName() if application else ""


def _transparency_preference():
    """Best-effort read-only preference hint, not a public Win32 API contract.

    DWM remains responsible for final policy. An unavailable hint is unknown,
    and is never interpreted as permission to override any system preference.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _kind = winreg.QueryValueEx(key, "EnableTransparency")
        return bool(value)
    except (OSError, ImportError):
        return None


def _policy_reason(user, kernel):
    if user.GetSystemMetrics(SM_REMOTESESSION):
        return "remote-session"
    contrast = _HighContrast()
    contrast.cbSize = ctypes.sizeof(contrast)
    if not user.SystemParametersInfoW(SPI_GETHIGHCONTRAST, contrast.cbSize, ctypes.byref(contrast), 0):
        return "accessibility-state-unavailable"
    if contrast.dwFlags & HCF_HIGHCONTRASTON:
        return "high-contrast"
    disable_overlap = wintypes.BOOL()
    if user.SystemParametersInfoW(SPI_GETDISABLEOVERLAPPEDCONTENT, 0, ctypes.byref(disable_overlap), 0) and disable_overlap.value:
        return "transparent-overlap-disabled"
    power = _PowerStatus()
    if kernel.GetSystemPowerStatus(ctypes.byref(power)) and power.SystemStatusFlag == 1:
        return "battery-saver"
    if _transparency_preference() is False:
        return "transparency-disabled"
    return ""


def _set_attribute(dwm, hwnd, attribute, value):
    data = ctypes.c_int32(value)
    return int(dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)))


def _clear(dwm, hwnd):
    _set_attribute(dwm, hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
    margins = _Margins(0, 0, 0, 0)
    dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))


def _fallback(widget, reason, api=None, hwnd=None):
    previous = _active.pop(widget, None)
    if previous and api:
        try:
            # Avoid clearing a destroyed/replaced HWND after a Qt flag change.
            current = int(widget.internalWinId())
            if current == previous:
                if _accent_active.get(widget) == previous:
                    _clear_acrylic(wintypes.HWND(current))
                _clear(api[1], wintypes.HWND(current))
        except (OSError, AttributeError, RuntimeError, TypeError, ValueError):
            pass
    _accent_active.pop(widget, None)
    return dict(enabled=False, method="fallback", reason=reason)


def apply_glass(widget, dark=False):
    """Request native Win11 glass or report a readable opaque fallback.

    Raster app windows use the isolated Accent compatibility path; other
    surfaces use documented Desktop Acrylic. No screenshot sampling, global
    transparency changes, or new dependencies. Call on the GUI thread.
    """
    api = None
    try:
        if os.name != "nt":
            return _fallback(widget, "non-windows")
        if _qt_platform() != "windows":
            return _fallback(widget, "non-native-qt-platform")
        if _build() < 22621:
            return _fallback(widget, "requires-windows-11-22621")
        api = _load_api()
        user, dwm, kernel, get_style = api
        reason = _policy_reason(user, kernel)
        if reason:
            return _fallback(widget, reason, api)
        if widget.windowOpacity() < 0.999:
            return _fallback(widget, "whole-window-opacity", api)
        handle = int(widget.winId())
        if not handle:
            return _fallback(widget, "no-native-window", api)
        hwnd = wintypes.HWND(handle)
        if int(get_style(hwnd, GWL_EXSTYLE)) & WS_EX_LAYERED:
            return _fallback(widget, "layered-native-window", api)
        composition = wintypes.BOOL()
        if int(dwm.DwmIsCompositionEnabled(ctypes.byref(composition))) != 0 or not composition.value:
            return _fallback(widget, "composition-unavailable", api)
        margins = _Margins(-1, -1, -1, -1)
        extended = int(dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)))
        if extended != 0:
            return _fallback(widget, f"frame-extension-failed:0x{extended & 0xffffffff:08x}", api)
        if bool(getattr(widget, "property", lambda _name: False)("appGlass")):
            return _apply_raster_acrylic(widget, dark, api, hwnd, handle)
        # Corner preference is advisory; the native material is still useful if
        # Windows declines rounding for this particular window shape/style.
        _set_attribute(dwm, hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        themed = _set_attribute(dwm, hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, int(bool(dark)))
        applied = _set_attribute(dwm, hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)
        if applied != 0 or themed != 0:
            _clear(dwm, hwnd)
            return _fallback(widget, f"backdrop-request-failed:0x{(applied or themed) & 0xffffffff:08x}", api)
        confirmed = ctypes.c_int32()
        read = int(dwm.DwmGetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(confirmed), ctypes.sizeof(confirmed)))
        if read != 0 or confirmed.value != DWMSBT_TRANSIENTWINDOW:
            _clear(dwm, hwnd)
            return _fallback(widget, "backdrop-readback-unconfirmed", api)
        _active[widget] = handle
        return dict(enabled=True, method="dwm-desktop-acrylic",
                    reason="native-request-confirmed; Windows may substitute a solid system fallback")
    except (OSError, AttributeError, RuntimeError, TypeError, ValueError, ctypes.ArgumentError) as exc:
        return _fallback(widget, "native-api-unavailable:" + type(exc).__name__, api)
