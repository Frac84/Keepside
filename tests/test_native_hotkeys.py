"""Exercise the actual Qt pointer shape; no global keys or input are sent."""
import ctypes
from ctypes import wintypes
import os

import pytest
from shiboken6 import VoidPtr

from snapvault import hotkey, ui
from test_screenshot import app, clipboard, window  # noqa: F401
from test_quick_privacy_ui import flush, make_window  # noqa: F401


@pytest.mark.skipif(os.name != "nt", reason="Windows MSG ABI")
@pytest.mark.parametrize("ident", [ui.QUICK_HOTKEY_ID, ui.PIN_HOTKEY_ID, ui.FULL_HOTKEY_ID, ui.SHOT_HOTKEY_ID])
def test_unsized_native_pointer_dispatches_each_hotkey(window, app, monkeypatch, ident):
    calls = []
    bindings = {ui.QUICK_HOTKEY_ID: (window.quick_key, "toggle_quick_paste"),
                ui.PIN_HOTKEY_ID: (window.pin_key, "pin_clipboard"),
                ui.FULL_HOTKEY_ID: (window.full_key, "take_fullscreen"),
                ui.SHOT_HOTKEY_ID: (window.shot_key, "start_screenshot")}
    shortcut, method = bindings[ident]
    shortcut.hwnd = int(window.winId())
    monkeypatch.setattr(shortcut, "unregister", lambda: None)
    monkeypatch.setattr(window, method, lambda: calls.append(method))
    message = wintypes.MSG()
    message.message, message.wParam = hotkey.WM_HOTKEY, ident
    pointer = VoidPtr(ctypes.addressof(message))
    assert int(pointer) == ctypes.addressof(message)
    assert hotkey.hotkey_message_id(pointer) == ident
    assert window.nativeEvent(b"windows_generic_MSG", pointer) == (True, 0)
    flush(app)
    assert calls == [method]
    shortcut.hwnd = 0


def test_null_and_non_hotkey_messages_are_ignored():
    assert hotkey.hotkey_message_id(None) is None
    assert hotkey.hotkey_message_id(0) is None
    if os.name == "nt":
        message = wintypes.MSG()
        message.message = 0x0400
        assert hotkey.hotkey_message_id(VoidPtr(ctypes.addressof(message))) is None


def test_recording_suspension_cannot_be_bypassed_by_reapplying_settings(window, monkeypatch):
    calls = []
    window.hotkeys_enabled = True
    for key in (window.quick_key, window.pin_key, window.full_key, window.shot_key):
        monkeypatch.setattr(key, "register", lambda *_: calls.append("register"))
        monkeypatch.setattr(key, "unregister", lambda: calls.append("unregister"))
    window.suspend_hotkeys(True)
    window.apply_quick_settings()
    assert "register" not in calls
    window.suspend_hotkeys(False)
    assert calls.count("register") == 4


def test_handle_replacement_restores_registration_but_never_during_recording(window, monkeypatch):
    window.hotkeys_enabled = True
    window.quick_key.hwnd = int(window.winId()) + 1
    restored = []
    monkeypatch.setattr(window, "apply_quick_settings", lambda **kwargs: restored.append(kwargs))
    monkeypatch.setattr(window.quick_key, "unregister", lambda: None)
    window._restore_hotkey_window()
    assert restored == [{"report": False}]
    window._hotkeys_suspended = True
    window._restore_hotkey_window()
    assert len(restored) == 1
    window.quick_key.hwnd = 0
