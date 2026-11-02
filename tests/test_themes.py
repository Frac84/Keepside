"""Theme behavior with temporary data, inert workers and an inaccessible clipboard."""
from hashlib import sha256

from PIL import Image
import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QImage, QPalette
from PySide6.QtWidgets import QApplication

from snapvault import ui
from snapvault.branding import asset_path
from snapvault.inspector import ImageInspector
from snapvault.preferences import PreferencesPanel
from snapvault.storage import Vault
from snapvault.theme import apply_theme, theme
from test_fluent_ui import FakeCapture, FakeClipboard, FakeOcr, FakeScroller


THEME_IDS = (
    "light", "terminal", "charcoal", "paper", "blueberry", "blueprint", "morandi",
    "aurora", "nordic", "matcha", "windows_light", "windows_dark",
    "glass_day", "glass_night",
)
NEW_THEME_IDS = ("aurora", "nordic", "matcha", "windows_light", "windows_dark")


def test_requested_new_theme_color_distinctions_are_preserved():
    assert theme("nordic")["action"] == "#ee6c4d"
    assert theme("nordic")["accent"] == "#3d5a80"
    assert theme("aurora")["link_text"] == "#00b0ff"
    assert theme("matcha")["highlight"] == "#f5e6cc"
    assert theme("windows_light")["canvas"] == "#f3f3f3"
    assert theme("windows_dark")["canvas"] == "#202020"


def test_dropdown_chevrons_decode_without_an_svg_plugin(app):
    for name in ("chevron-light.png", "chevron-dark.png"):
        assert not QImage(str(asset_path(name))).isNull()


def flush(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    return application


@pytest.fixture
def harness(app, tmp_path, monkeypatch):
    clipboard = FakeClipboard()
    windows = []

    class IsolatedApplication:
        @staticmethod
        def clipboard():
            return clipboard

        @staticmethod
        def instance():
            return app

        @staticmethod
        def quit():
            pass

    monkeypatch.setattr(ui, "QApplication", IsolatedApplication)
    monkeypatch.setattr(ui.MainWindow, "sequence", staticmethod(lambda: 0))
    monkeypatch.setattr(ui, "CaptureWorker", FakeCapture)
    monkeypatch.setattr(ui, "OcrWorker", FakeOcr)
    monkeypatch.setattr(ui, "TabScroller", FakeScroller)

    def reject_clipboard(*_):
        raise AssertionError("A theme change must not inspect the clipboard")

    monkeypatch.setattr(ui, "should_exclude_clipboard", reject_clipboard)

    def create(vault=None, saved_theme="light"):
        if vault is None:
            vault = Vault(tmp_path / f"vault-{len(windows)}")
            vault.set_setting("theme", saved_theme)
            vault.set_setting("retention_days", 0)
            image_key, _ = vault.add(Image.new("RGB", (240, 160), "#e0b06c"))
            vault.finish_ocr(image_key, "SQLite API documentation")
            text_key, _ = vault.add_text("阅读笔记：保持原始文字。\r\n这一行也必须保留。\r\n")
            vault.set_category(text_key, "daily")
        window = ui.MainWindow(vault, listen=False, use_tray=False, browser_tools=False, classify=False)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        windows.append(window)
        flush(app)
        return window

    yield create
    for window in windows:
        if shiboken6.isValid(window):
            for inspector in window.findChildren(ImageInspector):
                inspector.close()
            window.quit_app()
            window.finish_quit()
            window.hide()
            window.deleteLater()
    flush(app)
    apply_theme(app, "light")


def choose(window, theme_id):
    window.show_page("preferences")
    panel = window.preferences_panel
    panel.pages.setCurrentIndex(2)
    panel.select_theme(theme_id)
    return panel


@pytest.mark.parametrize("theme_id", THEME_IDS)
def test_each_theme_previews_and_saves_then_restores_in_a_new_window(harness, app, theme_id):
    window = harness()
    panel = choose(window, theme_id)
    flush(app)
    assert window.theme_id == theme_id
    assert window.vault.setting("theme") == "light", "Preview must not persist"
    preview_style = app.styleSheet()
    preview_background = app.palette().color(QPalette.ColorRole.Window)
    assert panel.apply()
    assert window.vault.setting("theme") == theme_id

    reopened = harness(window.vault)
    assert reopened.theme_id == theme_id
    assert app.styleSheet() == preview_style
    assert app.palette().color(QPalette.ColorRole.Window) == preview_background
    reopened.show_page("preferences")
    assert reopened.preferences_panel.selected_theme_id() == theme_id
    assert reopened.preferences_panel.theme_preview.theme_id == theme_id


@pytest.mark.parametrize("theme_id", NEW_THEME_IDS)
def test_new_theme_preview_can_be_cancelled_back_to_an_existing_saved_theme(harness, app, theme_id):
    window = harness(saved_theme="morandi")
    saved_style = app.styleSheet()
    saved_color = app.palette().color(QPalette.ColorRole.Window)
    panel = choose(window, theme_id)
    flush(app)
    assert window.theme_id == theme_id
    assert app.styleSheet() != saved_style
    assert window.vault.setting("theme") == "morandi"
    panel.cancel()
    flush(app)
    assert window.theme_id == "morandi"
    assert window.vault.setting("theme") == "morandi"
    assert app.styleSheet() == saved_style
    assert app.palette().color(QPalette.ColorRole.Window) == saved_color


def test_dark_preview_updates_actual_application_colors_and_cancel_restores_saved_theme(harness, app):
    window = harness(saved_theme="paper")
    original_style = app.styleSheet()
    original_color = app.palette().color(QPalette.ColorRole.Window)
    panel = choose(window, "terminal")
    flush(app)
    assert window.theme_id == "terminal"
    assert app.styleSheet() != original_style
    assert app.palette().color(QPalette.ColorRole.Window).lightness() < original_color.lightness()
    assert window.vault.setting("theme") == "paper"

    panel.cancel()
    flush(app)
    assert window.page == "library"
    assert window.theme_id == "paper"
    assert app.styleSheet() == original_style
    assert app.palette().color(QPalette.ColorRole.Window) == original_color
    assert window.vault.setting("theme") == "paper"


@pytest.mark.parametrize("destination", ("library", "status", "preferences"))
def test_leaving_unsaved_preferences_discards_theme_preview(harness, app, destination):
    window = harness(saved_theme="morandi")
    choose(window, "blueprint")
    assert window.theme_id == "blueprint"
    window.show_page(destination)
    flush(app)
    assert window.theme_id == "morandi"
    assert window.vault.setting("theme") == "morandi"
    if destination == "preferences":
        assert window.preferences_panel.theme_selector.currentData() == "morandi"


def test_unknown_saved_theme_opens_usable_default_and_can_be_replaced(harness, app):
    window = harness(saved_theme="removed-or-corrupt-theme")
    assert window.theme_id == "light"
    panel = choose(window, "charcoal")
    assert panel.theme_selector.currentData() == "charcoal"
    assert panel.apply()
    reopened = harness(window.vault)
    assert reopened.theme_id == "charcoal"


def test_rejecting_modal_preferences_discards_its_preview(harness, app, monkeypatch):
    window = harness(saved_theme="paper")

    def reject_after_preview(dialog):
        panel = dialog.findChild(PreferencesPanel)
        assert panel is not None
        panel.theme_selector.setCurrentIndex(panel.theme_selector.findData("terminal"))
        assert window.theme_id == "terminal"
        dialog.reject()
        return ui.QDialog.DialogCode.Rejected

    monkeypatch.setattr(ui.QDialog, "exec", reject_after_preview)
    window.settings_dialog()
    flush(app)
    assert window.theme_id == "paper"
    assert window.vault.setting("theme") == "paper"


def test_closing_real_modal_preview_restores_saved_colors_without_persisting(harness, app):
    window = harness(saved_theme="paper")
    original_style = app.styleSheet()
    original_color = app.palette().color(QPalette.ColorRole.Window)
    failures = []
    visited = []

    def preview_then_reject():
        # Exercise QDialog.exec's real event loop instead of replacing exec.
        dialog = app.activeModalWidget()
        try:
            assert isinstance(dialog, ui.QDialog)
            panel = dialog.findChild(PreferencesPanel)
            assert panel is not None
            panel.pages.setCurrentIndex(2)
            panel.theme_selector.setCurrentIndex(panel.theme_selector.findData("terminal"))
            assert window.theme_id == "terminal"
            assert window.vault.setting("theme") == "paper"
            assert app.styleSheet() != original_style
            visited.append(True)
        except BaseException as error:
            failures.append(error)
        finally:
            # The title-bar close action rejects this unsaved modal as well.
            if isinstance(dialog, ui.QDialog):
                dialog.reject()

    QTimer.singleShot(0, preview_then_reject)
    window.settings_dialog()
    flush(app)
    assert not failures, str(failures)
    assert visited == [True]
    assert window.theme_id == "paper"
    assert window.vault.setting("theme") == "paper"
    assert app.styleSheet() == original_style
    assert app.palette().color(QPalette.ColorRole.Window) == original_color


def test_switching_every_theme_keeps_content_category_search_and_image_bytes(harness, app):
    window = harness()
    vault = window.vault
    original_rows = vault.query(limit=30)
    original_files = {path.name: sha256(path.read_bytes()).hexdigest() for path in vault.images.iterdir()}
    selected = window.active_id
    original_capture_jobs = list(window.capture.jobs)
    original_ocr_queue = list(window.ocr.queued)
    for theme_id in THEME_IDS:
        window.set_theme(theme_id, persist=False)
        flush(app)
        assert window.active_id == selected
        assert vault.query(limit=30) == original_rows
        assert vault.query(search="原始文字")[0]["category"] == "daily"
    assert {path.name: sha256(path.read_bytes()).hexdigest() for path in vault.images.iterdir()} == original_files
    assert window.capture.jobs == original_capture_jobs
    assert window.ocr.queued == original_ocr_queue
    assert vault.setting("theme") == "light"


def test_open_inspector_and_ownerless_pin_update_without_changing_original_pixels(harness, app):
    window = harness()
    image_index = next(index for index in range(window.gallery.count())
                       if window.gallery.item(index).data(Qt.ItemDataRole.UserRole)["item_type"] == "image")
    window.gallery.setCurrentRow(image_index)
    inspector = ImageInspector(window)
    inspector.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    inspector.show()
    pin = window.pin_image()
    assert pin is not None and pin.parent() is None
    pin.set_scale(0.75)
    pin.set_opacity(0.6)
    original_inspector_style = inspector.styleSheet()
    original_pin_style = pin.toolbar.styleSheet()
    original_image = pin._pixmap.toImage().copy()
    window.set_theme("terminal", persist=False)
    flush(app)
    assert inspector.styleSheet() != original_inspector_style
    assert pin.toolbar.styleSheet() != original_pin_style
    assert pin._pixmap.toImage() == original_image
    assert pin.scale_factor == pytest.approx(0.75)
    assert pin.opacity == pytest.approx(0.6)
    inspector.close()
    pin.close()
    flush(app)
