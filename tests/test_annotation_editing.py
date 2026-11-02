"""Object edits remain independent, undoable, and absent from source pixels."""
from dataclasses import replace
import pytest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from snapvault.annotation import AnnotationCanvas, AnnotationDialog, render_annotations
from test_annotation import app, mark, source_image  # noqa: F401


def point(canvas, x, y):
    return canvas._widget_point(QPointF(x, y)).toPoint()


def shown_canvas():
    canvas = AnnotationCanvas(source_image())
    canvas.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    canvas.resize(760, 520)
    canvas.show()
    return canvas


def drag(canvas, first, last):
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=first)
    QTest.mouseMove(canvas, last)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=last)


def test_one_object_moves_and_one_undo_restores_complete_pixels():
    canvas = shown_canvas()
    arrow = mark("arrow", (25, 25), (75, 50), color="#0078d4")
    block = mark("redact", (120, 40), (170, 80), color="#111111")
    canvas.add_annotation(arrow)
    canvas.add_annotation(block)
    before = canvas.rendered_image()
    canvas.set_tool("select")
    drag(canvas, point(canvas, 50, 37.5), point(canvas, 65, 47.5))
    assert canvas.annotations[0].points[0].x() > arrow.points[0].x() + 14
    assert canvas.annotations[1] == block
    assert canvas.rendered_image() != before
    canvas.undo()
    assert canvas.rendered_image() == before and canvas.annotations[0] == arrow
    canvas.redo()
    assert canvas.annotations[0] != arrow and canvas.annotations[1] == block
    assert canvas.source == source_image()
    canvas.close()


def test_style_edit_delete_and_new_history_branch():
    canvas = shown_canvas()
    original = mark("line", (20, 30), (170, 30), color="#e81123", width=3)
    canvas.add_annotation(original)
    canvas.set_tool("select")
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point(canvas, 80, 30))
    assert canvas.selected_index == 0
    canvas.set_color("#0078d4")
    canvas.set_width(8)
    styled = canvas.rendered_image()
    assert styled.pixelColor(80, 30) == QColor("#0078d4")
    QTest.keyClick(canvas, Qt.Key.Key_Delete)
    assert not canvas.annotations
    canvas.undo()
    assert canvas.rendered_image() == styled
    canvas.undo()
    assert canvas.annotations[0].width == 3
    canvas.undo()
    assert canvas.annotations[0] == original
    canvas.select_annotation(0)
    canvas.set_color("#10893e")
    assert not canvas.can_redo
    canvas.close()


def test_double_click_edits_text_inline_and_escape_preserves_old_text():
    canvas = shown_canvas()
    canvas.add_annotation(mark("text", (30, 25), text="Before", width=3))
    original = canvas.rendered_image()
    canvas.set_tool("select")
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=point(canvas, 40, 35))
    assert canvas.text_editor.isVisible()
    canvas.text_editor.setText("修改后的文字 RQAM")
    QTest.keyClick(canvas.text_editor, Qt.Key.Key_Return)
    assert len(canvas.annotations) == 1 and canvas.annotations[0].text == "修改后的文字 RQAM"
    canvas.undo()
    assert canvas.annotations[0].text == "Before" and canvas.rendered_image() == original
    canvas.select_annotation(0)
    canvas.edit_selected_text()
    canvas.text_editor.setText("discarded")
    QTest.keyClick(canvas.text_editor, Qt.Key.Key_Escape)
    assert canvas.annotations[0].text == "Before" and canvas.rendered_image() == original
    canvas.close()


def test_selection_outline_and_preview_do_not_enter_exports():
    canvas = shown_canvas()
    block = mark("redact", (25, 25), (75, 70), color="#111111")
    canvas.add_annotation(block)
    expected = render_annotations(canvas.source, (block,))
    canvas.select_annotation(0)
    assert canvas.rendered_image() == expected
    canvas.grab()
    assert canvas.rendered_image() == expected and canvas.source == source_image()
    canvas.close()


def test_moving_redaction_keeps_old_sensitive_area_covered_during_drag():
    canvas = shown_canvas()
    canvas.add_annotation(mark("redact", (30, 25), (75, 70), color="#111111"))
    canvas.set_tool("select")
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=point(canvas, 50, 45))
    QTest.mouseMove(canvas, point(canvas, 110, 45))
    frame = canvas.grab().toImage()
    old = point(canvas, 50, 45)
    assert frame.pixelColor(old) == QColor("#111111")
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=point(canvas, 110, 45))
    assert canvas.rendered_image().pixelColor(50, 45) == QColor("white")
    canvas.undo()
    assert canvas.rendered_image().pixelColor(50, 45) == QColor("#111111")
    canvas.close()


def test_editor_preferences_and_shortcuts_do_not_capture_text_input():
    dialog = AnnotationDialog(source_image(), preferences={"tool": "text", "color": "#0078d4", "width": 3})
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    copies, options = [], []
    dialog.copyRequested.connect(copies.append)
    dialog.preferencesChanged.connect(options.append)
    canvas = dialog.canvas
    assert canvas.tool == "text" and canvas.color == "#0078d4" and canvas.stroke_width == 3
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=canvas.image_rect().center().toPoint())
    QTest.keyClicks(canvas.text_editor, "R Q A M")
    assert canvas.tool == "text" and not copies
    QTest.keyClick(canvas.text_editor, Qt.Key.Key_Return)
    assert not copies and canvas.annotations[0].text == "R Q A M"
    dialog.tool_buttons["select"].click()
    assert options[-1]["tool"] == "select"
    dialog.canvas.select_annotation(0)
    index = dialog.color_combo.findData("#10893e")
    dialog.color_combo.setCurrentIndex(index)
    assert canvas.annotations[0].color == "#10893e"
    assert options[-1]["color"] == "#10893e"
    dialog.copy_button.click()
    assert len(copies) == 1
    dialog.close()


@pytest.mark.parametrize("command", ["undo", "delete", "tool"])
def test_drag_interrupted_by_command_rolls_back_without_extra_history(command):
    canvas = shown_canvas()
    original = mark("arrow", (25, 25), (75, 50), color="#0078d4")
    canvas.add_annotation(original)
    canvas.set_tool("select")
    start, moved = point(canvas, 50, 37.5), point(canvas, 65, 47.5)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, moved)
    assert canvas._drag_origin is not None and canvas.annotations[0] != original
    if command == "undo":
        QTest.keyClick(canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    elif command == "delete":
        QTest.keyClick(canvas, Qt.Key.Key_Delete)
    else:
        QTest.keyClick(canvas, Qt.Key.Key_R)
    assert canvas._drag_origin is canvas._drag_before is canvas._drag_mark is None
    # The same physical gesture may still send events after a keyboard command.
    QTest.mouseMove(canvas, point(canvas, 90, 60))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=point(canvas, 90, 60))
    if command == "tool":
        assert canvas.annotations == [original] and canvas.tool == "rectangle"
        canvas.undo()
        assert not canvas.annotations
    else:
        assert not canvas.annotations
        canvas.redo() if command == "undo" else canvas.undo()
        assert canvas.annotations == [original]
    assert canvas.source == source_image()
    canvas.close()


def test_invalid_drag_selection_is_rolled_back_before_pointer_events():
    canvas = shown_canvas()
    original = mark("redact", (25, 25), (75, 70), color="#111111")
    canvas.add_annotation(original)
    canvas.set_tool("select")
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=point(canvas, 50, 45))
    QTest.mouseMove(canvas, point(canvas, 70, 55))
    canvas.selected_index = None  # Defensive guard even if an external caller cleared it.
    QTest.mouseMove(canvas, point(canvas, 80, 60))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=point(canvas, 80, 60))
    assert canvas.annotations == [original] and canvas._drag_origin is None
    canvas.undo()
    assert not canvas.annotations
    canvas.close()


def test_confirming_empty_existing_text_deletes_it_and_undo_restores_it():
    canvas = shown_canvas()
    original = mark("text", (30, 25), text="Keep on Escape", width=3)
    canvas.add_annotation(original)
    canvas.select_annotation(0)
    canvas.edit_selected_text()
    canvas.text_editor.setText("   ")
    QTest.keyClick(canvas.text_editor, Qt.Key.Key_Return)
    assert not canvas.annotations
    canvas.undo()
    assert canvas.annotations == [original]
    canvas.select_annotation(0)
    canvas.edit_selected_text()
    canvas.text_editor.clear()
    QTest.keyClick(canvas.text_editor, Qt.Key.Key_Escape)
    assert canvas.annotations == [original]
    canvas.close()
