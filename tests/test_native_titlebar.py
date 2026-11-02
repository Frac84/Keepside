"""Caption appearance contracts plus bounded inspector titles on synthetic data.

These API tests do not claim compositor verification; the release's native
window preview remains necessary for the Windows-rendered caption buttons.
"""
import ctypes
from hashlib import sha256
from types import SimpleNamespace

import pytest
from PySide6 import QtWidgets
from PySide6.QtCore import Qt

from snapvault import theme as palettes
from snapvault.branding import APP_NAME
from snapvault.inspector import ImageInspector
from test_auxiliary_glass import selected_image
from test_themes import app, flush, harness


class Hints:
    def __init__(self, current):
        self.current = current
        self.requests = []
        self.on_request = None

    def colorScheme(self):
        return self.current

    def setColorScheme(self, desired):
        self.requests.append(desired)
        self.current = desired
        if self.on_request is not None:
            self.on_request()


class FakeApplication:
    def __init__(self, initial):
        self.hints = Hints(initial)
        self.properties = {}

    def styleHints(self):
        return self.hints

    def property(self, name):
        return self.properties.get(name)

    def setProperty(self, name, value):
        self.properties[name] = value

    def platformName(self):
        return "windows"


@pytest.mark.parametrize("dark, initial", [(False, Qt.ColorScheme.Dark), (True, Qt.ColorScheme.Light),
                                           (False, Qt.ColorScheme.Light), (True, Qt.ColorScheme.Dark)])
def test_explicit_app_scheme_overrides_even_matching_startup_system_scheme(dark, initial):
    application = FakeApplication(initial)
    desired = Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light
    assert palettes._apply_app_color_scheme(application, dark) == desired
    assert application.hints.requests == [desired]
    assert application.property("snapvaultColorSchemeOverride") == ("dark" if dark else "light")
    # Native titlebar refreshes must not repeatedly reset Qt's palette/fonts.
    palettes._apply_app_color_scheme(application, dark)
    assert application.hints.requests == [desired]


def test_scheme_request_is_cached_before_synchronous_theme_notification():
    application = FakeApplication(Qt.ColorScheme.Dark)
    application.hints.on_request = lambda: palettes._apply_app_color_scheme(application, False)
    palettes._apply_app_color_scheme(application, False)
    assert application.hints.requests == [Qt.ColorScheme.Light]
    application.hints.on_request = None
    palettes._apply_app_color_scheme(application, True)
    assert application.hints.requests == [Qt.ColorScheme.Light, Qt.ColorScheme.Dark]


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.mark.parametrize("theme_id, dark", [("glass_day", False), ("glass_night", True)])
def test_native_caption_uses_readable_dwm_colors_and_repaints_owned_caption_children(monkeypatch, theme_id, dark):
    application = FakeApplication(Qt.ColorScheme.Dark if not dark else Qt.ColorScheme.Light)
    monkeypatch.setattr(QtWidgets, "QApplication", SimpleNamespace(instance=lambda: application))
    calls = []

    def set_attribute(hwnd, attr, pointer, size):
        assert size == 4
        value = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value
        calls.append(("attribute", hwnd.value, attr, value))
        return 0

    def repaint(hwnd, rect, region, flags):
        calls.append(("redraw", hwnd.value, flags))
        assert rect is None and region is None
        return True

    setter = Function(set_attribute)
    redraw = Function(repaint)
    libraries = {"dwmapi.dll": SimpleNamespace(DwmSetWindowAttribute=setter),
                 "user32.dll": SimpleNamespace(RedrawWindow=redraw)}

    def load(name, **kwargs):
        assert kwargs == {"winmode": 0x800}
        return libraries[name]

    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    handle = 0x123456789
    widget = SimpleNamespace(winId=lambda: handle)
    palettes.native_titlebar(widget, theme_id)
    p = palettes.theme(theme_id)
    colorref = lambda color: sum(channel << shift for channel, shift in zip(palettes._rgb(color), (0, 8, 16)))
    assert ("attribute", handle, 20, int(dark)) in calls
    assert ("attribute", handle, 35, colorref(p["surface"])) in calls
    assert ("attribute", handle, 36, colorref(p["text"])) in calls
    assert palettes._contrast(p["text"], p["surface"]) >= 4.5
    assert calls[-1] == ("redraw", handle, 0x0001 | 0x0080 | 0x0400)
    assert setter.argtypes[0] == ctypes.wintypes.HWND
    assert application.hints.colorScheme() == (Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)


def test_inspector_short_caption_and_elided_header_preserve_full_name_and_original(harness, app, monkeypatch):
    host = harness()
    row = selected_image(host)
    original = host.vault.images / row["filename"]
    before = sha256(original.read_bytes()).hexdigest()
    dialog = ImageInspector(host)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    full_name = "Synthetic_" + "很长的图片文件名包含日常项目说明_" * 45 + ".png"
    long_ocr = "这是不应该出现在原生标题栏里的整段识别内容" * 100
    shown = dict(row, filename=full_name, title=long_ocr)
    original_get = host.vault.get
    monkeypatch.setattr(host.vault, "get", lambda key: shown if key == row["id"] else original_get(key))
    # The already loaded original pixmap is retained while testing only the
    # metadata layout. No source path or library row is renamed/rewritten.
    dialog.refresh()
    dialog.resize(920, 580)
    dialog.show()
    flush(app)
    assert dialog.windowTitle().startswith(APP_NAME + " · 预览 · ")
    assert long_ocr not in dialog.windowTitle()
    assert len(dialog.windowTitle()) < len(full_name)
    assert dialog.fontMetrics().horizontalAdvance(dialog.windowTitle()) < dialog.width() - 180
    assert dialog.filename.toolTip() == full_name
    assert dialog.filename.accessibleName() == full_name
    assert dialog.filename.text() != full_name and "…" in dialog.filename.text()
    assert dialog.filename.fontMetrics().horizontalAdvance(dialog.filename.text()) <= dialog.filename.width()
    assert not dialog.filename.wordWrap()
    flags = dialog.windowFlags()
    assert flags & Qt.WindowType.WindowMinimizeButtonHint
    assert flags & Qt.WindowType.WindowMaximizeButtonHint
    assert flags & Qt.WindowType.WindowCloseButtonHint
    assert not flags & Qt.WindowType.FramelessWindowHint
    assert sha256(original.read_bytes()).hexdigest() == before
    assert original_get(row["id"]) == row
    dialog.reject()
    flush(app)


def test_missing_inspector_row_clears_previous_native_caption(harness, app):
    host = harness()
    selected_image(host)
    dialog = ImageInspector(host)
    assert " · 预览 · " in dialog.windowTitle()
    host.active_id = None
    dialog.refresh()
    assert dialog.windowTitle() == APP_NAME + " · 图片预览"
    assert dialog.filename.toolTip() == "这张截图已不在当前图库中"
    dialog.reject()
    flush(app)
