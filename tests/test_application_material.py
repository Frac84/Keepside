"""Application material behavior with synthetic data and mocked native effects.

No Windows settings, clipboard, user library, or biometric dialog is accessed.
Actual compositor blur remains a separate visual QA step.
"""
from hashlib import sha256

from PIL import Image
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QDialog, QMainWindow, QWidget

from snapvault import glass, hotkey, recent_panel
from snapvault.storage import Vault
from test_themes import app, harness


def flush(application):
    application.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()


@pytest.mark.parametrize("window_type", [QMainWindow, QDialog])
def test_preparing_glass_retains_native_window_identity_and_controls(app, window_type):
    window = window_type()
    window.setWindowTitle("Synthetic native-frame material")
    window.resize(640, 420)
    original_flags = window.windowFlags()
    assert not window.testAttribute(Qt.WidgetAttribute.WA_WState_Created)

    glass.configure_window_glass(window)

    flags = window.windowFlags()
    assert flags & Qt.WindowType.WindowType_Mask == original_flags & Qt.WindowType.WindowType_Mask
    for control in (Qt.WindowType.WindowMinimizeButtonHint, Qt.WindowType.WindowMaximizeButtonHint,
                    Qt.WindowType.WindowCloseButtonHint):
        assert bool(flags & control) == bool(original_flags & control)
    assert not flags & Qt.WindowType.FramelessWindowHint
    assert not window.testAttribute(Qt.WidgetAttribute.WA_WState_Created), "Configure before first HWND"
    assert window.windowOpacity() == 1.0
    assert window.windowTitle() == "Synthetic native-frame material"
    assert window.width() == 640 and window.height() == 420
    window.deleteLater()
    flush(app)


def test_native_success_and_fallback_update_the_same_visible_surface(app, monkeypatch):
    window = QMainWindow()
    root = QWidget()
    window.setCentralWidget(root)
    glass.configure_window_glass(window)
    calls = []
    enabled = [True]

    def native(widget, dark=False):
        calls.append((widget, dark))
        return dict(enabled=enabled[0], method="synthetic" if enabled[0] else "fallback",
                    reason="controlled material result")

    monkeypatch.setattr(glass, "apply_glass", native)
    result = glass.refresh_window_glass(window, dark=False, root=root)
    assert result["enabled"]
    assert window.property("appGlass") is True
    assert window.property("nativeGlass") is True
    assert root.property("nativeGlass") is True
    assert window.glass_state == result

    enabled[0] = False
    result = glass.refresh_window_glass(window, dark=True, root=root)
    assert result == dict(enabled=False, method="fallback", reason="controlled material result")
    assert window.property("nativeGlass") is False
    assert root.property("nativeGlass") is False
    assert window.glass_state == result
    assert window.windowOpacity() == 1.0, "Fallback must retain readable opaque foregrounds"
    assert calls == [(window, False), (window, True)]
    window.deleteLater()
    flush(app)


def test_refresh_without_a_separate_root_supports_modal_surfaces(app, monkeypatch):
    dialog = QDialog()
    glass.configure_window_glass(dialog)
    monkeypatch.setattr(glass, "apply_glass", lambda widget, dark=False: dict(
        enabled=False, method="fallback", reason="system transparency disabled"))
    result = glass.refresh_window_glass(dialog, dark=True)
    assert not result["enabled"]
    assert dialog.property("appGlass") is True
    assert dialog.property("nativeGlass") is False
    assert dialog.glass_state == result
    assert dialog.windowOpacity() == 1.0
    dialog.deleteLater()
    flush(app)


def test_native_material_keeps_restored_caption_on_its_screen(app, monkeypatch):
    window = QMainWindow()
    glass.configure_window_glass(window)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(640, 420)
    window.show()
    flush(app)
    window.move(20, -40)
    monkeypatch.setattr(glass, "_qt_platform", lambda: "windows")
    window._window_material_controller.keep_caption_visible()
    assert window.frameGeometry().top() >= window.screen().availableGeometry().top()
    position = window.pos()
    window._window_material_controller.keep_caption_visible()
    assert window.pos() == position
    window.deleteLater()
    flush(app)


def test_main_window_reapplies_material_after_theme_and_restore(harness, app, monkeypatch):
    calls = []

    def native(widget, dark=False):
        calls.append((widget, dark))
        return dict(enabled=False, method="fallback", reason="synthetic offscreen QA")

    monkeypatch.setattr(glass, "apply_glass", native)
    window = harness(saved_theme="glass_day")
    assert any(target is window and dark is False for target, dark in calls)
    assert window.property("appGlass") is True
    assert window.property("nativeGlass") is False
    assert window.windowOpacity() == 1.0
    native_handle = int(window.winId())
    calls.clear()
    window.set_theme("glass_night", persist=False)
    flush(app)
    assert any(target is window and dark is True for target, dark in calls)
    assert int(window.winId()) == native_handle, "Theme changes must retain registered hotkey HWNDs"
    selected = window.active_id
    window.showMaximized()
    flush(app)
    assert window.isMaximized()
    window.showNormal()
    flush(app)
    assert not window.isMaximized()
    assert window.active_id == selected
    assert int(window.winId()) == native_handle
    assert window.windowOpacity() == 1.0
    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
    calls.clear()
    window.set_theme("light", persist=False)
    flush(app)
    assert not any(target is window for target, _ in calls)
    assert window.glass_state["reason"] == "theme-disabled"
    window._window_material_controller.refresh()
    assert not any(target is window for target, _ in calls), "Lifecycle must retain the opaque-theme choice"


def test_material_switch_keeps_manual_recent_lock_and_collection_choices(
        harness, app, tmp_path, monkeypatch):
    vault = Vault(tmp_path / "material-state-vault")
    vault.set_setting("retention_days", 0)
    vault.set_setting("recent_files_locked_v1", True)
    vault.set_setting("collect_text", False)
    vault.set_setting("paused", True)
    key, _ = vault.add(Image.new("RGB", (80, 50), "#7DABC7"))
    vault.finish_ocr(key, "Synthetic protected content")
    vault.add_text("Synthetic text remains exact\r\n  indented line\r\n")
    original_rows = vault.query(limit=30)
    original_images = {path.name: sha256(path.read_bytes()).hexdigest() for path in vault.images.iterdir()}
    monkeypatch.setattr(glass, "apply_glass", lambda widget, dark=False: dict(
        enabled=bool(dark), method="synthetic", reason="test both material policies"))
    monkeypatch.setattr(hotkey, "foreground_window", lambda: 0)

    def no_authentication(*_args, **_kwargs):
        raise AssertionError("A material or theme change must not ask to authenticate")

    monkeypatch.setattr(recent_panel.windows_hello, "availability", no_authentication)
    monkeypatch.setattr(recent_panel.windows_hello, "verify", no_authentication)
    window = harness(vault)
    panel = recent_panel.ShelfPanel(window)
    window.quick_panel = panel
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    panel.keep_open.setChecked(True)
    panel.open(files=True)
    assert panel.files_locked and panel.file_results.count() == 0
    capture_jobs = list(window.capture.jobs)

    for theme_id in ("glass_day", "glass_night", "terminal", "light", "windows_dark", "windows_light"):
        window.set_theme(theme_id, persist=False)
        flush(app)
        assert panel.files_locked and panel.file_results.count() == 0
        assert vault.setting("recent_files_locked_v1") is True
        assert vault.setting("collect_text") is False
        assert vault.setting("paused") is True
        assert not window.listening and not window.collect_text
        assert vault.query(limit=30) == original_rows
        assert window.capture.jobs == capture_jobs
    assert {path.name: sha256(path.read_bytes()).hexdigest() for path in vault.images.iterdir()} == original_images
