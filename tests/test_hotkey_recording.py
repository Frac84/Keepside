"""Explicit shortcut recording and release paths; no global input is registered."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from shiboken6 import isValid

from snapvault import preferences
from snapvault.hotkey_edit import HotkeyEdit
from snapvault.preferences import PreferencesPanel
from test_quick_privacy_ui import app, flush, make_window
from test_screenshot import clipboard, window


@pytest.fixture
def edit(app):
    widget = HotkeyEdit("Alt+Shift+V")
    widget.set_hotkey("Alt+Shift+V")
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    yield widget
    if isValid(widget):
        widget.close()
        widget.deleteLater()
    flush(app)


def test_focus_in_does_not_begin_recording_or_suspend_shortcuts(edit):
    states = []
    edit.recording.connect(states.append)
    for reason in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.ActiveWindowFocusReason,
                   Qt.FocusReason.OtherFocusReason):
        QCoreApplication.sendEvent(edit.field, QFocusEvent(QEvent.Type.FocusIn, reason))
    assert not edit.is_recording and states == []
    assert edit.field.text() == "Alt + Shift + V"


@pytest.mark.parametrize("trigger", ["click", "enter", "space"])
def test_only_explicit_actions_begin_recording(edit, trigger):
    states = []
    edit.recording.connect(states.append)
    if trigger == "click":
        QTest.mouseClick(edit.field, Qt.MouseButton.LeftButton)
    else:
        QTest.keyClick(edit.field, Qt.Key.Key_Return if trigger == "enter" else Qt.Key.Key_Space)
    assert edit.is_recording and states == [True]
    assert edit.recording_hint.isVisibleTo(edit)
    assert "暂时暂停" in edit.recording_hint.text() and not edit.message.isVisibleTo(edit)
    QTest.keyClick(edit.field, Qt.Key.Key_F9)
    assert not edit.is_recording and edit.hotkey() == "F9" and states == [True, False]


@pytest.mark.parametrize("action", ["clear", "default", "reload", "hide", "focus_out", "escape", "delete_later"])
def test_every_exit_from_recording_restores_shortcuts_and_clears_error(edit, app, action):
    states = []
    edit.recording.connect(states.append)
    edit.start_recording()
    QTest.keyClick(edit.field, Qt.Key.Key_A)
    assert edit.is_recording and "单独一个键" in edit.message.text()
    assert edit.hotkey() == "Alt+Shift+V"
    if action == "clear":
        edit.clear_button.click()
    elif action == "default":
        edit.default_button.click()
    elif action == "reload":
        edit.set_hotkey("Ctrl+Alt+V")
    elif action == "hide":
        edit.hide()
    elif action == "focus_out":
        QCoreApplication.sendEvent(edit.field, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason))
    elif action == "escape":
        QTest.keyClick(edit.field, Qt.Key.Key_Escape)
    else:
        edit.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert states == [True, False]
    if isValid(edit):
        assert not edit.is_recording and edit.message.text() == ""
        assert not edit.message.isVisibleTo(edit) and not edit.recording_hint.isVisibleTo(edit)
        assert edit.hotkey() == ("" if action == "clear" else "Ctrl+Alt+V" if action == "reload" else "Alt+Shift+V")


def test_disabled_shortcut_is_labeled_without_entering_record_mode(edit):
    edit.set_hotkey("")
    assert edit.hotkey() == "" and edit.field.text() == "未启用" and not edit.is_recording


@pytest.fixture
def panel(window, monkeypatch, app):
    monkeypatch.setattr(preferences.autostart, "supported", lambda: False)
    monkeypatch.setattr(preferences.autostart, "enabled", lambda: False)
    states = []
    def suspended(value):
        window._hotkeys_suspended = bool(value)
        states.append(bool(value))
    monkeypatch.setattr(window, "suspend_hotkeys", suspended)
    widget = PreferencesPanel(window)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    widget.pages.setCurrentIndex(3)
    flush(app)
    yield window, widget, states
    if isValid(widget):
        widget.hide()
        widget.deleteLater()
    flush(app)


def test_showing_shortcut_preferences_does_not_suspend_registered_actions(panel):
    _host, widget, states = panel
    assert states == []
    assert all(not edit.is_recording for edit, *_ in widget._hotkey_status_bindings)
    assert "未绑定" in widget.shot_hotkey.registration_status.text()


@pytest.mark.parametrize("action", ["tab", "hide", "reload", "cancel", "apply"])
def test_preferences_always_cancel_recording_before_leaving_or_saving(panel, app, action):
    host, widget, states = panel
    original = (host.full_hotkey, host.shot_hotkey, host.pin_hotkey, host.quick_hotkey)
    widget.quick_hotkey.start_recording()
    QTest.keyClick(widget.quick_hotkey.field, Qt.Key.Key_A)
    assert states == [True] and "暂停" in widget.full_hotkey.registration_status.text()
    if action == "tab":
        widget.pages.setCurrentIndex(0)
    elif action == "hide":
        widget.hide()
    elif action == "reload":
        widget.reload()
    elif action == "cancel":
        widget.cancel()
    else:
        assert widget.apply()
    flush(app)
    assert states == [True, False] and not host._hotkeys_suspended
    assert all(not edit.is_recording for edit, *_ in widget._hotkey_status_bindings)
    assert widget.quick_hotkey.message.text() == ""
    assert (host.full_hotkey, host.shot_hotkey, host.pin_hotkey, host.quick_hotkey) == original


def test_one_recorder_stopping_does_not_resume_while_another_is_active(panel):
    host, widget, states = panel
    widget.quick_hotkey.start_recording()
    widget.full_hotkey.start_recording()
    widget.quick_hotkey.stop_recording()
    assert host._hotkeys_suspended and states[-1] is True
    widget.full_hotkey.stop_recording()
    assert not host._hotkeys_suspended and states[-1] is False


def test_registration_status_distinguishes_active_pending_conflict_and_unbound(window, monkeypatch, app):
    # Simulate the existing host registration objects. Even teardown cannot
    # reach RegisterHotKey/UnregisterHotKey or a real native window.
    monkeypatch.setattr(preferences.autostart, "supported", lambda: False)
    monkeypatch.setattr(preferences.autostart, "enabled", lambda: False)
    for shortcut in (window.quick_key, window.pin_key, window.full_key, window.shot_key):
        def unregister(current=shortcut):
            current.hwnd = 0
        def register(hwnd, text, current=shortcut):
            current.text, current.error = text, ""
            current.hwnd = int(hwnd) if text else 0
            return bool(text)
        monkeypatch.setattr(shortcut, "unregister", unregister)
        monkeypatch.setattr(shortcut, "register", register)
    window.hotkeys_enabled = True
    window.apply_quick_settings()
    widget = PreferencesPanel(window)
    try:
        assert "已启用" in widget.full_hotkey.registration_status.text()
        assert "未绑定" in widget.shot_hotkey.registration_status.text()
        widget.quick_hotkey._set_value("Ctrl+Alt+Q")
        assert "待保存" in widget.quick_hotkey.registration_status.text()
        window.pin_key.hwnd = 0
        window.pin_key.error = "Alt + Shift + P 已被其他程序占用"
        window.hotkeys_state_changed.emit()
        assert "其他程序占用" in widget.pin_hotkey.registration_status.text()
        widget.full_hotkey.start_recording()
        assert all("暂停" in status.text() for _edit, status, *_ in widget._hotkey_status_bindings)
        widget.full_hotkey.stop_recording()
        assert "已启用" in widget.full_hotkey.registration_status.text()
        assert "待保存" in widget.quick_hotkey.registration_status.text()
        window.hotkeys_enabled = False
        window.hotkeys_state_changed.emit()
        assert widget.full_hotkey.registration_status.text() == "未注册"
    finally:
        widget.deleteLater()
        flush(app)
