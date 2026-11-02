"""Unified controls verified without reading the clipboard or sending input."""
import time

from PySide6.QtWidgets import QApplication
import pytest

from snapvault.storage import Vault
from snapvault import ui


class FakeScroller:
    def __init__(self, app, hwnd, error_callback=None):
        self.active = False
        self.error = ""
        self.error_callback = error_callback
        self.calls = []
        self.stopped = False

    def configure(self, **options):
        self.calls.append(options)
        self.active = options["enabled"]
        return True

    def stop(self):
        self.stopped = True
        self.active = False


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "TabScroller", FakeScroller)
    vault = Vault(tmp_path / "vault")
    vault.set_setting("retention_days", 0)
    instance = ui.MainWindow(vault, listen=False, use_tray=False, classify=False)
    yield instance
    instance.quit_app()
    deadline = time.monotonic() + 8
    while (instance.capture.isRunning() or instance.ocr.isRunning()) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not instance.capture.isRunning()
    assert not instance.ocr.isRunning()
    app.processEvents()
    instance.deleteLater()
    app.processEvents()


def test_controls_are_independent_and_preferences_persist(window):
    assert window.tab_scroller.active
    assert not window.listening
    window.toggle_pause()
    assert window.listening and window.tab_scroller.active
    window.toggle_tabs()
    assert window.listening and not window.tab_scroller.active
    window.toggle_pause()
    assert not window.listening and not window.tab_scroller.active
    window.toggle_tabs()
    assert not window.listening and window.tab_scroller.active
    window.set_tabs_enabled(False)
    reopened = Vault(window.vault.root)
    assert reopened.setting("tabs_enabled") is False
    assert reopened.setting("paused") is True


def test_runtime_failure_updates_ui_without_restarting(window):
    calls_before = len(window.tab_scroller.calls)
    window.tab_scroller.active = False
    window.tab_scroller.error = "Synthetic native input failure"
    window.on_tab_error(window.tab_scroller.error)
    assert "不可用" in window.tabs_button.text()
    assert len(window.tab_scroller.calls) == calls_before
    assert window.vault.stats()["total"] == 0


def test_quit_stops_browser_input_before_screenshot_workers(window):
    service = window.tab_scroller
    window.quit_app()
    assert window.quitting
    assert service.stopped and not service.active
    assert not window.listening
