"""Native UI behavior with synthetic files and fake workers/input services.

No system clipboard, browser engine, mouse registration, or key injection is used.
"""
from datetime import datetime, timedelta
import threading

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QListWidgetItem
import pytest
import shiboken6

from snapvault import ui
from snapvault.gallery import GalleryView, ThumbnailCache
from snapvault.inspector import ImageInspector
from snapvault.preferences import PreferencesPanel
from snapvault.storage import Vault


class FakeClipboard(QObject):
    dataChanged = Signal()

    def mimeData(self):
        raise AssertionError("These tests must not read clipboard contents")

    def setImage(self, image):
        raise AssertionError("These tests must not change the clipboard")

    def setText(self, text):
        raise AssertionError("These tests must not change the clipboard")


class FakeCapture(QObject):
    saved = Signal(str, bool)
    failed = Signal(str)
    cleaned = Signal(int)

    def __init__(self, vault):
        super().__init__()
        self.jobs = []

    def start(self):
        pass

    def isRunning(self):
        return False

    def submit(self, kind, value=None):
        self.jobs.append((kind, value))
        return True


class FakeOcr(QObject):
    changed = Signal(str)
    availability = Signal(object, str)

    def __init__(self, vault):
        super().__init__()
        self.stopping = threading.Event()
        self.queued = []

    def start(self):
        pass

    def isRunning(self):
        return False

    def enqueue(self, key):
        self.queued.append(key)


class FakeScroller(QObject):
    state_changed = Signal()

    def __init__(self, app, hwnd, error_callback=None):
        super().__init__()
        self.active = self.starting = False
        self.error = None
        self.calls = []

    def configure(self, **options):
        self.calls.append(options)
        self.active = options["enabled"]
        self.state_changed.emit()
        return True

    def stop(self):
        self.active = False


def flush(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    application.setStyleSheet(ui.STYLE)
    return application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    clipboard = FakeClipboard()

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
    vault = Vault(tmp_path / "vault")
    vault.set_setting("retention_days", 0)
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    ids = {}
    for name, color, stamp, text, error in (
        ("new", "red", now, "Needle alpha", ""),
        ("favorite", "blue", now-timedelta(minutes=1), "Needle beta", ""),
        ("pending", "green", now-timedelta(minutes=2), None, ""),
        ("yesterday", "yellow", now-timedelta(days=1), "Needle yesterday", ""),
        ("error", "purple", now-timedelta(minutes=3), "", "Synthetic OCR failure"),
    ):
        key, _ = vault.add(Image.new("RGB", (300, 180), color), stamp)
        ids[name] = key
        if text is not None:
            vault.finish_ocr(key, text, error)
    vault.toggle_favorite(ids["favorite"])
    instance = ui.MainWindow(vault, listen=False, use_tray=False, classify=False)
    instance.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    instance.synthetic_ids = ids
    instance.show()
    flush(app)
    yield instance
    instance.quit_app()
    instance.finish_quit()
    instance.hide()
    instance.deleteLater()
    flush(app)


def visible_ids(window):
    return [window.gallery.item(i).data(Qt.ItemDataRole.UserRole)["id"]
            for i in range(window.gallery.count())]


def test_gallery_chips_search_scope_and_sort_use_real_storage(window):
    window.search.setText("needle")
    window.search_timer.stop()
    window.set_scope("today")
    window.set_kind("text")
    window.sort_box.setCurrentIndex(window.sort_box.findData("oldest"))
    window.refresh()
    assert visible_ids(window) == [window.synthetic_ids["favorite"], window.synthetic_ids["new"]]
    window.set_kind("pending")
    assert window.gallery.count() == 0
    assert not window.active_id
    assert all(not action.isEnabled() for action in window.detail_buttons)
    window.search.clear()
    window.search_timer.stop()
    window.refresh()
    assert visible_ids(window) == [window.synthetic_ids["pending"]]
    assert window.active_id == window.synthetic_ids["pending"]


def test_preferences_are_drafts_until_saved_and_cancel_preserves_state(window):
    window.show_page("preferences")
    panel = window.preferences_panel
    original_calls = len(window.tab_scroller.calls)
    panel.retention_group.button(14).setChecked(True)
    panel.reverse.setChecked(True)
    panel.capture_enabled.setChecked(True)
    assert window.vault.setting("retention_days") == 0
    assert not window.listening and not window.tabs_reverse
    assert len(window.tab_scroller.calls) == original_calls
    panel.cancel()
    assert window.page == "library"
    assert window.vault.setting("retention_days") == 0
    assert not window.listening and not window.tabs_reverse

    window.show_page("preferences")
    panel = window.preferences_panel
    panel.retention_group.button(14).setChecked(True)
    panel.capture_enabled.setChecked(True)
    panel.reverse.setChecked(True)
    panel.focus_modes.button(1).setChecked(True)
    panel.focus_delay.setCurrentIndex(panel.focus_delay.findData(50))
    assert panel.apply()
    assert window.page == "library"
    assert window.listening and window.tabs_reverse
    assert window.vault.setting("retention_days") == 14
    assert window.vault.setting("paused") is False
    assert window.tab_scroller.calls[-1]["focus_mode"] == 1
    assert window.tab_scroller.calls[-1]["focus_delay"] == 50
    assert window.capture.jobs[-1] == ("cleanup", None)


def test_header_switches_update_open_draft_without_losing_other_edits(window):
    window.show_page("preferences")
    panel = window.preferences_panel
    panel.retention_group.button(30).setChecked(True)
    window.toggle_pause()
    window.toggle_tabs()
    assert panel.capture_enabled.isChecked() is True
    assert panel.tabs_enabled.isChecked() is False
    assert panel.retention_group.checkedId() == 30
    assert panel.apply()
    assert window.listening and not window.tabs_enabled
    assert window.vault.setting("retention_days") == 30


def test_search_shortcut_returns_from_other_pages(window):
    shortcut = next(s for s in window.findChildren(QShortcut) if s.key().toString() == "Ctrl+F")
    for page in ("status", "preferences"):
        window.show_page(page)
        shortcut.activated.emit()
        assert window.page == "library"
        assert window.pages.currentIndex() == 0


def test_reopening_preferences_releases_previous_panel(window, app):
    for _ in range(3):
        previous = window.preferences_panel
        window.show_page("preferences")
        flush(app)
        if previous is not None:
            assert not shiboken6.isValid(previous)
        assert window.findChildren(PreferencesPanel) == [window.preferences_panel]


def test_thumbnail_cache_is_lru_bounded_and_items_do_not_store_pixmaps(window):
    keys = list(window.synthetic_ids.values())
    cache = ThumbnailCache(window.vault.thumbs, capacity=3)
    for key in keys[:3]:
        assert not cache.get(key).isNull()
    cache.get(keys[0])
    cache.get(keys[3])
    assert len(cache.items) == 3
    assert keys[1] not in cache.items
    assert keys[0] in cache.items
    for i in range(70):
        window.card_delegate.cache.get(f"missing-synthetic-{i}")
    assert len(window.card_delegate.cache.items) <= 48
    assert all(window.gallery.item(i).data(Qt.ItemDataRole.DecorationRole) is None
               for i in range(window.gallery.count()))


def test_gallery_resize_updates_existing_card_geometry(app):
    gallery = GalleryView()
    gallery.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    for _ in range(12):
        item = QListWidgetItem()
        item.setSizeHint(gallery.card_size)
        gallery.addItem(item)
    gallery.resize(460, 420)
    gallery.show()
    flush(app)
    narrow = QSize(gallery.card_size)
    narrow_top = gallery.visualItemRect(gallery.item(0)).top()
    narrow_columns = sum(gallery.visualItemRect(gallery.item(i)).top() == narrow_top
                         for i in range(gallery.count()))
    gallery.resize(1120, 420)
    flush(app)
    wide_top = gallery.visualItemRect(gallery.item(0)).top()
    wide_columns = sum(gallery.visualItemRect(gallery.item(i)).top() == wide_top
                       for i in range(gallery.count()))
    assert wide_columns > narrow_columns
    assert gallery.card_size != narrow
    assert gallery.gridSize() == gallery.card_size
    assert all(gallery.item(i).sizeHint() == gallery.card_size for i in range(gallery.count()))
    assert gallery.visualItemRect(gallery.item(0)).right() <= gallery.viewport().width()
    gallery.hide()
    gallery.deleteLater()
    flush(app)


def test_inspector_navigation_zoom_and_close_release_original_images(window, app):
    for _ in range(3):
        window.gallery.setCurrentRow(0)
        dialog = ImageInspector(window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        assert dialog.view.has_image
        dialog.navigate(1)
        assert dialog._image_id == visible_ids(window)[1]
        dialog.view.actual_size()
        assert dialog.view.zoom_percent == pytest.approx(100)
        dialog.view.zoom_by(1.2)
        assert dialog.view.zoom_percent == pytest.approx(120)
        QTimer.singleShot(0, dialog.reject)
        dialog.exec()
        flush(app)
        assert not shiboken6.isValid(dialog)
        assert not window.findChildren(ImageInspector)


@pytest.fixture
def offscreen_pins(monkeypatch):
    from snapvault import pinboard

    class OffscreenPinnedImageWindow(pinboard.PinnedImageWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # MainWindow calls show() only after construction has completed.
            self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

    monkeypatch.setattr(pinboard, "PinnedImageWindow", OffscreenPinnedImageWindow)
    return OffscreenPinnedImageWindow


def test_bufengzhuoying_brand_icon_and_pin_entrypoints(window, app, monkeypatch, offscreen_pins):
    from PySide6.QtWidgets import QLabel

    assert window.windowTitle().startswith("捕风捉影")
    assert any(label.text() == "捕风捉影" for label in window.findChildren(QLabel, "brand"))
    icon = ui.app_icon()
    assert not icon.isNull() and not window.windowIcon().isNull()
    for size in (16, 32):
        assert not icon.pixmap(QSize(size, size)).isNull()

    assert window.pin_btn.isEnabled()
    window.pin_btn.click()
    shortcut = next(s for s in window.findChildren(QShortcut) if s.key().toString() == "Ctrl+P")
    shortcut.activated.emit()  # No system key event is sent.

    menu_actions = []
    class SelectingPinMenu(ui.QMenu):
        def exec(self, *_):
            menu_actions.extend(action.text() for action in self.actions())
            self.actions()[0].trigger()  # Exercise the context action without showing a menu.

    monkeypatch.setattr(ui, "QMenu", SelectingPinMenu)
    position = window.gallery.visualItemRect(window.gallery.item(0)).center()
    window.gallery_menu(position)
    flush(app)
    assert menu_actions[0] == "贴到屏幕"
    assert len(window.pinned_windows) == 3
    assert all(isinstance(pin, offscreen_pins) for pin in window.pinned_windows)
    window.close_all_pins()
    flush(app)


def test_independent_pins_remain_visible_when_library_hides_and_release_on_close(window, app, offscreen_pins):
    window.gallery.setCurrentRow(0)
    first = window.pin_image()
    window.gallery.setCurrentRow(1)
    second = window.pin_image()
    assert first is not None and second is not None and first is not second
    assert first.image_path != second.image_path
    assert window.pinned_windows == [first, second]
    for pin in (first, second):
        assert pin.parent() is None
        assert pin.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert pin.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        assert pin.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        assert pin.image_size == QSize(300, 180)
    window.hide()
    flush(app)
    assert not window.isVisible()
    assert first.isVisible() and second.isVisible()
    first.close()
    flush(app)
    assert not shiboken6.isValid(first)
    assert window.pinned_windows == [second]
    assert second.isVisible()
    second.close()
    flush(app)
    assert not shiboken6.isValid(second)
    assert window.pinned_windows == []


def test_close_all_pins_and_quitting_release_every_reference(window, app, offscreen_pins):
    pins = [window.pin_image(), window.pin_image()]
    assert all(pin is not None for pin in pins)
    window.close_all_pins()
    flush(app)
    assert not window.pinned_windows
    assert all(not shiboken6.isValid(pin) for pin in pins)

    remaining = [window.pin_image(), window.pin_image()]
    assert all(pin is not None for pin in remaining)
    window.quit_app()
    flush(app)
    assert window.quitting and not window.pinned_windows
    assert all(not shiboken6.isValid(pin) for pin in remaining)
    assert window.pin_image() is None  # Shutdown cannot create another ownerless window.


def test_inspector_pin_exits_modal_dialog_and_keeps_reference_alive(window, app, offscreen_pins):
    from PySide6.QtWidgets import QDialog

    dialog = ImageInspector(window)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    observed = {}

    def pin_from_modal():
        observed["modal_before"] = app.activeModalWidget() is dialog
        try:
            dialog.pin_image()
        except Exception as exc:
            observed["error"] = exc
        finally:
            # Avoid a hanging test if creation fails or the dialog forgets accept().
            if shiboken6.isValid(dialog) and dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, pin_from_modal)
    result = dialog.exec()
    flush(app)
    assert "error" not in observed
    assert observed["modal_before"]
    assert result == QDialog.DialogCode.Accepted
    assert not shiboken6.isValid(dialog)
    assert app.activeModalWidget() is None
    assert len(window.pinned_windows) == 1
    pin = window.pinned_windows[0]
    assert shiboken6.isValid(pin) and pin.isVisible() and pin.isEnabled()
    assert pin.parent() is None
    window.hide()
    flush(app)
    assert pin.isVisible()
    window.close_all_pins()
    flush(app)
    assert not shiboken6.isValid(pin)


def test_offline_ocr_preferences_force_auto_language_and_keep_failed_retry_available(window, app):
    from PySide6.QtWidgets import QLabel

    window.vault.set_setting("ocr_language", "fr-FR")
    window.show_page("preferences")
    panel = window.preferences_panel
    assert panel.language.objectName() == "ocrLanguage"
    assert panel.language.count() == 1
    assert panel.language.currentData() == "auto"
    assert not panel.language.isEnabled()
    assert panel.retry_button.isEnabled()  # Fixed language must not disable retries.

    before = list(window.ocr.queued)
    panel.retry_button.click()
    assert window.synthetic_ids["error"] in window.ocr.queued[len(before):]
    window.ocr.availability.emit([("auto", "自动识别（中英文混合）")], "")
    assert panel.language.count() == 1 and panel.language.currentData() == "auto"
    assert "就绪" in panel.language_status.text()
    assert panel.retry_button.isEnabled()
    window.ocr.availability.emit([], "Synthetic model unavailable")
    assert panel.retry_button.isEnabled()
    assert "暂不可用" in panel.language_status.text()
    assert "Synthetic model unavailable" in panel.language_warning.text()
    assert "离线" in window.statusBar().currentMessage()
    labels = "\n".join(label.text() for label in window.findChildren(QLabel))
    assert "Windows OCR" not in labels and "语言包" not in labels
    assert "逐张排队" in labels and "释放模型" in labels
    assert panel.apply()
    assert window.vault.setting("ocr_language") == "auto"
    flush(app)


def test_failed_ocr_retry_keeps_existing_text_in_detail_inspector_and_search(window, app):
    key = window.synthetic_ids["new"]
    window.gallery.setCurrentRow(visible_ids(window).index(key))
    original = window.vault.get(key)["ocr_text"]
    window.vault.fail_ocr(key, "Synthetic recognition timeout")
    window.refresh()
    assert window.ocr_text.toPlainText() == original
    assert window.copy_text_btn.isEnabled()
    assert "保留原有文字" in window.ocr_status.text()
    assert [row["id"] for row in window.vault.query("alpha")] == [key]

    dialog = ImageInspector(window)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    assert dialog.ocr_text.toPlainText() == original
    assert dialog.copy_text_button.isEnabled()
    assert "保留原有文字" in dialog.ocr_status.text()
    dialog.retry_ocr()
    assert window.ocr.queued[-1] == key
    assert window.vault.get(key)["ocr_text"] == original
    window.vault.finish_ocr(key, "")
    window.ocr.changed.emit(key)
    window.refresh_timer.stop()
    window.refresh()
    assert window.ocr_text.toPlainText() == original
    assert dialog.ocr_text.toPlainText() == original
    assert dialog.retry_button.isEnabled()

    window.vault.finish_ocr(key, "Updated strawberry content")
    window.ocr.changed.emit(key)
    window.refresh_timer.stop()
    window.refresh()
    assert window.ocr_text.toPlainText() == "Updated strawberry content"
    assert dialog.ocr_text.toPlainText() == "Updated strawberry content"
    assert not window.vault.query("alpha")
    assert [row["id"] for row in window.vault.query("strawberry")] == [key]
    dialog.close()
    flush(app)
    assert not shiboken6.isValid(dialog)


def select_record(window, key):
    window.refresh()
    window.gallery.setCurrentRow(visible_ids(window).index(key))


def test_plain_text_detail_and_exact_txt_export_reject_image_actions(window, tmp_path, monkeypatch):
    content = "    def example():\r\n        return '<script>plain text</script>'\r\n"
    key, _ = window.vault.add_text(content)
    window.vault.set_category(key, "tech")
    select_record(window, key)
    assert window.ocr_text.toPlainText() == content.replace("\r\n", "\n")
    assert window.content_heading.text() == "原始文字"
    assert "PNG" not in window.detail_meta.text()
    assert "px" not in window.detail_meta.text()
    assert all(not control.isEnabled() for control in (window.copy_btn, window.open_btn, window.pin_btn, window.retry_btn))
    assert window.export_btn.isEnabled() and window.star_btn.isEnabled()

    queued = list(window.ocr.queued)
    window.copy_image()  # FakeClipboard rejects writes, proving the guard is in the handler.
    window.retry_ocr()
    assert window.pin_image() is None
    window.preview()  # Plain-text selection must not create a modal image viewer.
    assert window.ocr.queued == queued and not window.pinned_windows
    assert not window.findChildren(ImageInspector)
    destination = tmp_path / "exact-code.txt"
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *args: (str(destination), "纯文本 (*.txt)"))
    window.export_image()
    assert destination.read_bytes() == content.encode("utf-8")


def test_link_open_is_explicit_http_only_and_does_not_rewrite_original(window, monkeypatch):
    url = "https://example.invalid/article?id=42&token=demo%2Fvalue#fragment"
    key, _ = window.vault.add_text(url)
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda value: opened.append(value.toString()) or True)
    select_record(window, key)
    window.preview()
    assert opened == [] and window.ocr_text.toPlainText() == url
    assert not window.open_link_btn.isHidden()
    window.open_link()
    assert opened == [url]
    assert window.vault.get(key)["content"] == url
    # The action also validates the stored scheme rather than trusting a stale UI state.
    with window.vault.connect() as db:
        db.execute("UPDATE screenshots SET content=? WHERE id=?", ("javascript:alert('fixture')", key))
    window.open_link()
    assert opened == [url]
    assert "HTTP" in window.statusBar().currentMessage()


def test_nonimage_cards_never_decode_a_thumbnail(window, app, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtWidgets import QStyleOptionViewItem

    text, _ = window.vault.add_text("Synthetic technical code\n    value = 42")
    link, _ = window.vault.add_text("https://example.invalid/test")
    window.set_item_type("text")

    def reject_thumbnail(key):
        raise AssertionError("A text/link card must not decode a thumbnail: " + key)

    monkeypatch.setattr(window.card_delegate.cache, "get", reject_thumbnail)
    badges = []
    original_badge = window.card_delegate._badge

    def record_badge(painter, x, y, text, background, foreground):
        badges.append(text)
        return original_badge(painter, x, y, text, background, foreground)

    monkeypatch.setattr(window.card_delegate, "_badge", record_badge)
    for kind, key in (("text", text), ("link", link)):
        window.set_item_type(kind)
        badges.clear()
        assert visible_ids(window) == [key]
        canvas = QPixmap(270, 290)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 260, 280)
        try:
            window.card_delegate.paint(painter, option, window.gallery.indexFromItem(window.gallery.item(0)))
        finally:
            painter.end()
        assert ui.TYPE_LABELS[kind] in badges
        assert not {"PNG", "已识别", "识别中", "识别失败"}.intersection(badges)
    flush(app)


def test_topic_editor_and_type_filters_compose_with_favorites(window):
    text, _ = window.vault.add_text("Synthetic shopping reference")
    select_record(window, text)
    window.apply_tags([text], ["tasks", "receipts"])
    assert window.vault.get(text)["tags"] == ["tasks", "receipts"]
    assert window.vault.get(text)["tags_manual"]
    assert "手动" in window.topic_status.text()
    window.favorite()
    window.set_scope("favorites")
    window.set_topic("tasks")
    window.set_item_type("text")
    assert visible_ids(window) == [text]
    assert window.topic_buttons["tasks"].isChecked()
    assert window.type_filter.currentData() == "text"
    window.set_item_type("image")
    assert visible_ids(window) == []


def test_inspector_constructed_on_text_does_not_load_or_retry_image(window, app):
    text, _ = window.vault.add_text("Synthetic plain-text inspector fixture")
    select_record(window, text)
    queued = list(window.ocr.queued)
    dialog = ImageInspector(window)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    try:
        assert dialog._row is None and not dialog.view.has_image
        assert not dialog.retry_button.isEnabled()
        dialog.retry_ocr()
        assert window.ocr.queued == queued
    finally:
        dialog.reject()
        flush(app)
