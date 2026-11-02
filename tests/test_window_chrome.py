"""Main titlebar behavior with temporary widgets and synthetic Win32 messages.

All desktop API calls are mocked, and no real library/clipboard is accessed.
"""
import ctypes
from ctypes import wintypes

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QWidget

from snapvault import glass, window_chrome as chrome
from test_themes import app, harness, flush


@pytest.mark.parametrize("x,y,expected", [
    (0, 0, chrome.HTTOPLEFT), (799, 0, chrome.HTTOPRIGHT),
    (0, 599, chrome.HTBOTTOMLEFT), (799, 599, chrome.HTBOTTOMRIGHT),
    (200, 0, chrome.HTTOP), (200, 599, chrome.HTBOTTOM),
    (0, 200, chrome.HTLEFT), (799, 200, chrome.HTRIGHT),
    (100, 100, None), (-1, 1, None), (800, 100, None),
])
def test_native_resize_edges_and_corners(x, y, expected):
    assert chrome.resize_hit_test(x, y, 800, 600, 8, 8) == expected
    assert chrome.resize_hit_test(x, y, 800, 600, 8, 8, maximized=True) is None


def test_native_frame_hit_test_scales_and_decodes_negative_monitor_coordinates():
    assert chrome.resize_hit_test(11, 30, 1200, 900, 12, 12) == chrome.HTLEFT
    assert chrome.resize_hit_test(12, 30, 1200, 900, 12, 12) is None
    x, y = -1540, -100
    packed = (x & 0xffff) | ((y & 0xffff) << 16)
    assert chrome.signed_screen_point(packed) == (x, y)


@pytest.mark.parametrize("monitor,work,expected", [
    ((0, 0, 1920, 1080), (0, 0, 1920, 1032), (0, 0, 1920, 1032)),
    ((-1920, -100, 0, 980), (-1920, -100, 0, 932), (0, 0, 1920, 1032)),
    ((0, 0, 1920, 1080), (50, 0, 1920, 1080), (50, 0, 1870, 1080)),
    ((1920, 0, 3840, 1080), (1920, 40, 3840, 1080), (0, 40, 1920, 1040)),
])
def test_maximize_uses_each_monitors_work_area(monitor, work, expected):
    assert chrome.maximize_geometry(chrome.NativeRect(*monitor), chrome.NativeRect(*work)) == expected


def test_integrated_material_keeps_real_native_frame_without_qt_caption_child(app):
    window = QMainWindow()
    flags = window.windowFlags()
    glass.configure_window_glass(window, integrated_titlebar=True)
    assert window.windowFlags() == flags
    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not window.windowFlags() & Qt.WindowType.ExpandedClientAreaHint
    assert not window.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
    assert window.windowOpacity() == 1
    if hasattr(Qt.WidgetAttribute, "WA_ContentsMarginsRespectsSafeArea"):
        assert not window.testAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea)
    window.deleteLater()
    flush(app)


@pytest.fixture
def header(app):
    window = QMainWindow()
    window.setMinimumSize(600, 300)
    window.resize(1000, 600)
    adapter = chrome.MainWindowChrome(window)
    # Test scaffolding must never call live Win32 APIs, even on native Qt.
    adapter.enabled = False
    bar = QFrame()
    bar.setFixedHeight(68)
    layout = QHBoxLayout(bar)
    logo = QLabel("Synthetic app")
    layout.addWidget(logo)
    layout.addStretch()
    nav = QPushButton("Settings")
    layout.addWidget(nav)
    adapter.attach_header(bar, layout)
    root = QWidget()
    from PySide6.QtWidgets import QVBoxLayout
    vertical = QVBoxLayout(root)
    vertical.setContentsMargins(0, 0, 0, 0)
    vertical.addWidget(bar)
    vertical.addStretch()
    window.setCentralWidget(root)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    flush(app)
    yield window, adapter, bar, logo, nav
    window.hide()
    window.deleteLater()
    flush(app)


def test_header_drags_on_brand_and_whitespace_but_preserves_controls(header):
    window, adapter, bar, logo, nav = header
    assert adapter.is_drag_point(logo.mapTo(window, logo.rect().center()))
    assert adapter.is_drag_point(bar.mapTo(window, QPoint(400, 34)))
    assert not adapter.is_drag_point(nav.mapTo(window, nav.rect().center()))
    for control in adapter.buttons.values():
        assert not adapter.is_drag_point(control.mapTo(window, control.rect().center()))
        assert control.width() >= 44 and control.height() >= 44
        assert control.accessibleName() and control.toolTip()
    assert not adapter.is_drag_point(QPoint(300, 120))


def test_caption_maximize_restore_updates_name_and_keeps_handle(header, app):
    window, adapter, *_ = header
    handle = int(window.winId())
    maximize = adapter.buttons["maximize"]
    maximize.click()
    flush(app)
    assert window.isMaximized()
    assert maximize.accessibleName() == "还原"
    maximize.click()
    flush(app)
    assert not window.isMaximized()
    assert maximize.accessibleName() == "最大化"
    assert int(window.winId()) == handle
    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint


class FakeUser:
    def __init__(self):
        self.zoomed = False
        self.captures = []
        self.tracked = []
        self.default_calls = []

    def IsZoomed(self, hwnd):
        return self.zoomed

    def IsIconic(self, hwnd):
        return False

    def GetWindowRect(self, hwnd, out):
        out._obj.left, out._obj.top, out._obj.right, out._obj.bottom = 100, 100, 1100, 700
        return True

    def GetDpiForWindow(self, hwnd):
        return 144

    def GetSystemMetricsForDpi(self, metric, dpi):
        assert dpi == 144
        return 4 if metric == chrome.SM_CXPADDEDBORDER else 8

    def ClientToScreen(self, hwnd, point):
        point._obj.x, point._obj.y = 100, 100
        return True

    def SetCapture(self, hwnd):
        self.captures.append(hwnd)

    def ReleaseCapture(self):
        self.captures.append(None)

    def TrackMouseEvent(self, tracking):
        self.tracked.append(tracking._obj.dwFlags)
        return True

    def DefWindowProcW(self, hwnd, kind, wparam, lparam):
        self.default_calls.append((hwnd, kind, wparam, lparam))
        return 0


def native_message(kind, lparam=0, wparam=0):
    value = wintypes.MSG()
    value.message, value.hWnd = kind, 123
    value.lParam, value.wParam = lparam, wparam
    return value


def test_non_client_layout_removes_caption_and_limits_maximize_without_losing_minimum(header, monkeypatch):
    window, adapter, *_ = header
    user = FakeUser()
    monkeypatch.setattr(chrome, "_user_api", lambda: user)
    adapter.enabled = True
    info = chrome.MonitorInfo()
    info.rcMonitor = chrome.NativeRect(-1920, 0, 0, 1080)
    info.rcWork = chrome.NativeRect(-1920, 0, 0, 1032)
    monkeypatch.setattr(adapter, "_monitor", lambda hwnd: info)
    proposed = chrome.NativeRect(100, 100, 1100, 700)
    assert adapter.handle_native_message(native_message(
        chrome.WM_NCCALCSIZE, ctypes.addressof(proposed), 1)) == (True, 0)
    assert (proposed.left, proposed.top, proposed.right, proposed.bottom) == (100, 100, 1100, 700)
    user.zoomed = True
    adapter.handle_native_message(native_message(chrome.WM_NCCALCSIZE, ctypes.addressof(proposed), 1))
    assert (proposed.left, proposed.top, proposed.right, proposed.bottom) == (-1920, 0, 0, 1032)
    bounds = chrome.MinMaxInfo()
    assert adapter.handle_native_message(native_message(
        chrome.WM_GETMINMAXINFO, ctypes.addressof(bounds))) == (True, 0)
    assert (bounds.ptMaxPosition.x, bounds.ptMaxPosition.y) == (0, 0)
    assert (bounds.ptMaxSize.x, bounds.ptMaxSize.y) == (1920, 1032)
    assert bounds.ptMinTrackSize.x == round(window.minimumWidth() * window.devicePixelRatioF())
    assert bounds.ptMinTrackSize.y == round(window.minimumHeight() * window.devicePixelRatioF())
    assert adapter.handle_native_message(native_message(chrome.WM_NCACTIVATE)) == (True, 1)


def test_native_hit_testing_exposes_snap_target_and_preserves_navigation(header, monkeypatch):
    window, adapter, bar, logo, nav = header
    user = FakeUser()
    monkeypatch.setattr(chrome, "_user_api", lambda: user)
    adapter.enabled = True
    pack = lambda x, y: (x & 0xffff) | ((y & 0xffff) << 16)
    assert adapter.handle_native_message(native_message(chrome.WM_NCHITTEST, pack(101, 101))) == (True, chrome.HTTOPLEFT)
    point = [adapter.buttons["maximize"].mapTo(window, adapter.buttons["maximize"].rect().center())]
    monkeypatch.setattr(adapter, "_client_point", lambda *_: point[0])
    assert adapter.handle_native_message(native_message(chrome.WM_NCHITTEST, pack(900, 130))) == (True, chrome.HTMAXBUTTON)
    point[0] = nav.mapTo(window, nav.rect().center())
    assert adapter.handle_native_message(native_message(chrome.WM_NCHITTEST, pack(900, 130))) == (True, chrome.HTCLIENT)
    point[0] = logo.mapTo(window, logo.rect().center())
    assert adapter.handle_native_message(native_message(chrome.WM_NCHITTEST, pack(300, 130))) == (True, chrome.HTCAPTION)


def test_snap_button_native_click_and_cancel_are_delivered_once(header, monkeypatch, app):
    window, adapter, *_ = header
    user = FakeUser()
    monkeypatch.setattr(chrome, "_user_api", lambda: user)
    adapter.enabled = True
    point = [adapter.buttons["maximize"].mapTo(window, adapter.buttons["maximize"].rect().center())]
    monkeypatch.setattr(adapter, "_client_point", lambda *_: point[0])
    calls = []
    monkeypatch.setattr(adapter, "toggle_maximized", lambda: calls.append("toggle"))
    assert adapter.handle_native_message(native_message(chrome.WM_NCMOUSEMOVE,
        wparam=chrome.HTMAXBUTTON)) == (True, 0)
    assert adapter.buttons["maximize"].native_hover and user.tracked == [0x12]
    adapter.handle_native_message(native_message(chrome.WM_NCMOUSELEAVE))
    assert not adapter.buttons["maximize"].native_hover
    down = native_message(chrome.WM_NCLBUTTONDOWN, wparam=chrome.HTMAXBUTTON)
    up = native_message(chrome.WM_NCLBUTTONUP, wparam=chrome.HTMAXBUTTON)
    assert adapter.handle_native_message(down) == (True, 0)
    assert adapter.buttons["maximize"].isDown()
    assert adapter.handle_native_message(up) == (True, 0)
    flush(app)
    assert calls == ["toggle"] and user.captures == [123, None]
    assert not adapter.buttons["maximize"].isDown()
    adapter.handle_native_message(down)
    point[0] = QPoint(10, 200)
    adapter.handle_native_message(up)
    flush(app)
    assert calls == ["toggle"], "Releasing outside a caption control must cancel the action"
    adapter.handle_native_message(down)
    adapter.handle_native_message(native_message(chrome.WM_CAPTURECHANGED))
    assert not adapter.buttons["maximize"].isDown()
    assert adapter.handle_native_message(native_message(0x0312)) == (False, 0), "Hotkeys still reach MainWindow"


def test_main_header_starts_at_top_and_theme_does_not_recreate_hwnd(harness, app):
    window = harness()
    assert window.window_chrome.header.mapTo(window, QPoint(0, 0)).y() == 0
    assert not window.windowFlags() & Qt.WindowType.ExpandedClientAreaHint
    handle = int(window.winId())
    for name in ("windows_dark", "windows_light"):
        window.set_theme(name, persist=False)
        flush(app)
        assert int(window.winId()) == handle
        assert window.windowOpacity() == 1
    assert set(window.window_chrome.buttons) == {"minimize", "maximize", "close"}


def test_main_native_event_accepts_zero_sized_shiboken_message_pointer(harness, monkeypatch):
    """The actual Qt callback supplies a falsey VoidPtr with a valid address."""
    from shiboken6 import VoidPtr

    window = harness()
    user = FakeUser()
    monkeypatch.setattr(chrome, "_user_api", lambda: user)
    monkeypatch.setattr(window.window_chrome, "enabled", True)
    proposed = chrome.NativeRect(100, 100, 1100, 700)
    message = native_message(chrome.WM_NCCALCSIZE, ctypes.addressof(proposed), 1)
    pointer = VoidPtr(ctypes.addressof(message), 0)
    assert int(pointer) != 0 and not pointer
    assert window.nativeEvent(b"windows_generic_MSG", pointer) == (True, 0)
    assert (proposed.left, proposed.top, proposed.right, proposed.bottom) == (100, 100, 1100, 700)


def test_native_caption_is_removed_without_changing_qt_window_type(header, monkeypatch):
    """DWM must not draw its own buttons above the application's controls."""
    window, adapter, *_ = header
    before_flags = window.windowFlags()
    initial_style = 0x00CF0000  # WS_OVERLAPPEDWINDOW: caption, resize, menu, min/max.
    calls = []

    class FrameUser:
        def GetWindowLongPtrW(self, handle, index):
            assert (handle, index) == (123, chrome.GWL_STYLE)
            return initial_style

        def SetWindowLongPtrW(self, handle, index, value):
            calls.append(("style", handle, index, value))

        def SetWindowPos(self, *args):
            calls.append(("frame", *args))

    monkeypatch.setattr(chrome, "_user_api", lambda: FrameUser())
    adapter._remove_native_caption(123)
    style = calls[0][-1]
    assert not style & chrome.WS_CAPTION
    assert style & 0x000F0000 == 0x000F0000, "Resize, menu and min/max styles must remain"
    assert window.windowFlags() == before_flags
    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert calls[1][-1] & chrome.SWP_FRAMECHANGED


@pytest.mark.parametrize("hit", sorted(chrome.SYSTEM_DRAG_HITS))
@pytest.mark.parametrize("kind", [chrome.WM_NCLBUTTONDOWN, chrome.WM_NCLBUTTONDBLCLK])
def test_native_drag_press_reaches_windows_system_loop_once(header, monkeypatch, hit, kind):
    """A correct cursor/hit code is insufficient: forward the actual press."""
    window, adapter, *_ = header
    user = FakeUser()
    monkeypatch.setattr(chrome, "_user_api", lambda: user)
    adapter.enabled = True
    handle, flags, geometry = int(window.winId()), window.windowFlags(), window.geometry()
    # Preserve packed negative screen coordinates verbatim. They are physical
    # Win32 coordinates, so applying Qt's DPI factor here would be incorrect.
    packed = ((-1700) & 0xffff) | (((-80) & 0xffff) << 16)
    message = native_message(kind, packed, hit)
    assert adapter.handle_native_message(message) == (True, 0)
    assert user.default_calls == [(123, kind, hit, packed)]
    assert not user.captures, "Windows must own its move/size capture, not Qt"
    assert int(window.winId()) == handle and window.windowFlags() == flags
    assert window.geometry() == geometry, "The adapter must not implement manual dragging"


def test_native_maximize_button_is_not_forwarded_as_a_drag(header, monkeypatch):
    _window, adapter, *_ = header
    user = FakeUser()
    monkeypatch.setattr(chrome, "_user_api", lambda: user)
    adapter.enabled = True
    adapter.handle_native_message(native_message(chrome.WM_NCLBUTTONDOWN, wparam=chrome.HTMAXBUTTON))
    assert not user.default_calls and user.captures == [123]
    assert adapter.handle_native_message(native_message(chrome.WM_NCLBUTTONDOWN, wparam=chrome.HTCLIENT)) == (False, 0)
    assert not user.default_calls


def test_native_drag_forwards_default_procedures_result_without_a_second_dispatch(header, monkeypatch):
    _window, adapter, *_ = header
    user = FakeUser()
    calls = []
    def dispatch(*args):
        calls.append(args)
        return 0x12345678
    monkeypatch.setattr(user, "DefWindowProcW", dispatch)
    monkeypatch.setattr(chrome, "_user_api", lambda: user)
    adapter.enabled = True
    message = native_message(chrome.WM_NCLBUTTONDOWN, wparam=chrome.HTCAPTION)
    assert adapter.handle_native_message(message) == (True, 0x12345678)
    assert len(calls) == 1
