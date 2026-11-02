"""Synthetic target-size exports and non-blocking editor lifecycle checks."""
from io import BytesIO
import random
import time

from PIL import Image
import pytest
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication, QLabel

from snapvault import image_compression as compression
from snapvault.image_compression import CompressionCancelled, CompressionDialog, compress_image, pil_from_qimage


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def noisy_image(width=800, height=500):
    return Image.frombytes("RGB", (width, height), random.Random(19).randbytes(width * height * 3))


def wait(app, predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Synthetic image operation did not finish")


def test_jpeg_target_is_a_strict_file_limit_and_original_is_unchanged():
    image = noisy_image()
    before = image.tobytes()
    result = compress_image(image, 12 * 1024)
    assert result.format == "JPEG" and len(result.data) <= 12 * 1024
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.size == (result.width, result.height)
        assert decoded.width < image.width and decoded.height < image.height
        assert abs(decoded.width / decoded.height - image.width / image.height) < .02
    assert image.size == (800, 500) and image.tobytes() == before


def test_large_limit_keeps_original_dimensions_and_high_quality():
    image = noisy_image(120, 80)
    result = compress_image(image, 1024 * 1024)
    assert not result.resized and result.quality == 95
    assert result.width == 120 and result.height == 80


def test_auto_detects_real_alpha_not_rgba_storage_format():
    image = Image.new("RGBA", (80, 60), (30, 80, 120, 255))
    assert compress_image(image, 10 * 1024).format == "JPEG"
    image.putpixel((20, 20), (30, 80, 120, 0))
    result = compress_image(image, 10 * 1024)
    assert result.format == "WEBP" and not result.flattened_alpha
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.getpixel((20, 20))[3] == 0
    assert image.getpixel((20, 20)) == (30, 80, 120, 0)


def test_jpeg_explicitly_flattens_transparent_pixels_to_white():
    image = Image.new("RGBA", (64, 40), (10, 20, 30, 0))
    result = compress_image(image, 10 * 1024, "JPEG")
    assert result.flattened_alpha
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.mode == "RGB" and min(decoded.getpixel((20, 20))) >= 250
    assert image.mode == "RGBA" and image.getpixel((20, 20))[3] == 0


def test_png_resizes_to_fit_and_retains_transparency():
    image = noisy_image(220, 150).convert("RGBA")
    image.putalpha(110)
    result = compress_image(image, 4 * 1024, "PNG")
    assert result.resized and len(result.data) <= 4 * 1024
    assert result.quality is None and result.extension == ".png"
    with Image.open(BytesIO(result.data)) as decoded:
        assert decoded.mode == "RGBA" and decoded.getchannel("A").getextrema() == (110, 110)


@pytest.mark.parametrize("target", [0, -1, True, 10.5])
def test_invalid_limit_is_not_silently_relaxed(target):
    with pytest.raises(ValueError):
        compress_image(Image.new("RGB", (20, 20)), target)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_impossibly_small_limits_raise_instead_of_returning_oversized_file(fmt):
    with pytest.raises(ValueError, match="目标太小"):
        compress_image(Image.new("RGBA", (30, 20), (50, 80, 90, 100)), 8, fmt)


def test_cancellation_is_checked_between_encoder_calls():
    checks = []

    def cancelled():
        checks.append(True)
        return len(checks) >= 3

    with pytest.raises(CompressionCancelled):
        compress_image(noisy_image(400, 300), 1024, cancelled=cancelled)
    assert len(checks) == 3


def test_qimage_conversion_does_not_mutate_or_swap_channels(app):
    image = QImage(19, 13, QImage.Format.Format_ARGB32)
    image.fill(QColor(20, 80, 140, 110))
    before = image.copy()
    with pil_from_qimage(image) as converted:
        assert converted.getpixel((0, 0)) == (20, 80, 140, 110)
        converted.putpixel((0, 0), (1, 2, 3, 4))
    assert image == before


def test_dialog_auto_compresses_and_copies_exact_encoded_result(app, monkeypatch):
    image = QImage(120, 80, QImage.Format.Format_RGB32)
    image.fill(QColor("#0088cc"))
    dialog = CompressionDialog(image, source_name="synthetic.png", source_size=25000)
    results = []
    dialog.fileCopyRequested.connect(results.append)
    wait(app, lambda: dialog.result is not None and dialog.worker is None)
    assert dialog.result.format == "JPEG" and len(dialog.result.data) <= 500 * 1024
    assert dialog.copy_button.isEnabled() and dialog.save_button.isEnabled()
    assert any("24.4 KB" in label.text() for label in dialog.findChildren(QLabel))
    dialog.copy_file()
    assert results == [dialog.result]
    assert "已达到目标大小" in dialog.status.text()
    dialog.target.setValue(12)
    assert dialog.result is None and not dialog.copy_button.isEnabled() and not dialog.save_button.isEnabled()
    dialog.reject()
    dialog.deleteLater()
    app.processEvents()


def test_save_refuses_original_path_and_extension_mismatch(app, tmp_path, monkeypatch):
    original = tmp_path / "synthetic.jpg"
    original.write_bytes(b"synthetic original sentinel")
    image = QImage(60, 40, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    dialog = CompressionDialog(image, source_path=original, source_name=original.name)
    wait(app, lambda: dialog.result is not None and dialog.worker is None)
    monkeypatch.setattr(compression.QFileDialog, "getSaveFileName", lambda *a, **k: (str(original), ""))
    dialog.save_file()
    assert original.read_bytes() == b"synthetic original sentinel"
    assert "新的文件名" in dialog.status.text()
    bad = tmp_path / "output.png"
    monkeypatch.setattr(compression.QFileDialog, "getSaveFileName", lambda *a, **k: (str(bad), ""))
    dialog.save_file()
    assert not bad.exists() and "扩展名" in dialog.status.text()
    exported = tmp_path / "output.jpg"
    monkeypatch.setattr(compression.QFileDialog, "getSaveFileName", lambda *a, **k: (str(exported), ""))
    dialog.save_file()
    assert exported.read_bytes() == dialog.result.data
    dialog.reject()
    dialog.deleteLater()
    app.processEvents()


def test_close_requests_cooperative_cancel_without_destroying_live_thread(app, monkeypatch):
    image = QImage(80, 60, QImage.Format.Format_RGB32)
    image.fill(QColor("green"))
    entered = []

    def slow(image, target, output_format, cancelled):
        entered.append(True)
        deadline = time.monotonic() + 3
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(.01)
        raise CompressionCancelled()

    monkeypatch.setattr(compression, "compress_image", slow)
    dialog = CompressionDialog(image)
    wait(app, lambda: bool(entered))
    worker = dialog.worker
    finished = []
    worker.finished.connect(lambda: finished.append(True))
    dialog.reject()
    assert dialog._closing and worker.isInterruptionRequested()
    wait(app, lambda: dialog.worker is None)
    assert finished == [True]
    dialog.deleteLater()
    app.processEvents()
