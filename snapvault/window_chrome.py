"""An integrated main-window header retaining the real Win32 resize frame.

No Qt FramelessWindowHint, tool window, opacity change, or HWND replacement is
used. The native caption style is removed after normal-window creation while
the Win32 resize frame remains. WM_NCCALCSIZE puts the app header at the top;
Win32 still owns moving, resizing, caption double-click and keyboard snapping.
Other app windows keep their existing native decorations.

References: https://learn.microsoft.com/windows/win32/dwm/customframe
https://learn.microsoft.com/windows/apps/desktop/modernize/title-bar
"""
from __future__ import annotations
from .i18n import tr

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QWidget

from .theme import theme


WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCACTIVATE = 0x0086
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCLBUTTONDBLCLK = 0x00A3
WM_NCMOUSEMOVE = 0x00A0
WM_NCMOUSELEAVE = 0x02A2
WM_LBUTTONUP = 0x0202
WM_CAPTURECHANGED = 0x0215
HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT, HTBOTTOM = 13, 14, 15
HTBOTTOMLEFT, HTBOTTOMRIGHT = 16, 17
SYSTEM_DRAG_HITS = frozenset((HTCAPTION, HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT,
                              HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT))
MONITOR_DEFAULTTONEAREST = 2
SM_CXSIZEFRAME, SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 32, 33, 92
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x1, 0x2, 0x4
SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x10, 0x20
GWL_STYLE = -16
WS_CAPTION = 0x00C00000


class NativePoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class NativeRect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("left", "top", "right", "bottom")]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", NativeRect),
                ("rcWork", NativeRect), ("dwFlags", wintypes.DWORD)]


class MinMaxInfo(ctypes.Structure):
    _fields_ = [(name, NativePoint) for name in
                ("ptReserved", "ptMaxSize", "ptMaxPosition", "ptMinTrackSize", "ptMaxTrackSize")]


class TrackMouseInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


def signed_screen_point(lparam):
    """WM_NCHITTEST packs signed screen coordinates, including left monitors."""
    return ctypes.c_int16(lparam & 0xffff).value, ctypes.c_int16((lparam >> 16) & 0xffff).value


def resize_hit_test(x, y, width, height, border_x, border_y, maximized=False):
    """Physical-pixel frame hit testing, independent of Qt's logical DPI."""
    if maximized or not (0 <= x < width and 0 <= y < height):
        return None
    left, right = x < border_x, x >= width - border_x
    top, bottom = y < border_y, y >= height - border_y
    if top:
        return HTTOPLEFT if left else HTTOPRIGHT if right else HTTOP
    if bottom:
        return HTBOTTOMLEFT if left else HTBOTTOMRIGHT if right else HTBOTTOM
    return HTLEFT if left else HTRIGHT if right else None


def maximize_geometry(monitor, work):
    """MINMAXINFO position is relative to the selected monitor, not desktop 0."""
    return (work.left - monitor.left, work.top - monitor.top,
            work.right - work.left, work.bottom - work.top)


@lru_cache(maxsize=1)
def _user_api():
    user = ctypes.WinDLL("user32.dll", winmode=0x00000800)
    definitions = {
        "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(NativeRect)], wintypes.BOOL),
        "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(NativePoint)], wintypes.BOOL),
        "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
        "IsIconic": ([wintypes.HWND], wintypes.BOOL),
        "GetDpiForWindow": ([wintypes.HWND], wintypes.UINT),
        "GetSystemMetricsForDpi": ([ctypes.c_int, wintypes.UINT], ctypes.c_int),
        "MonitorFromWindow": ([wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
        "GetMonitorInfoW": ([wintypes.HANDLE, ctypes.POINTER(MonitorInfo)], wintypes.BOOL),
        "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        "GetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
        "SetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
        "SetCapture": ([wintypes.HWND], wintypes.HWND),
        "ReleaseCapture": ([], wintypes.BOOL),
        "TrackMouseEvent": ([ctypes.POINTER(TrackMouseInfo)], wintypes.BOOL),
        "DefWindowProcW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                           ctypes.c_ssize_t),
    }
    for name, (arguments, result) in definitions.items():
        function = getattr(user, name)
        function.argtypes, function.restype = arguments, result
    return user


class CaptionButton(QAbstractButton):
    """Theme-aware vector controls with the same thin strokes as app icons."""

    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action
        self.maximized = False
        self.native_hover = False
        self.setObjectName("caption" + action.title())
        self.setFixedSize(44, 44)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update_name()

    def update_name(self):
        title = tr({"minimize": "最小化", "maximize": "还原" if self.maximized else "最大化",
                    "close": "关闭窗口"}[self.action])
        self.setAccessibleName(title)
        self.setToolTip(title)
        self.update()

    def set_native_hover(self, hovered):
        if self.native_hover != hovered:
            self.native_hover = hovered
            self.update()

    def paintEvent(self, event):
        p = theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        hovered = self.underMouse() or self.native_hover or self.isDown()
        color = p["text"]
        if hovered:
            background = p["danger"] if self.action == "close" else p["pressed"] if self.isDown() else p["hover"]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(background))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 7, 7)
            if self.action == "close":
                color = "#14161c" if p["dark"] else "#ffffff"
        painter.setPen(QPen(QColor(color), 1.2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center = self.rect().center()
        x, y = center.x() - 5, center.y() - 5
        if self.action == "minimize":
            painter.drawLine(x, y + 5, x + 10, y + 5)
        elif self.action == "close":
            painter.drawLine(x, y, x + 10, y + 10)
            painter.drawLine(x + 10, y, x, y + 10)
        elif self.maximized:
            # Draw only the visible rear edges; an opaque fill in this tiny
            # icon would show a colored patch over the native glass header.
            painter.drawLine(x + 3, y, x + 11, y)
            painter.drawLine(x + 11, y, x + 11, y + 8)
            painter.drawRect(QRectF(x, y + 3, 8, 8))
        else:
            painter.drawRect(QRectF(x, y, 10, 10))
        if self.hasFocus():
            painter.setPen(QPen(QColor(p["accent"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(4, 4, -4, -4), 5, 5)


class MainWindowChrome(QObject):
    """MainWindow-only non-client adapter, installed before any native handle."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.header = None
        self.buttons = {}
        self._pressed_maximize = False
        self._frame_handle = 0
        # Offscreen tests have the same widgets, but never touch desktop APIs.
        from PySide6.QtGui import QGuiApplication
        self.enabled = os.name == "nt" and QGuiApplication.platformName() == "windows"
        window.installEventFilter(self)

    def attach_header(self, header, layout):
        self.header = header
        layout.addSpacing(12)
        controls = QWidget(header)
        controls.setObjectName("captionControls")
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        for action, callback in (("minimize", self.window.showMinimized),
                                 ("maximize", self.toggle_maximized),
                                 ("close", self.window.close)):
            control = CaptionButton(action, controls)
            control.clicked.connect(callback)
            row.addWidget(control)
            self.buttons[action] = control
        layout.addWidget(controls)

    def toggle_maximized(self):
        self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()

    def _update_state(self):
        control = self.buttons.get("maximize")
        if control is not None:
            control.maximized = self.window.isMaximized()
            control.update_name()
        for control in self.buttons.values():
            control.update()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.WindowStateChange, QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange):
            self._update_state()
        elif event.type() == QEvent.Type.Show and self.enabled:
            # Recalculate once per HWND, after Qt has created it. Theme/restore
            # must not call setWindowFlags or replace registered hotkey HWNDs.
            handle = int(self.window.internalWinId())
            if handle and handle != self._frame_handle:
                self._frame_handle = handle
                self._remove_native_caption(handle)
        return False

    def _remove_native_caption(self, handle):
        # NCCALCSIZE removes the reserved caption area but DWM can still draw
        # a faint second set of caption buttons while WS_CAPTION is present.
        # Remove only the native caption bits after Qt creates a normal Window;
        # never set Qt FramelessWindowHint, which would make its alpha surface
        # layered and prevent the native Acrylic backdrop.
        user = _user_api()
        style = int(user.GetWindowLongPtrW(handle, GWL_STYLE))
        user.SetWindowLongPtrW(handle, GWL_STYLE, style & ~WS_CAPTION)
        user.SetWindowPos(handle, None, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)

    def is_drag_point(self, point):
        if self.header is None or not self.header.isVisible():
            return False
        local = self.header.mapFrom(self.window, point)
        if not self.header.rect().contains(local):
            return False
        child = self.header.childAt(local)
        # Header whitespace, logo and brand labels drag. Buttons keep their
        # own clicks, keyboard focus, tooltips and accessibility behavior.
        return child is None or isinstance(child, QLabel)

    def _monitor(self, hwnd):
        user = _user_api()
        monitor = user.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        return info if monitor and user.GetMonitorInfoW(monitor, ctypes.byref(info)) else None

    def _client_point(self, hwnd, screen_x, screen_y):
        origin = NativePoint(0, 0)
        if not _user_api().ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        ratio = self.window.devicePixelRatioF()
        return QPoint(round((screen_x - origin.x) / ratio), round((screen_y - origin.y) / ratio))

    def _maximize_contains(self, point):
        control = self.buttons.get("maximize")
        return bool(point is not None and control is not None and control.isVisible()
                    and control.rect().contains(control.mapFrom(self.window, point)))

    def handle_native_message(self, message):
        if not self.enabled:
            return False, 0
        # ctypes.wintypes.MSG uses the Win32 spelling hWnd (capital W).
        # Using a test-only ``hwnd`` attribute here silently leaves Qt's
        # ordinary native caption in place when a real message is delivered.
        kind, hwnd = message.message, message.hWnd
        user = _user_api()
        if kind == WM_NCCALCSIZE and message.lParam:
            # The first RECT is identical for TRUE NCCALCSIZE_PARAMS and FALSE
            # RECT messages. Returning zero uses the full proposed client area.
            if user.IsZoomed(hwnd) and not user.IsIconic(hwnd):
                info = self._monitor(hwnd)
                if info is not None:
                    rect = NativeRect.from_address(message.lParam)
                    rect.left, rect.top, rect.right, rect.bottom = (
                        info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)
            return True, 0
        if kind == WM_GETMINMAXINFO and message.lParam:
            info = self._monitor(hwnd)
            if info is not None:
                bounds = MinMaxInfo.from_address(message.lParam)
                x, y, width, height = maximize_geometry(info.rcMonitor, info.rcWork)
                bounds.ptMaxPosition.x, bounds.ptMaxPosition.y = x, y
                bounds.ptMaxSize.x, bounds.ptMaxSize.y = width, height
                ratio = self.window.devicePixelRatioF()
                bounds.ptMinTrackSize.x = round(self.window.minimumWidth() * ratio)
                bounds.ptMinTrackSize.y = round(self.window.minimumHeight() * ratio)
                if self.window.maximumWidth() < 16777215:
                    bounds.ptMaxTrackSize.x = round(self.window.maximumWidth() * ratio)
                if self.window.maximumHeight() < 16777215:
                    bounds.ptMaxTrackSize.y = round(self.window.maximumHeight() * ratio)
                return True, 0
        if kind == WM_NCACTIVATE and not user.IsIconic(hwnd):
            return True, 1
        if kind == WM_NCHITTEST:
            sx, sy = signed_screen_point(message.lParam)
            rect = NativeRect()
            if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False, 0
            dpi = user.GetDpiForWindow(hwnd) or 96
            padding = user.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi)
            bx = user.GetSystemMetricsForDpi(SM_CXSIZEFRAME, dpi) + padding
            by = user.GetSystemMetricsForDpi(SM_CYSIZEFRAME, dpi) + padding
            hit = resize_hit_test(sx - rect.left, sy - rect.top, rect.right - rect.left,
                                  rect.bottom - rect.top, bx, by, bool(user.IsZoomed(hwnd)))
            if hit is not None:
                return True, hit
            point = self._client_point(hwnd, sx, sy)
            if self._maximize_contains(point):
                # HTMAXBUTTON enables Windows 11's system Snap Layout flyout.
                return True, HTMAXBUTTON
            return True, HTCAPTION if point is not None and self.is_drag_point(point) else HTCLIENT
        if kind in (WM_NCLBUTTONDOWN, WM_NCLBUTTONDBLCLK) and message.wParam in SYSTEM_DRAG_HITS:
            # A hit-test result only identifies a caption/resize edge; the
            # corresponding press still has to reach the Windows default
            # procedure to enter its SC_MOVE/SC_SIZE modal loop. Qt's event
            # conversion can consume a non-client press on our full-client
            # raster window. Forward the original message once, before Qt
            # converts it, retaining system Snap, drag-to-restore and sizing.
            # DefWindowProc owns the gesture until it returns; do not move or
            # resize the QWidget manually and do not replace its HWND.
            return True, int(user.DefWindowProcW(hwnd, kind, message.wParam, message.lParam))
        maximize = self.buttons.get("maximize")
        if maximize is None:
            return False, 0
        if kind == WM_NCMOUSEMOVE:
            maximize.set_native_hover(message.wParam == HTMAXBUTTON)
            if message.wParam == HTMAXBUTTON:
                tracking = TrackMouseInfo(ctypes.sizeof(TrackMouseInfo), 0x00000012, hwnd, 0)
                user.TrackMouseEvent(ctypes.byref(tracking))  # TME_LEAVE | TME_NONCLIENT
                # Qt can consume non-client mouse events. Let the system see
                # this genuine maximize-button hover to show its Snap flyout.
                result = user.DefWindowProcW(hwnd, kind, message.wParam, message.lParam)
                return True, int(result)
        elif kind == WM_NCMOUSELEAVE:
            maximize.set_native_hover(False)
        elif kind in (WM_NCLBUTTONDOWN, WM_NCLBUTTONDBLCLK) and message.wParam == HTMAXBUTTON:
            self._pressed_maximize = True
            maximize.setDown(True)
            user.SetCapture(hwnd)
            return True, 0
        elif kind in (WM_LBUTTONUP, WM_NCLBUTTONUP) and self._pressed_maximize:
            # Capture yields client mouse-up; non-client mouse-up is also
            # accepted, e.g. when another component releases capture first.
            self._pressed_maximize = False
            user.ReleaseCapture()
            maximize.setDown(False)
            if kind == WM_NCLBUTTONUP:
                sx, sy = signed_screen_point(message.lParam)
                point = self._client_point(hwnd, sx, sy)
            else:
                x, y = signed_screen_point(message.lParam)
                ratio = self.window.devicePixelRatioF()
                point = QPoint(round(x / ratio), round(y / ratio))
            if self._maximize_contains(point):
                QTimer.singleShot(0, self.toggle_maximized)
            return True, 0
        elif kind == WM_CAPTURECHANGED:
            self._pressed_maximize = False
            maximize.setDown(False)
        return False, 0
