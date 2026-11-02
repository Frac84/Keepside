"""Export pixels and keyboard history for the screenshot annotation tools."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from snapvault.annotation import Annotation, AnnotationCanvas, AnnotationDialog, render_annotations
from test_annotation import app, mark, source_image  # noqa: F401


def test_ellipse_line_and_highlight_export_actual_pixels():
    source = source_image()
    ellipse = render_annotations(source, [mark("ellipse", (20, 20), (100, 80), color="#0067c0", width=4)])
    assert ellipse.pixelColor(60, 20) == QColor("#0067c0")
    assert ellipse.pixelColor(60, 50) == QColor("white")  # outlined, no opaque fill
    assert ellipse.pixelColor(20, 20) == QColor("white")  # an ellipse, not a rectangle
    line = render_annotations(source, [mark("line", (15, 100), (180, 100), color="#16815d", width=4)])
    assert line.pixelColor(100, 100) == QColor("#16815d")
    assert line.pixelColor(100, 85) == QColor("white")

    source.setPixelColor(80, 60, QColor("black"))
    highlighted = render_annotations(source, [mark("highlight", (20, 60), (160, 60), color="#ffff00", width=4)])
    light, dark = highlighted.pixelColor(100, 60), highlighted.pixelColor(80, 60)
    assert 0 < light.blue() < 255  # translucent yellow over white
    assert 0 < dark.red() < 255 and dark.blue() == 0  # underlying ink still visible
    assert light != dark
    assert highlighted.pixelColor(100, 30) == QColor("white")
    assert source.pixelColor(80, 60) == QColor("black")


def test_redaction_is_opaque_and_outward_rounded_even_with_transparent_color(tmp_path):
    source = source_image()
    source.fill(QColor(80, 120, 160, 25))
    before = source.copy()
    result = render_annotations(source, [mark("redact", (10.2, 20.8), (40.1, 50.2), color="#00000000")])
    for x, y in ((10, 20), (40, 50), (25, 35)):
        assert result.pixelColor(x, y) == QColor("black")
        assert result.pixelColor(x, y).alpha() == 255
    assert result.pixelColor(9, 20) == before.convertToFormat(result.format()).pixelColor(9, 20)
    assert source == before
    output = tmp_path / "flattened.png"
    assert result.save(str(output), "PNG")
    loaded = QImage(str(output))
    assert loaded.pixelColor(25, 35) == QColor("black")
    assert loaded.textKeys() == []


def test_number_badges_increment_and_history_restores_exact_output():
    source = source_image()
    canvas = AnnotationCanvas(source)
    first = Annotation("number", (QPointF(40, 40),), "#e81123", 4, "1")
    second = Annotation("number", (QPointF(100, 40),), "#f5c400", 4, "2")
    canvas.add_annotation(first)
    one = canvas.rendered_image()
    canvas.add_annotation(second)
    two = canvas.rendered_image()
    assert canvas.next_number() == 3 and two != one
    assert two.pixelColor(100, 30) == QColor("#f5c400")
    canvas.undo()
    assert canvas.next_number() == 2 and canvas.can_redo
    assert canvas.rendered_image() == one
    canvas.redo()
    assert canvas.rendered_image() == two and not canvas.can_redo
    canvas.undo()
    canvas.add_annotation(mark("line", (10, 100), (190, 100)))
    branch = canvas.rendered_image()
    assert not canvas.can_redo
    canvas.redo()
    assert canvas.rendered_image() == branch and canvas.next_number() == 2
    canvas.reset()
    assert not canvas.can_undo and not canvas.can_redo
    assert canvas.rendered_image() == render_annotations(source, ())


def test_number_tool_single_click_uses_visible_sequence():
    canvas = AnnotationCanvas(source_image())
    canvas.resize(600, 420)
    canvas.set_tool("number")
    point = canvas.image_rect().center().toPoint()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    assert [entry.text for entry in canvas.annotations] == ["1", "2"]
    canvas.undo()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    assert [entry.text for entry in canvas.annotations] == ["1", "2"]
    assert not canvas.can_redo


def test_editor_tools_fit_minimum_width_and_redo_keyboard_shortcuts():
    dialog = AnnotationDialog(source_image())
    dialog.resize(780, 620)
    dialog.show()
    dialog.activateWindow()
    dialog.setFocus()
    QApplication.processEvents()
    buttons = list(dialog.tool_buttons.values())
    assert len(buttons) == 11  # The added object selection tool is available too.
    for button in buttons:
        assert dialog.root.rect().contains(button.geometry())
        for other in buttons:
            if other is not button:
                assert not button.geometry().intersects(other.geometry())
    assert buttons[0].geometry().top() != buttons[-1].geometry().top()
    assert not dialog.undo_button.isEnabled() and not dialog.redo_button.isEnabled()
    dialog.width_plus.click()
    assert dialog.width_spin.value() == dialog.canvas.stroke_width == 5
    dialog.width_minus.click()
    assert dialog.width_spin.value() == dialog.canvas.stroke_width == 4
    dialog.width_spin.setValue(1)
    assert not dialog.width_minus.isEnabled() and dialog.width_plus.isEnabled()
    dialog.width_spin.setValue(24)
    assert dialog.width_minus.isEnabled() and not dialog.width_plus.isEnabled()
    dialog.width_spin.setValue(4)
    dialog.canvas.add_annotation(mark("line", (10, 10), (180, 100)))
    marked = dialog.rendered_image()
    QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    assert not dialog.canvas.can_undo and dialog.redo_button.isEnabled()
    QTest.keyClick(dialog, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    assert dialog.rendered_image() == marked and not dialog.redo_button.isEnabled()
    QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    QApplication.processEvents()
    assert dialog.rendered_image() == marked
    dialog.undo_button.click()
    dialog.redo_button.click()
    assert dialog.rendered_image() == marked
    dialog.close()
