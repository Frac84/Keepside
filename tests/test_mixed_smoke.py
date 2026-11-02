"""Mixed-content integration using temporary data and a fully fake clipboard.

The real capture queue is exercised, but OCR, browser input, URL opening and the
system clipboard are never invoked. No personal vault is opened.
"""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from snapvault import ui
from snapvault.inspector import ImageInspector
from snapvault.preferences import PreferencesPanel
from snapvault.storage import Vault, normalized


class TrackingMime(QMimeData):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def text(self):
        self.owner.text_reads += 1
        return super().text()


class FakeClipboard(QObject):
    dataChanged = Signal()

    def __init__(self):
        super().__init__()
        self.sequence_number = 100
        self.text_reads = 0
        self._mime = TrackingMime(self)
        self._image = QImage()

    def mimeData(self):
        return self._mime

    def image(self):
        return QImage(self._image)

    def text(self):
        return self._mime.text()

    def put_text(self, text, *, repeat_async=False, image=None):
        self.sequence_number += 1
        self._mime = TrackingMime(self)
        self._mime.setText(text)
        self._image = QImage(image) if image is not None else QImage()
        if image is not None:
            self._mime.setImageData(self._image)
        self.dataChanged.emit()
        if repeat_async:
            # Real clipboard notifications can arrive after the writing call.
            QTimer.singleShot(0, self.dataChanged.emit)

    def setText(self, text):
        self.put_text(text, repeat_async=True)

    def setImage(self, image):
        self.put_text("", image=image, repeat_async=True)


class FakeOcr(QObject):
    changed = Signal(str)
    availability = Signal(object, str)

    def __init__(self, vault):
        super().__init__()
        self.queued = []
        self.stopping = threading.Event()

    def start(self):
        pass

    def isRunning(self):
        return False

    def enqueue(self, key):
        self.queued.append(key)


def pump(app, seconds=0):
    deadline = time.monotonic() + seconds
    while True:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)


def until(app, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        pump(app, 0.01)
    assert predicate(), "Synthetic mixed-content operation did not finish"
    pump(app)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def harness(app, tmp_path, monkeypatch):
    clipboard = FakeClipboard()
    policy = {"blocked": False}
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
    monkeypatch.setattr(ui.MainWindow, "sequence", staticmethod(lambda: clipboard.sequence_number))
    monkeypatch.setattr(ui, "should_exclude_clipboard", lambda mime=None: policy["blocked"], raising=False)
    monkeypatch.setattr(ui, "OcrWorker", FakeOcr)

    def create(vault=None):
        vault = vault or Vault(tmp_path / ("vault-" + str(len(windows))))
        vault.set_setting("retention_days", 0)
        window = ui.MainWindow(vault, listen=True, use_tray=False, browser_tools=False, classify=False)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        windows.append(window)
        pump(app)
        return window

    yield SimpleNamespace(app=app, clipboard=clipboard, policy=policy, create=create)
    for window in windows:
        if shiboken6.isValid(window):
            window.quit_app()
            until(app, lambda: not window.capture.isRunning() and not window.ocr.isRunning())
            window.finish_quit()
            window.hide()
            window.deleteLater()
    pump(app)


def visible_ids(window):
    return [window.gallery.item(i).data(Qt.ItemDataRole.UserRole)["id"]
            for i in range(window.gallery.count())]


def select(window, key):
    window.refresh()
    window.gallery.setCurrentRow(visible_ids(window).index(key))
    assert window.active_id == key


def make_legacy_vault(root):
    """Create the actual old table/FTS shape, without new Vault migration code."""
    images, thumbnails = root / "images", root / "thumbnails"
    images.mkdir(parents=True)
    thumbnails.mkdir()
    filename = "legacy-synthetic.png"
    Image.new("RGB", (100, 60), "navy").save(images / filename)
    Image.new("RGB", (50, 30), "navy").save(thumbnails / "legacy.png")
    original = (images / filename).read_bytes()
    text = "SQLite API fixture 已识别文字"
    with sqlite3.connect(root / "vault.sqlite3") as db:
        db.executescript("""
            CREATE TABLE screenshots (
                id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL, captured_at TEXT NOT NULL,
                width INTEGER NOT NULL, height INTEGER NOT NULL, size INTEGER NOT NULL,
                title TEXT NOT NULL, ocr_text TEXT NOT NULL DEFAULT '',
                search_text TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0,
                ocr_status TEXT NOT NULL DEFAULT 'pending', ocr_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE VIRTUAL TABLE search_index USING fts5(
                search_text, content='screenshots', content_rowid='rowid', tokenize='trigram'
            );
            CREATE TRIGGER shots_insert AFTER INSERT ON screenshots BEGIN
                INSERT INTO search_index(rowid,search_text) VALUES(new.rowid,new.search_text);
            END;
            CREATE TRIGGER shots_delete AFTER DELETE ON screenshots BEGIN
                INSERT INTO search_index(search_index,rowid,search_text)
                VALUES('delete',old.rowid,old.search_text);
            END;
            CREATE TRIGGER shots_update AFTER UPDATE OF search_text ON screenshots BEGIN
                INSERT INTO search_index(search_index,rowid,search_text)
                VALUES('delete',old.rowid,old.search_text);
                INSERT INTO search_index(rowid,search_text) VALUES(new.rowid,new.search_text);
            END;
        """)
        db.execute("INSERT INTO screenshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "legacy", "legacy-pixel-hash", filename, datetime.now(timezone.utc).isoformat(),
            100, 60, len(original), "旧标题", text,
            normalized("旧标题 " + text + " " + filename), 1, "done", ""))
        db.row_factory = sqlite3.Row
        before = dict(db.execute("SELECT * FROM screenshots").fetchone())
    return original, before


def test_legacy_migration_ui_does_not_reocr_or_modify_original(harness, tmp_path):
    root = tmp_path / "old-vault"
    original, before = make_legacy_vault(root)
    vault = Vault(root)
    window = harness.create(vault)
    row = vault.get("legacy")
    for field, value in before.items():
        assert row[field] == value
    assert row["item_type"] == "image"
    assert row["category"] in {"tech", "read", "shopping", "daily", "other"}
    assert not row["category_manual"]
    assert (vault.images / before["filename"]).read_bytes() == original
    assert not window.ocr.queued
    assert not window.collect_text
    assert visible_ids(window) == ["legacy"]
    assert vault.query("SQLite")[0]["id"] == "legacy"


def test_text_default_off_does_not_read_body_but_manual_paste_saves(harness):
    window = harness.create()
    content = "    def fixture():\r\n        return '<b>plain text</b>'\r\n"
    harness.clipboard.put_text(content)
    pump(harness.app, 0.7)
    assert not window.collect_text
    assert harness.clipboard.text_reads == 0
    assert window.vault.stats()["total"] == 0
    window.capture_current()
    until(harness.app, lambda: window.vault.stats()["total"] == 1)
    row = window.vault.query()[0]
    assert row["item_type"] == "text" and row["content"] == content
    assert not window.ocr.queued
    select(window, row["id"])
    assert window.ocr_text.toPlainText() == content.replace("\r\n", "\n")
    assert not window.pin_btn.isEnabled()
    assert not window.retry_btn.isEnabled()


def test_opt_in_link_retains_all_parameters_and_never_enters_ocr(harness):
    window = harness.create()
    window.set_text_capture_enabled(True)
    url = "https://example.invalid/article?id=42&token=synthetic%2Ftest#section-2"
    harness.clipboard.put_text(url)
    until(harness.app, lambda: window.vault.stats()["total"] == 1)
    row = window.vault.query()[0]
    assert row["item_type"] == "link" and row["content"] == url
    assert row["ocr_status"] == "done" and not window.ocr.queued
    assert row["id"] not in window.vault.pending(include_errors=True)
    assert window.vault.query("synthetic")[0]["id"] == row["id"]


def test_excluded_clipboard_is_not_read_or_saved_even_manually(harness):
    window = harness.create()
    window.set_text_capture_enabled(True)
    harness.policy["blocked"] = True
    harness.clipboard.put_text("synthetic sensitive marker fixture")
    window.capture_current()
    pump(harness.app, 0.7)
    assert harness.clipboard.text_reads == 0
    assert window.vault.stats()["total"] == 0


def test_copying_own_text_ignores_delayed_duplicate_notification(harness):
    window = harness.create()
    window.set_text_capture_enabled(True)
    key, _ = window.vault.add_text("Synthetic copy loop fixture")
    select(window, key)
    submitted = []
    original_submit = window.capture.submit

    def observe(kind, value=None):
        submitted.append(kind)
        return original_submit(kind, value)

    window.capture.submit = observe
    window.copy_text()
    pump(harness.app, 0.7)
    assert "text" not in submitted
    assert window.vault.stats()["total"] == 1


def test_mime_with_image_and_text_saves_only_image(harness):
    window = harness.create()
    window.set_text_capture_enabled(True)
    image = QImage(90, 50, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.darkCyan)
    harness.clipboard.put_text("Alternative text should not become a second item", image=image)
    until(harness.app, lambda: window.vault.stats()["total"] == 1)
    pump(harness.app, 0.2)
    row = window.vault.query()[0]
    assert row["item_type"] == "image"
    assert window.ocr.queued == [row["id"]]
    assert harness.clipboard.text_reads == 0


@pytest.mark.parametrize("payload_type", ["text", "image"])
def test_clipboard_replaced_after_privacy_check_is_not_saved(harness, monkeypatch, payload_type):
    window = harness.create()
    window.set_text_capture_enabled(True)
    image = None
    if payload_type == "text":
        original_text = TrackingMime.text

        def changed_during_text_read(mime):
            value = original_text(mime)
            mime.owner.sequence_number += 1
            return value

        monkeypatch.setattr(TrackingMime, "text", changed_during_text_read)
    else:
        image = QImage(85, 45, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.darkMagenta)
        original_image = harness.clipboard.image

        def changed_during_image_read():
            value = original_image()
            harness.clipboard.sequence_number += 1
            return value

        monkeypatch.setattr(harness.clipboard, "image", changed_during_image_read)
    harness.clipboard.put_text("Synthetic content replaced during the read", image=image)
    pump(harness.app, 0.3)
    assert window.vault.stats()["total"] == 0
    assert not window.ocr.queued


def test_image_inspector_skips_text_and_link_rows(harness):
    window = harness.create()
    image_a, _ = window.vault.add(Image.new("RGB", (110, 80), "red"))
    window.vault.finish_ocr(image_a, "first image")
    window.vault.add_text("plain fixture text")
    window.vault.add_text("https://example.invalid/fixture")
    image_b, _ = window.vault.add(Image.new("RGB", (110, 80), "blue"))
    window.vault.finish_ocr(image_b, "second image")
    window.sort_box.setCurrentIndex(window.sort_box.findData("oldest"))
    select(window, image_a)
    inspector = ImageInspector(window)
    inspector.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    try:
        inspector.navigate(1)
        assert inspector._image_id == image_b and window.active_id == image_b
        inspector.navigate(-1)
        assert inspector._image_id == image_a and window.active_id == image_a
    finally:
        inspector.reject()
        pump(harness.app)


def test_mixed_type_category_favorite_search_and_manual_classification(harness):
    window = harness.create()
    image, _ = window.vault.add(Image.new("RGB", (90, 70), "olive"))
    window.vault.finish_ocr(image, "fixture API documentation")
    text, _ = window.vault.add_text("fixture API technical notes")
    link, _ = window.vault.add_text("https://example.invalid/fixture?topic=API")
    window.vault.set_category(image, "shopping")
    window.vault.set_category(text, "shopping")
    window.vault.set_category(link, "shopping")
    window.vault.toggle_favorite(link)
    window.vault.finish_ocr(image, "fixture SQLite API updated OCR")
    assert window.vault.get(image)["category"] == "shopping"
    assert window.vault.get(image)["category_manual"]
    rows = window.vault.query("fixture", kind="favorites", category="shopping", item_type="link")
    assert [row["id"] for row in rows] == [link]
    assert not window.vault.query("fixture", kind="favorites", category="shopping", item_type="image")
    assert {row["id"] for row in window.vault.query(category="shopping", item_type="text")} == {text}
    window.vault.delete(text)
    assert window.vault.get(image) and (window.vault.images / window.vault.get(image)["filename"]).is_file()


def test_quit_drains_already_accepted_text_tasks(harness):
    window = harness.create()
    expected = {f"Synthetic accepted queue fixture {index}" for index in range(12)}
    for content in expected:
        assert window.capture.submit("text", content)
    window.quit_app()
    until(harness.app, lambda: not window.capture.isRunning())
    assert {row["content"] for row in window.vault.query(item_type="text")} == expected
    assert not window.ocr.queued


def test_text_capture_preferences_cancel_save_and_live_sync(harness):
    window = harness.create()
    panel = PreferencesPanel(window)
    try:
        assert not panel.text_capture_enabled.isChecked()
        panel.text_capture_enabled.setChecked(True)
        assert not window.collect_text
        panel.cancel()
        assert not window.collect_text and not panel.text_capture_enabled.isChecked()
        panel.text_capture_enabled.setChecked(True)
        panel.apply()
        assert window.collect_text and window.vault.setting("collect_text") is True
        panel.reverse.setChecked(True)
        window.set_text_capture_enabled(False)
        assert not panel.text_capture_enabled.isChecked()
        assert panel.reverse.isChecked(), "Live toggle must preserve unrelated draft options"
    finally:
        panel.deleteLater()
        pump(harness.app)
