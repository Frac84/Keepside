"""User-facing action entry points with owned synthetic data and clipboard.

No real desktop capture, global shortcuts, Hello dialog, user files or system
clipboard are involved. The actual MainWindow and editing dialogs are used.
"""
from datetime import datetime, timezone
import os
from pathlib import Path
import time
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QImage, QKeyEvent, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton
import pytest
from shiboken6 import isValid

from snapvault import annotation, image_compression, recent_panel, ui
from snapvault.annotation import Annotation
from test_quick_privacy_ui import app, flush, make_window
from test_recent_lock_security import hello
from test_v160_ui import PictureClipboard, QueueOcr, TaggingCapture


class OwnedClipboard(PictureClipboard):
    def __init__(self):
        super().__init__()
        self.mime_copies = []

    def setMimeData(self, mime):
        self.mime_copies.append(mime)


@pytest.fixture
def clipboard():
    return OwnedClipboard()


@pytest.fixture
def integration(make_window, monkeypatch):
    monkeypatch.setattr(ui, "CaptureWorker", TaggingCapture)
    monkeypatch.setattr(ui, "OcrWorker", QueueOcr)
    # ShelfPanel reads the same isolated clipboard as MainWindow.
    monkeypatch.setattr(recent_panel, "QApplication", ui.QApplication)
    monkeypatch.setattr(recent_panel.hotkey, "foreground_window", lambda: 0)
    monkeypatch.setattr(recent_panel, "apply_glass", lambda *_args: dict(enabled=False, method="test"))
    return make_window


def local_files(tmp_path):
    paths = [tmp_path / "synthetic note.txt", tmp_path / "synthetic video.mp4"]
    for path in paths:
        path.write_bytes(b"Synthetic original file, never imported or copied.")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return paths, mime


def select(window, key):
    window.refresh()
    for index in range(window.gallery.count()):
        item = window.gallery.item(index)
        if item.data(Qt.ItemDataRole.UserRole)["id"] == key:
            window.gallery.setCurrentItem(item)
            window.active_id = key
            window.update_detail()
            return
    raise AssertionError("Synthetic library item missing")


def library_image(window):
    with Image.new("RGB", (520, 220), "#3d287c") as image:
        key, _created = window.vault.add(image, datetime.now(timezone.utc))
    row = window.vault.get(key)
    path = window.vault.images / row["filename"]
    select(window, key)
    return key, path, path.read_bytes()


def wait_for(app, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.01)
    raise AssertionError("Image tool did not finish cleanly")


def test_primary_tools_are_visible_and_click_their_actions(integration, monkeypatch):
    actions = []
    for name, label in (("show_file_basket", "basket"),
                        ("screenshot_annotation_from_window", "annotation"),
                        ("compress_local_image", "compression")):
        monkeypatch.setattr(ui.MainWindow, name, lambda self, _checked=False, value=label: actions.append(value))
    window = integration()
    for button in (window.basket_button, window.annotate_capture_button, window.compress_file_button):
        assert isinstance(button, QPushButton) and button.isVisible() and button.isEnabled()
        button.click()
    assert actions == ["basket", "annotation", "compression"]
    assert "Ctrl+Shift+B" in window.basket_button.toolTip()


@pytest.mark.parametrize("previous_tab", [0, 1, 2])
def test_main_window_file_paste_needs_no_basket_tab_and_never_imports(integration, clipboard, tmp_path, previous_tab):
    window = integration()
    paths, mime = local_files(tmp_path)
    clipboard.mime = mime
    window.show_file_basket()
    window.quick_panel.tabs.setCurrentIndex(previous_tab)
    before_jobs = list(window.capture.jobs)
    window.paste_from_window()
    panel = window.quick_panel
    assert panel.isVisible() and panel.basket_mode
    assert [Path(row["path"]) for row in panel.basket_entries()] == paths
    assert window.vault.query(limit=10) == [] and window.capture.jobs == before_jobs
    assert window.recent_files.entries() == [] and not clipboard.copied and not clipboard.mime_copies


def test_main_window_file_drop_goes_directly_to_basket(integration, tmp_path):
    window = integration()
    paths, mime = local_files(tmp_path)
    originals = {path: path.read_bytes() for path in paths}
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                       mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(event)
    assert event.isAccepted() and event.dropAction() == Qt.DropAction.CopyAction
    assert window.quick_panel.basket_mode and window.file_basket.count == 2
    assert window.vault.query(limit=10) == []
    assert not any(job[0] in {"file", "image"} for job in window.capture.jobs)
    assert {path: path.read_bytes() for path in paths} == originals


@pytest.mark.filterwarnings("ignore:Function.*QApplication.setActiveWindow.*:DeprecationWarning")
def test_search_focused_file_paste_is_routed_before_native_text_paste(integration, clipboard, tmp_path, app):
    window = integration()
    paths, mime = local_files(tmp_path)
    clipboard.mime = mime
    window.search.setText("keep this search query")
    # These windows explicitly use WA_DontShowOnScreen. A native activation
    # request cannot focus them, so set only Qt's test-local active window.
    app.setActiveWindow(window)
    window.search.setFocus()
    flush(app)
    assert window.search.hasFocus()
    before_jobs = list(window.capture.jobs)
    QTest.keyClick(window.search, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    flush(app)
    assert window.quick_panel is not None and window.quick_panel.basket_mode
    assert [Path(row["path"]) for row in window.quick_panel.basket_entries()] == paths
    assert window.search.text() == "keep this search query"
    assert window.capture.jobs == before_jobs and window.vault.query(limit=10) == []


@pytest.mark.parametrize("target", ["search", "gallery_viewport", "detail_viewport"])
def test_file_drop_on_child_controls_is_intercepted_before_text_or_import(integration, tmp_path, app, target):
    window = integration()
    paths, mime = local_files(tmp_path)
    window.search.setText("keep this search query")
    surfaces = {"search": window.search, "gallery_viewport": window.gallery.viewport(),
                "detail_viewport": window.detail_scroll.viewport()}
    surface = surfaces[target]
    before_jobs = list(window.capture.jobs)
    enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                            mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QCoreApplication.sendEvent(surface, enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                      mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QCoreApplication.sendEvent(surface, drop)
    flush(app)
    assert drop.isAccepted() and drop.dropAction() == Qt.DropAction.CopyAction
    assert window.quick_panel is not None and window.quick_panel.basket_mode
    assert [Path(row["path"]) for row in window.quick_panel.basket_entries()] == paths
    assert window.search.text() == "keep this search query"
    assert window.capture.jobs == before_jobs and window.vault.query(limit=10) == []


def test_main_search_plain_text_paste_is_left_to_native_line_edit(integration, clipboard):
    window = integration()
    mime = QMimeData()
    mime.setText("Synthetic ordinary search text")
    clipboard.mime = mime
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    before_jobs = list(window.capture.jobs)
    # Check the decision only. Native QLineEdit paste would consult the C++
    # system clipboard, so it is deliberately not dispatched in this test.
    assert not window.eventFilter(window.search, event)
    assert window.quick_panel is None and window.capture.jobs == before_jobs


def test_main_drop_rejects_move_only_and_remote_urls(integration, tmp_path):
    window = integration()
    _paths, mime = local_files(tmp_path)
    move = QDropEvent(QPointF(10, 10), Qt.DropAction.MoveAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(move)
    assert not move.isAccepted() and window.quick_panel is None
    remote = QMimeData()
    remote.setUrls([QUrl("https://example.invalid/synthetic.png")])
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, remote,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(drop)
    assert not drop.isAccepted() and window.file_basket.count == 0


def test_manual_lock_blocks_main_file_paste_and_drop_without_auth(integration, clipboard, hello, tmp_path):
    window = integration()
    _paths, mime = local_files(tmp_path)
    window.show_file_basket()
    assert window.quick_panel.lock_files()
    clipboard.mime = mime
    window.paste_from_window()
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(drop)
    assert not drop.isAccepted() and window.file_basket.count == 0
    assert window.quick_panel.lock_card.isVisible() and "verify" not in hello
    assert window.vault.query(limit=10) == []


@pytest.mark.parametrize("kind", ["text", "image"])
def test_normal_main_paste_keeps_original_manual_capture_flow(integration, clipboard, kind):
    window = integration()
    mime = QMimeData()
    if kind == "text":
        mime.setText("Synthetic ordinary text keeps its normal flow.")
    else:
        clipboard.picture = QImage(120, 70, QImage.Format.Format_RGB32)
        clipboard.picture.fill(QColor("#7a3399"))
        mime.setImageData(clipboard.picture)
    clipboard.mime = mime
    before = len(window.capture.jobs)
    window.paste_from_window()
    jobs = window.capture.jobs[before:]
    assert len(jobs) == 1 and jobs[0][0] == kind
    if kind == "text":
        assert jobs[0][1] == mime.text()
    else:
        assert jobs[0][1].size() == clipboard.picture.size()
    assert window.quick_panel is None and window.file_basket.count == 0


def test_selected_image_buttons_open_original_pixels_and_ignore_text(integration, monkeypatch, app):
    window = integration()
    key, path, original = library_image(window)
    # Deliberately change the thumbnail. Tools must decode the actual original.
    thumb = QImage(20, 20, QImage.Format.Format_RGB32)
    thumb.fill(QColor("green"))
    assert thumb.save(str(window.vault.thumbs / f"{key}.png"))
    annotations, compressions = [], []
    monkeypatch.setattr(window, "open_annotation", lambda image: annotations.append(image.copy()))
    monkeypatch.setattr(window, "_open_compression", lambda image, value=None: compressions.append((image.copy(), value)))
    assert window.annotate_btn.isVisible() and window.compress_btn.isVisible()
    window.annotate_btn.click()
    window.compress_btn.click()
    assert len(annotations) == len(compressions) == 1
    for image in (annotations[0], compressions[0][0]):
        assert image.size().toTuple() == (520, 220)
        assert image.pixelColor(100, 100) == QColor("#3d287c")
    assert compressions[0][1] == path and path.read_bytes() == original
    text_key, _ = window.vault.add_text("Synthetic non-image content")
    select(window, text_key)
    assert window.annotate_btn.isHidden() and window.compress_btn.isHidden()
    assert not window.annotate_btn.isEnabled() and not window.compress_btn.isEnabled()
    assert window.annotate_image() is None and window.compress_image() is None
    assert len(annotations) == len(compressions) == 1


def test_screenshot_annotation_entry_captures_original_then_opens_editor(integration, clipboard, monkeypatch, app):
    window = integration()
    image = QImage(800, 600, QImage.Format.Format_RGB32)
    image.fill(QColor("#225588"))
    window.grab_screens = lambda: [(QRect(0, 0, 400, 300), QPixmap.fromImage(image))]
    editors = []
    monkeypatch.setattr(window, "open_annotation", lambda value: editors.append(value.copy()))
    before = len(window.capture.jobs)
    assert window.start_screenshot(annotate=True)
    overlay = window.shot_session.overlays[0]
    overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    overlay.set_selection(QRect(20, 30, 100, 50))
    overlay.finish("copy")
    flush(app)
    assert window.shot_session is None and len(editors) == 1
    assert editors[0].size().toTuple() == (200, 100)
    assert editors[0].pixelColor(5, 5) == QColor("#225588")
    jobs = window.capture.jobs[before:]
    assert len(jobs) == 1 and jobs[0][0] == "image"
    assert jobs[0][1] == editors[0] and clipboard.copied == []


def test_annotation_copy_and_save_are_wired_to_host_without_changing_original(integration, clipboard, app, monkeypatch):
    window = integration()
    _key, path, original = library_image(window)
    dialog = window.annotate_image()
    assert isinstance(dialog, annotation.AnnotationDialog) and dialog in window.image_tool_dialogs
    dialog.canvas.add_annotation(Annotation("rectangle", (QPointF(30, 30), QPointF(100, 90)), "#e81123", 6))
    expected = dialog.rendered_image()
    owned = []
    operation = window._copy_owned
    monkeypatch.setattr(window, "_copy_owned", lambda setter: (owned.append(True), operation(setter)))
    captured_pixels = []
    monkeypatch.setattr(clipboard, "setImage", lambda image: captured_pixels.append(image.copy()))
    before = len(window.capture.jobs)
    dialog.copy_button.click()
    assert owned == [True] and captured_pixels == [expected]
    assert window.capture.jobs[before:] == []
    dialog.library_button.click()
    jobs = window.capture.jobs[before:]
    assert len(jobs) == 1 and jobs[0][0] == "image" and jobs[0][1] == expected
    assert path.read_bytes() == original and len(window.vault.query(limit=10)) == 1
    dialog.close()
    flush(app)
    assert dialog not in window.image_tool_dialogs


def test_compression_file_entry_opens_without_import_and_copy_preserves_encoded_size(integration, clipboard, tmp_path, app, monkeypatch):
    window = integration()
    source = tmp_path / "synthetic-photo.png"
    with Image.new("RGB", (240, 160), "#835abd") as image:
        image.save(source)
    original = source.read_bytes()
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *_args: (str(source), ""))
    dialog = window.compress_local_image()
    assert isinstance(dialog, image_compression.CompressionDialog)
    assert dialog.source_path == source.resolve() and dialog.original_size == len(original)
    wait_for(app, lambda: dialog.worker is None and dialog.result is not None)
    assert len(dialog.result.data) <= 500 * 1024
    before_jobs = list(window.capture.jobs)
    dialog.copy_button.click()
    assert len(clipboard.mime_copies) == 1
    exported = Path(clipboard.mime_copies[0].urls()[0].toLocalFile())
    assert exported.is_file() and exported.read_bytes() == dialog.result.data
    assert source.read_bytes() == original and window.vault.query(limit=10) == []
    assert window.capture.jobs == before_jobs
    dialog.close()
    flush(app)
    assert dialog not in window.image_tool_dialogs


def test_quit_closes_editors_and_ignores_late_tool_signals(integration, clipboard, app):
    window = integration()
    _key, _path, _original = library_image(window)
    editor = window.annotate_image()
    compressor = window.compress_image()
    wait_for(app, lambda: compressor.worker is None and compressor.result is not None)
    before_jobs = list(window.capture.jobs)
    before_copies = list(clipboard.copied)
    window.quit_app()
    assert window.image_tool_dialogs == []
    image = QImage(20, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    window._copy_tool_image(image)
    window._save_tool_image(image)
    assert clipboard.copied == before_copies
    assert not any(job[0] == "image" for job in window.capture.jobs[len(before_jobs):])
    flush(app)
    assert not isValid(editor) and not isValid(compressor)


@pytest.mark.parametrize("action", ["close", "quit"])
def test_closing_with_compression_in_flight_cancels_and_releases_tool(integration, app, monkeypatch, action):
    entered = threading.Event()
    def cancellable_encoder(_image, _target, _fmt, cancelled):
        entered.set()
        deadline = time.monotonic() + 5
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(.01)
        raise image_compression.CompressionCancelled()
    monkeypatch.setattr(image_compression, "compress_image", cancellable_encoder)
    window = integration()
    library_image(window)
    dialog = window.compress_image()
    wait_for(app, entered.is_set)
    worker = dialog.worker
    assert worker is not None and worker.isRunning()
    if action == "quit":
        window.quit_app()
    else:
        dialog.close()
    wait_for(app, lambda: dialog not in window.image_tool_dialogs)
    flush(app)
    assert not isValid(dialog)
    assert not isValid(worker) or not worker.isRunning()
