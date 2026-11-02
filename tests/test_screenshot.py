"""Hotkey screen capture with synthetic frozen screens; nothing is grabbed for real."""
import ctypes
from ctypes import wintypes
import os

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
import pytest

from snapvault import hotkey, ui
from snapvault.preferences import PreferencesPanel
from snapvault.screenshot import ScreenshotOverlay, ScreenshotSession
from test_quick_privacy_ui import app, flush, make_window  # noqa: F401  (fixtures)
from test_v160_ui import PictureClipboard, QueueOcr, TaggingCapture  # noqa: F401


@pytest.fixture
def clipboard():
    return PictureClipboard()


def frozen(width=800, height=600):
    """A 2× screen: left half red, right half blue, in device pixels."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("red"))
    painter = QPainter(pixmap)
    painter.fillRect(width // 2, 0, width // 2, height, QColor("blue"))
    painter.end()
    return pixmap


@pytest.fixture
def overlay(app):
    widget = ScreenshotOverlay(QRect(0, 0, 400, 300), frozen())
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    flush(app)
    yield widget
    widget.close()
    flush(app)


def test_crop_uses_full_device_resolution(overlay):
    overlay.set_selection(QRect(10, 20, 100, 50))
    image = overlay.crop()
    assert (image.width(), image.height()) == (200, 100)
    assert image.pixelColor(5, 5) == QColor("red")
    overlay.set_selection(QRect(250, 0, 100, 100))
    assert overlay.crop().pixelColor(5, 5) == QColor("blue")
    overlay.set_selection(QRect(380, 280, 100, 100))  # clipped to the screen
    assert (overlay.crop().width(), overlay.crop().height()) == (40, 40)


def test_drag_click_and_right_click(overlay):
    results, cancels = [], []
    overlay.finished.connect(lambda image, action: results.append((image.size().toTuple(), action)))
    overlay.cancelled.connect(lambda: cancels.append(True))
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(20, 30))
    QTest.mouseMove(overlay, QPoint(120, 80))
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(120, 80))
    assert abs(overlay.selection.width() - 100) <= 1 and abs(overlay.selection.height() - 50) <= 1
    assert overlay.toolbar.isVisible()
    toolbar = overlay.toolbar.geometry()
    assert overlay.rect().contains(toolbar) and not toolbar.intersects(overlay.selection)
    QTest.mouseClick(overlay, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(50, 50))
    assert overlay.selection.isEmpty() and not cancels
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
    assert overlay.selection == overlay.rect()  # a click takes the whole screen
    toolbar = overlay.toolbar.geometry()
    assert overlay.rect().contains(toolbar)
    QTest.mouseDClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
    assert results[-1] == ((800, 600), "copy")
    overlay.clear_selection()
    QTest.mouseClick(overlay, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(50, 50))
    assert cancels == [True]


def test_keyboard_actions(overlay):
    results, cancels = [], []
    overlay.finished.connect(lambda image, action: results.append(action))
    overlay.cancelled.connect(lambda: cancels.append(True))
    QTest.keyClick(overlay, Qt.Key.Key_P)  # nothing selected yet: no pin
    assert results == []
    QTest.keyClick(overlay, Qt.Key.Key_Return)  # Enter without a selection takes the screen
    overlay.set_selection(QRect(0, 0, 50, 50))
    QTest.keyClick(overlay, Qt.Key.Key_P)
    QTest.keyClick(overlay, Qt.Key.Key_Escape)
    assert results == ["copy", "pin"] and cancels == [True]
    overlay.grab()  # paints selection, size label and hint without errors
    overlay.clear_selection()
    overlay.grab()


def test_session_spans_screens_and_finishes_once(app):
    session = ScreenshotSession([(QRect(0, 0, 400, 300), frozen()), (QRect(400, 0, 300, 300), frozen(300, 300))])
    for overlay in session.overlays:
        overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    captured, ended = [], []
    session.captured.connect(lambda image, action: captured.append((image.size().toTuple(), action)))
    session.ended.connect(lambda: ended.append(True))
    assert session.start()
    first, second = session.overlays
    first.set_selection(QRect(0, 0, 40, 40))
    second.selecting.emit(second)  # starting on the other screen clears this one
    assert first.selection.isEmpty()
    second.set_selection(QRect(0, 0, 30, 30))
    second.finish("pin")
    second.finish("copy")
    assert captured == [((30, 30), "pin")] and ended == [True] and session.overlays == []
    flush(app)


@pytest.fixture
def window(make_window, monkeypatch):
    monkeypatch.setattr(ui, "CaptureWorker", TaggingCapture)
    monkeypatch.setattr(ui, "OcrWorker", QueueOcr)
    created = make_window()
    created.grab_screens = lambda: [(QRect(0, 0, 400, 300), frozen())]
    return created


def overlays_of(window):
    return window.shot_session.overlays


def test_copy_action_saves_and_copies(window, clipboard, app):
    assert window.start_screenshot()
    assert not window.start_screenshot()  # one capture at a time
    overlay = overlays_of(window)[0]
    overlay.set_selection(QRect(0, 0, 100, 50))
    overlay.finish("copy")
    flush(app)
    assert window.shot_session is None
    assert clipboard.copied[-1] == ("image", (200, 100))
    kind, image, tag = window.capture.jobs[-1]
    assert kind == "image" and image.size().toTuple() == (200, 100) and tag is None
    assert "已复制到剪贴板" in window.statusBar().currentMessage()


def test_pin_action_pins_and_links_after_saving(window, app):
    window.start_screenshot()
    overlay = overlays_of(window)[0]
    overlay.set_selection(QRect(200, 0, 100, 100))
    overlay.finish("pin")
    flush(app)
    pin = window.pinned_windows[-1]
    assert pin.source_image().size().toTuple() == (200, 200) and pin.source_image().pixelColor(5, 5) == QColor("blue")
    kind, image, tag = window.capture.jobs[-1]
    assert kind == "image" and tag is not None


def test_cancel_restores_the_window_when_asked(window, app, monkeypatch):
    shown = []
    monkeypatch.setattr(window, "show_window", lambda: shown.append(True))
    window.start_screenshot(restore=True)
    overlays_of(window)[0].cancel()
    flush(app)
    assert window.shot_session is None and shown == [True]
    window.grab_screens = lambda: []
    assert not window.start_screenshot(restore=True)
    assert shown == [True, True] and "无法截取" in window.statusBar().currentMessage()


def test_screenshot_hotkey_message_and_setting(window, app, monkeypatch):
    if os.name == "nt":
        calls = []
        monkeypatch.setattr(window, "start_screenshot", lambda *args, **kwargs: calls.append(True))
        window.shot_key.hwnd = 1
        monkeypatch.setattr(window.shot_key, "unregister", lambda: None)
        message = wintypes.MSG()
        message.message, message.wParam = hotkey.WM_HOTKEY, ui.SHOT_HOTKEY_ID
        assert window.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message)) == (True, 0)
        flush(app)
        assert calls == [True]
    window.apply_quick_settings = lambda: None
    panel = PreferencesPanel(window)
    assert panel.shot_hotkey.hotkey() == hotkey.DEFAULT_REGION_HOTKEY == ""
    panel.shot_hotkey.set_hotkey("Ctrl+Alt+A")
    assert panel.apply()
    assert window.vault.setting("region_hotkey") == "Ctrl+Alt+A" == window.shot_hotkey
    panel.deleteLater()
    flush(app)


def test_fullscreen_hotkey_saves_and_copies_without_selection(window, clipboard, app):
    window.grab_fullscreen = lambda: frozen(1000, 500)
    image = window.take_fullscreen()
    assert image.size().toTuple() == (1000, 500) and window.shot_session is None  # no overlay at all
    assert clipboard.copied[-1] == ("image", (1000, 500))
    kind, saved, tag = window.capture.jobs[-1]
    assert kind == "image" and saved.size().toTuple() == (1000, 500) and tag is None
    assert window.toast.isVisible() and "已截取全屏 1000×500" in window.toast.text
    window.toast.grab()
    window.toast.close()
    window.grab_fullscreen = lambda: None
    assert window.take_fullscreen() is None and "无法截取" in window.statusBar().currentMessage()
    flush(app)


def test_fullscreen_hotkey_message(window, app, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows message format")
    calls = []
    monkeypatch.setattr(window, "take_fullscreen", lambda: calls.append(True))
    window.full_key.hwnd = 1
    monkeypatch.setattr(window.full_key, "unregister", lambda: None)
    message = wintypes.MSG()
    message.message, message.wParam = hotkey.WM_HOTKEY, ui.FULL_HOTKEY_ID
    assert window.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message)) == (True, 0)
    flush(app)
    assert calls == [True]


def test_fullscreen_settings_printscreen_and_duplicates(window, app):
    window.apply_quick_settings = lambda: None
    panel = PreferencesPanel(window)
    assert panel.full_hotkey.hotkey() == hotkey.DEFAULT_FULLSCREEN_HOTKEY == "Alt+Shift+S"
    panel.full_hotkey.set_hotkey("PrintScreen")
    assert panel.apply()
    assert window.vault.setting("fullscreen_hotkey") == "PrintScreen" == window.full_hotkey
    assert hotkey.parse_hotkey("PrintScreen") == (0, 0x2C)
    panel.shot_hotkey.set_hotkey("Ctrl+Alt+A")
    panel.pin_hotkey.set_hotkey("Ctrl+Alt+P")
    panel.quick_hotkey.set_hotkey("Ctrl+Alt+V")
    panel.full_hotkey.set_hotkey("Ctrl+Alt+S")
    assert panel.apply()
    panel.shot_hotkey.set_hotkey("Ctrl+Alt+S")
    assert not panel.apply()  # the same shortcut for two features is refused
    assert "被设置给了多个功能" in window.statusBar().currentMessage()
    assert window.vault.setting("region_hotkey") == "Ctrl+Alt+A"
    panel.deleteLater()
    flush(app)


def test_old_region_shortcut_setting_is_migrated(make_window, monkeypatch, tmp_path):
    from snapvault.storage import Vault
    monkeypatch.setattr(ui, "CaptureWorker", TaggingCapture)
    monkeypatch.setattr(ui, "OcrWorker", QueueOcr)
    default = Vault(tmp_path / "default")
    default.set_setting("screenshot_hotkey", "Alt+Shift+S")  # 1.6.0 default now means full screen
    custom = Vault(tmp_path / "custom")
    custom.set_setting("screenshot_hotkey", "Ctrl+Alt+A")
    assert make_window(default).shot_hotkey == "" and make_window(default).full_hotkey == "Alt+Shift+S"
    assert make_window(custom).shot_hotkey == "Ctrl+Alt+A"


def test_any_key_or_combination_can_be_recorded(app):
    from snapvault.hotkey_edit import HotkeyEdit
    edit = HotkeyEdit("Alt+Shift+S")
    edit.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    edit.show()
    states, saved = [], []
    edit.recording.connect(states.append)
    edit.changed.connect(saved.append)

    def press(key, modifiers=Qt.KeyboardModifier.NoModifier):
        edit.start_recording()
        QTest.keyClick(edit.field, key, modifiers)

    press(Qt.Key.Key_F9)
    assert edit.hotkey() == "F9" and edit.field.text() == "F9" and not edit.is_recording
    press(Qt.Key.Key_F12, Qt.KeyboardModifier.ControlModifier)
    assert edit.hotkey() == "Ctrl+F12"
    press(Qt.Key.Key_Q, Qt.KeyboardModifier.AltModifier)
    assert edit.hotkey() == "Alt+Q" and edit.field.text() == "Alt + Q"
    press(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier)
    assert edit.hotkey() == "Ctrl+Alt+Shift+S"
    edit.start_recording()
    QTest.keyRelease(edit.field, Qt.Key.Key_Print)  # Windows reports Print Screen on release
    assert edit.hotkey() == "PrintScreen" and edit.field.text() == "PrtSc"
    assert saved == ["F9", "Ctrl+F12", "Alt+Q", "Ctrl+Alt+Shift+S", "PrintScreen"]
    # A plain letter would stop that letter from being typed anywhere.
    press(Qt.Key.Key_A)
    assert edit.hotkey() == "PrintScreen" and edit.is_recording and edit.message.isVisibleTo(edit)
    assert "单独一个键" in edit.message.text()
    QTest.keyClick(edit.field, Qt.Key.Key_A, Qt.KeyboardModifier.ShiftModifier)
    assert edit.hotkey() == "PrintScreen" and "Shift" in edit.message.text()
    QTest.keyPress(edit.field, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
    assert edit.field.text() == "Ctrl + …" and edit.is_recording
    QTest.keyClick(edit.field, Qt.Key.Key_Escape)  # cancel keeps the previous key
    assert not edit.is_recording and edit.hotkey() == "PrintScreen"
    edit.clear_button.click()
    assert edit.hotkey() == "" and "未设置" in edit.field.placeholderText()
    edit.default_button.click()
    assert edit.hotkey() == "Alt+Shift+S"
    assert states.count(True) == states.count(False)  # every recording ended
    edit.close()


def test_recording_releases_and_restores_global_shortcuts(window, app):
    calls = []
    window.hotkeys_enabled = True
    for name in ("quick_key", "pin_key", "full_key", "shot_key"):
        getattr(window, name).unregister = lambda name=name: calls.append(("off", name))
    window.apply_quick_settings = lambda report=True: calls.append(("on", report))
    window.suspend_hotkeys(True)
    window.suspend_hotkeys(False)
    assert calls == [("off", "quick_key"), ("off", "pin_key"), ("off", "full_key"), ("off", "shot_key"), ("on", False)]
    panel = PreferencesPanel(window)
    calls.clear()
    panel.full_hotkey.start_recording()
    QTest.keyClick(panel.full_hotkey.field, Qt.Key.Key_F8)
    assert panel.full_hotkey.hotkey() == "F8" and calls[0][0] == "off" and calls[-1] == ("on", False)
    panel.deleteLater()
    flush(app)


def test_registration_problems_are_reported(window, monkeypatch):
    window.hotkeys_enabled = True

    class User:
        def RegisterHotKey(self, hwnd, ident, modifiers, key):
            return key != 0x77  # F8 is taken by another program

        def UnregisterHotKey(self, hwnd, ident):
            return True

    monkeypatch.setattr(hotkey.GlobalHotkey, "_user32", staticmethod(lambda: User()))
    window.full_hotkey = "F8"
    errors = window.apply_quick_settings()
    assert errors == ["全屏截图：F8 已被其他程序占用，请在设置中更换。"]
    assert "F8 已被其他程序占用" in window.statusBar().currentMessage()
    for shortcut in (window.quick_key, window.pin_key, window.full_key, window.shot_key):
        shortcut.hwnd = 0
