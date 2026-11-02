"""Annotation changes export pixels, never source pixels or the clipboard."""
import pytest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from snapvault import annotation
from snapvault.annotation import Annotation, AnnotationCanvas, AnnotationDialog, render_annotations


@pytest.fixture(scope="session", autouse=True)
def app():
    value = QApplication.instance() or QApplication([])
    yield value


def source_image():
    image = QImage(200, 120, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    return image


def mark(kind, *points, **kwargs):
    return Annotation(kind, tuple(QPointF(*point) for point in points), **kwargs)


def test_render_shapes_preserves_original_dimensions_and_pixels():
    original = source_image()
    before = original.copy()
    marks = [mark("rectangle", (10, 10), (80, 70), color="#ff0000", width=4),
             mark("arrow", (100, 20), (175, 80), color="#0000ff", width=5),
             mark("pen", (15, 95), (45, 100), (75, 95), color="#008000", width=5)]
    result = render_annotations(original, marks)
    assert original == before and result.size() == original.size()
    assert result.pixelColor(10, 30) == QColor("#ff0000")
    assert result.pixelColor(45, 100) == QColor("#008000")
    assert result.pixelColor(175, 80).blue() > result.pixelColor(175, 80).red()
    assert result.pixelColor(195, 115) == QColor("white")


def test_mosaic_flattens_selected_details_and_retains_surrounding_pixels():
    original = source_image()
    for y in range(20, 90):
        for x in range(20, 100):
            original.setPixelColor(x, y, QColor("black") if (x + y) % 2 else QColor("white"))
    before = original.copy()
    result = render_annotations(original, [mark("mosaic", (20, 20), (100, 90), width=4)])
    assert original == before
    assert result.pixelColor(30, 30) != original.pixelColor(30, 30)
    assert result.pixelColor(30, 30) == result.pixelColor(31, 30)
    assert result.pixelColor(150, 100) == original.pixelColor(150, 100)


def test_text_is_rendered_and_undo_replays_original_pixels():
    image = source_image()
    canvas = AnnotationCanvas(image)
    canvas.add_annotation(mark("rectangle", (5, 5), (75, 75), color="#ff0000"))
    rectangle_only = canvas.rendered_image()
    canvas.add_annotation(mark("text", (85, 20), text="Test", color="#000000", width=3))
    assert canvas.rendered_image() != rectangle_only
    canvas.undo()
    assert canvas.rendered_image() == rectangle_only
    canvas.reset()
    assert canvas.rendered_image() == render_annotations(image, ())
    assert image == source_image()


def test_coordinate_mapping_fits_letterbox_and_keeps_export_resolution():
    canvas = AnnotationCanvas(source_image())
    canvas.resize(800, 600)
    area = canvas.image_rect()
    assert canvas.image_point(QPointF(1, 1)) is None
    center = canvas.image_point(area.center())
    assert center.x() == pytest.approx(100)
    assert center.y() == pytest.approx(60)
    edge = canvas.image_point(QPointF(-100, 900), clamp=True)
    assert edge == QPointF(0, 119)
    assert canvas.rendered_image().size() == source_image().size()


def test_drag_builds_original_coordinate_rectangle_and_click_outside_does_nothing():
    canvas = AnnotationCanvas(source_image())
    canvas.resize(600, 420)
    canvas.show()
    QApplication.processEvents()
    area = canvas.image_rect()
    start = area.topLeft() + QPointF(area.width() * .1, area.height() * .1)
    end = area.topLeft() + QPointF(area.width() * .5, area.height() * .6)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start.toPoint())
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end.toPoint())
    assert len(canvas.annotations) == 1
    first, last = canvas.annotations[0].points
    assert first.x() == pytest.approx(20, abs=.5)
    assert first.y() == pytest.approx(12, abs=.5)
    assert last.x() == pytest.approx(100, abs=.5)
    assert last.y() == pytest.approx(72, abs=.5)
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPointF(2, 2).toPoint())
    assert len(canvas.annotations) == 1
    canvas.close()


def test_text_tool_commit_and_cancel():
    canvas = AnnotationCanvas(source_image())
    canvas.resize(600, 420)
    canvas.set_tool("text")
    canvas.show()
    QApplication.processEvents()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas.image_rect().center().toPoint())
    assert canvas.text_editor.isVisible() and not canvas.annotations
    canvas.text_editor.setText("Fixture")
    QTest.keyClick(canvas.text_editor, Qt.Key.Key_Return)
    assert len(canvas.annotations) == 1 and canvas.annotations[0].text == "Fixture"
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas.image_rect().center().toPoint())
    canvas.text_editor.setText("Cancelled")
    QTest.keyClick(canvas.text_editor, Qt.Key.Key_Escape)
    assert len(canvas.annotations) == 1
    canvas.close()


def test_output_buttons_emit_detached_images_without_clipboard_or_library_access(monkeypatch):
    original = source_image()
    dialog = AnnotationDialog(original)
    dialog.canvas.add_annotation(mark("rectangle", (10, 10), (80, 70)))
    copies, saves = [], []
    dialog.copyRequested.connect(copies.append)
    dialog.saveToLibraryRequested.connect(saves.append)
    monkeypatch.setattr(QApplication, "clipboard", lambda: pytest.fail("editor must use host-owned copy policy"))
    dialog.copy_button.click()
    dialog.library_button.click()
    assert len(copies) == len(saves) == 1
    assert copies[0] == saves[0] == dialog.rendered_image()
    copies[0].fill(QColor("green"))
    assert dialog.rendered_image() != copies[0]
    assert original == source_image()
    dialog.close()


def test_save_as_png_is_separate_file_and_cancel_has_no_side_effect(tmp_path, monkeypatch):
    original_path = tmp_path / "original.png"
    original = source_image()
    assert original.save(str(original_path))
    before = original_path.read_bytes()
    dialog = AnnotationDialog(original)
    dialog.canvas.add_annotation(mark("arrow", (15, 15), (150, 90)))
    destination = tmp_path / "marked.png"
    monkeypatch.setattr(annotation.QFileDialog, "getSaveFileName", lambda *_: (str(destination), ""))
    assert dialog.save_as()
    exported = QImage(str(destination)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    assert destination.is_file() and exported == dialog.rendered_image()
    assert original_path.read_bytes() == before
    monkeypatch.setattr(annotation.QFileDialog, "getSaveFileName", lambda *_: ("", ""))
    assert not dialog.save_as()
    assert len(list(tmp_path.iterdir())) == 2
    dialog.close()


def test_empty_images_rejected():
    with pytest.raises(ValueError):
        render_annotations(QImage(), ())
    with pytest.raises(ValueError):
        AnnotationCanvas(QImage())
