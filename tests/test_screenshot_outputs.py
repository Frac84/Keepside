"""Screenshot output boundaries: final pixels, cancel, export, and local OCR."""
from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
import pytest

from snapvault import ui
from snapvault.annotation import Annotation, render_annotations
from snapvault.screenshot_output import write_screenshot_png
from test_quick_privacy_ui import app, flush, make_window  # noqa: F401
from test_v160_ui import window, clipboard  # noqa: F401


def picture():
    value = QImage(160, 100, QImage.Format.Format_RGB32)
    value.fill(QColor("white"))
    return value


def test_legacy_editor_entry_does_not_save_raw_screenshot(window, monkeypatch):
    shown = []
    monkeypatch.setattr(window, "open_annotation", lambda image: shown.append(image.copy()))
    window.capture.jobs.clear()
    window._screenshot_taken(picture(), "annotate")
    assert len(shown) == 1
    assert window.capture.jobs == []


def test_cancel_save_preserves_clipboard_and_library(window, clipboard, monkeypatch):
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *_a, **_k: ("", ""))
    window.capture.jobs.clear()
    copied = list(clipboard.copied)
    window._screenshot_taken(picture(), "save")
    assert window.capture.jobs == [] and clipboard.copied == copied
    assert len(window.image_tool_dialogs) == 1  # Cancel keeps a usable result.


def test_export_and_library_receive_only_flattened_pixels(window, monkeypatch, tmp_path):
    original = picture()
    marks = [Annotation("redact", (QPointF(20, 20), QPointF(70, 70)), "#000000", 4)]
    final = render_annotations(original, marks)
    destination = tmp_path / "exported"
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(destination), "PNG"))
    window.capture.jobs.clear()
    window._screenshot_taken(final, "save")
    assert original.pixelColor(40, 40) == QColor("white")
    with Image.open(destination.with_suffix(".png")) as output:
        assert output.size == (160, 100) and output.getpixel((40, 40))[:3] == (0, 0, 0)
    kind, saved, tag = window.capture.jobs[-1]
    assert kind == "image" and tag is None and saved == final


def test_atomic_export_failure_does_not_create_parent(tmp_path):
    path = tmp_path / "missing" / "output.png"
    with pytest.raises(OSError):
        write_screenshot_png(picture(), path)
    assert not path.exists() and not path.parent.exists()


def test_unknown_action_never_copies_or_saves(window, clipboard):
    window.capture.jobs.clear()
    copied = list(clipboard.copied)
    window._screenshot_taken(picture(), "unknown")
    assert not window.capture.jobs and clipboard.copied == copied
    assert "未知" in window.statusBar().currentMessage()


def test_full_library_queue_reports_copy_only(window, clipboard, monkeypatch):
    monkeypatch.setattr(window.capture, "submit", lambda *_a, **_k: False)
    window._screenshot_taken(picture(), "copy")
    assert clipboard.copied[-1] == ("image", (160, 100))
    assert "未存入图库" in window.statusBar().currentMessage()


def test_screenshot_ocr_tracks_its_own_job_and_copies_on_request(window, clipboard, app):
    window.capture.jobs.clear()
    dialog = window.recognize_screenshot(picture())
    assert dialog.item_id is None and not dialog.copy_button.isEnabled()
    kind, final, tag = window.capture.jobs[-1]
    assert kind == "image" and tag[0] == "screenshot-ocr"
    key, created = window.vault.add(Image.new("RGB", (160, 100), "white"))
    window.capture.saved.emit(key, created)
    window.capture.tagged.emit(tag, key, created)
    assert dialog.item_id == key and not dialog.copy_button.isEnabled()
    window.vault.finish_ocr(key, "本地识字验证")
    window.ocr.changed.emit(key)
    assert dialog.text.toPlainText() == "本地识字验证"
    assert dialog.copy_button.isEnabled() and not clipboard.copied
    dialog.copy_button.click()
    assert clipboard.copied[-1] == ("text", "本地识字验证")
    dialog.close()
    flush(app)
    assert not window.screenshot_text_windows and not window._pending_screenshot_text


def test_closing_ocr_before_save_ignores_late_completion(window, app):
    dialog = window.recognize_screenshot(picture())
    tag = window.capture.jobs[-1][2]
    dialog.close()
    flush(app)
    window.capture.tagged.emit(tag, "late-key", True)
    assert not window._pending_screenshot_text and not window.screenshot_text_windows


def test_reused_ocr_result_does_not_restart_finished_job(window):
    key, _ = window.vault.add(Image.new("RGB", (160, 100), "white"))
    window.vault.finish_ocr(key, "已有结果")
    dialog = window.recognize_screenshot(picture())
    tag = window.capture.jobs[-1][2]
    window.ocr.queued.clear()
    window.capture.tagged.emit(tag, key, False)
    assert dialog.text.toPlainText() == "已有结果" and window.ocr.queued == []
    dialog.retry_button.click()
    assert window.ocr.queued == [key]
    dialog.close()
