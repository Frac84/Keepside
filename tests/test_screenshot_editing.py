"""Editable screenshot objects and output choices use frozen synthetic pixels."""
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
import pytest

from snapvault import screenshot
from snapvault.annotation import Annotation
from snapvault.screenshot import ScreenshotOverlay, ScreenshotSession
from test_screenshot import app, flush, frozen  # noqa: F401
from test_screenshot_advanced import drag


@pytest.fixture
def field(app):
    widget = ScreenshotOverlay(QRect(0, 0, 600, 400), frozen(1200, 800))
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    widget.set_selection(QRect(20, 20, 360, 230))
    flush(app)
    yield widget
    widget.close()
    flush(app)


def test_selected_object_moves_without_moving_selection_and_undo_is_one_step(field):
    mark = Annotation('redact', (QPointF(120, 120), QPointF(220, 200)), '#111111')
    field.add_annotation(mark)
    selected = QRect(field.selection)
    drag(field, (80, 80), (120, 100))
    assert field.selection == selected and field.selected_index == 0
    assert field.annotations[0].points == (QPointF(200, 160), QPointF(300, 240))
    assert field.crop().pixelColor(200, 160) == QColor('#111111')
    field.undo()
    assert field.annotations == [mark]
    field.redo()
    assert field.annotations[0].points[0] == QPointF(200, 160)
    assert field.source.pixelColor(240, 200) == QColor('red')


def test_object_color_width_delete_and_undo_restore_full_marks(field):
    mark = Annotation('arrow', (QPointF(120, 120), QPointF(260, 160)), '#e81123', 4)
    field.add_annotation(mark)
    QTest.mouseClick(field, Qt.MouseButton.LeftButton, pos=QPoint(95, 70))
    assert field.selected_index == 0
    field.color_combo.setCurrentIndex(field.color_combo.findData('#0078d4'))
    field.width_spin.setValue(9)
    assert field.annotations[0].color == '#0078d4' and field.annotations[0].width == 9
    QTest.keyClick(field, Qt.Key.Key_Delete)
    assert not field.annotations
    field.undo()
    assert field.annotations[0].color == '#0078d4' and field.annotations[0].width == 9
    field.undo()
    assert field.annotations[0].width == 4
    field.undo()
    assert field.annotations == [mark]


def test_text_double_click_edits_existing_mark_without_finishing(field):
    mark = Annotation('text', (QPointF(140, 140),), '#e81123', 4, 'old')
    field.add_annotation(mark)
    outputs = []
    field.finished.connect(lambda *_a: outputs.append(True))
    QTest.mouseDClick(field, Qt.MouseButton.LeftButton, pos=QPoint(78, 78))
    assert field.text_editor.isVisible() and field.text_editor.text() == 'old'
    field.text_editor.setText('PHOTOS new')
    QTest.keyClick(field.text_editor, Qt.Key.Key_Return)
    assert len(field.annotations) == 1 and field.annotations[0].text == 'PHOTOS new'
    assert not outputs
    field.undo()
    assert field.annotations == [mark]
    field.redo()
    assert field.annotations[0].text == 'PHOTOS new'


def test_cancel_existing_text_does_not_mutate_mark(field):
    mark = Annotation('text', (QPointF(140, 140),), text='old')
    field.add_annotation(mark)
    QTest.mouseDClick(field, Qt.MouseButton.LeftButton, pos=QPoint(78, 78))
    field.text_editor.setText('discard')
    QTest.keyClick(field.text_editor, Qt.Key.Key_Escape)
    assert field.annotations == [mark] and not field.text_editor.isVisible()


def test_copy_choice_is_explicit_and_next_default_is_remembered(app):
    widget = ScreenshotOverlay(QRect(0, 0, 400, 300), frozen(), preferences={
        'tool':'arrow', 'color':'#0078d4', 'width':7, 'copy_save':False})
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    widget.set_selection(QRect(20, 20, 120, 80))
    assert widget.tool == 'arrow' and widget.color == '#0078d4' and widget.stroke_width == 7
    assert widget.action_buttons['copy_only'].objectName() == 'primary'
    events, changes = [], []
    widget.finished.connect(lambda image, action: events.append(action))
    widget.preferencesChanged.connect(changes.append)
    QTest.keyClick(widget, Qt.Key.Key_Return)
    assert events == ['copy_only']
    widget.action_buttons['copy'].click()
    assert events[-1] == 'copy' and changes[-1]['copy_save'] is True
    widget.close()
    flush(app)


def test_save_cancel_and_failure_keep_same_selection_objects_and_no_region(field, monkeypatch, tmp_path):
    mark = Annotation('redact', (QPointF(120, 120), QPointF(220, 200)), '#111111')
    field.add_annotation(mark)
    before = field.crop()
    rectangle = QRect(field.selection)
    outputs, regions = [], []
    field.finished.connect(lambda *_a: outputs.append(True))
    field.regionSelected.connect(regions.append)
    monkeypatch.setattr(screenshot.QFileDialog, 'getSaveFileName', lambda *_a, **_k: ('', ''))
    assert not field.save_as()
    assert field.selection == rectangle and field.annotations == [mark] and field.crop() == before
    monkeypatch.setattr(screenshot.QFileDialog, 'getSaveFileName', lambda *_a, **_k: (str(tmp_path/'missing'/'result.png'), 'PNG'))
    assert not field.save_as()
    assert field.selection == rectangle and field.annotations == [mark] and field.crop() == before
    assert field.isVisible() and not outputs and not regions
    assert '保存失败' in field.action_buttons['save'].toolTip()


def test_save_success_outputs_once_only_final_pixels(field, monkeypatch, tmp_path):
    mark = Annotation('redact', (QPointF(120, 120), QPointF(220, 200)), '#111111')
    field.add_annotation(mark)
    outputs, regions = [], []
    field.finished.connect(lambda image, action: outputs.append((image.copy(), action)))
    field.regionSelected.connect(regions.append)
    path = tmp_path/'result.png'
    monkeypatch.setattr(screenshot.QFileDialog, 'getSaveFileName', lambda *_a, **_k: (str(path), 'PNG'))
    assert field.save_as() and path.is_file()
    assert len(outputs) == 1 and outputs[0][1] == 'save_done'
    assert outputs[0][0].pixelColor(100, 100) == QColor('#111111')
    assert regions[-1]['selection'] == [20, 20, 360, 230]


def test_restore_region_requires_exact_available_screen_and_valid_rectangle(app):
    valid = {'geometry':[-800,50,400,300], 'selection':[40,50,100,80]}
    session = ScreenshotSession([(QRect(-800,50,400,300), frozen())], last_region=valid)
    assert session.overlays[0].selection == QRect(40,50,100,80)
    session.close()
    for invalid in ({**valid, 'geometry':[0,0,400,300]},
                    {**valid, 'selection':[390,10,100,80]},
                    {**valid, 'selection':[40,50,0,80]},
                    {**valid, 'selection':[40.5,50,100,80]}):
        session = ScreenshotSession([(QRect(-800,50,400,300), frozen())], last_region=invalid)
        assert session.overlays[0].selection.isEmpty()
        session.close()
    flush(app)


def test_selected_outline_is_not_exported_and_old_mask_remains_during_drag(field):
    mark = Annotation('redact', (QPointF(120,120), QPointF(220,200)), '#111111')
    field.add_annotation(mark)
    before = field.crop()
    QTest.mousePress(field, Qt.MouseButton.LeftButton, pos=QPoint(80,80))
    assert field.selected_index == 0
    QTest.mouseMove(field, QPoint(140,100))
    assert field._mode == 'object_move' and field._object_preview is not None
    # The original committed cache stays masked until release; the translated
    # preview is also a flattened opaque tile, never a raw-source substitute.
    assert field._rendered.pixelColor(160,160) == QColor('#111111')
    assert field._object_preview.pixelColor(20,20) == QColor('#111111')
    QTest.mouseRelease(field, Qt.MouseButton.LeftButton, pos=QPoint(140,100))
    result = field.crop()
    field.select_annotation(None)
    assert result == field.crop() and result != before


@pytest.mark.parametrize('action', ['undo', 'delete', 'switch', 'redo', 'width'])
def test_object_drag_interrupted_by_edit_cannot_commit_stale_snapshot(field, action):
    mark = Annotation('redact', (QPointF(120,120), QPointF(220,200)), '#111111')
    later = Annotation('line', (QPointF(500,400), QPointF(600,430)))
    field.add_annotation(mark)
    if action == 'redo':
        field.add_annotation(later)
        field.undo()
    QTest.mousePress(field, Qt.MouseButton.LeftButton, pos=QPoint(80,80))
    QTest.mouseMove(field, QPoint(140,100))
    assert field._mode == 'object_move' and field.annotations[0] != mark
    if action == 'undo':
        field.undo()
        expected = []
    elif action == 'delete':
        field.delete_selected()
        expected = []
    elif action == 'switch':
        field.set_tool('arrow')
        expected = [mark]
    elif action == 'redo':
        field.redo()
        expected = [mark, later]
    else:
        field.width_spin.setValue(9)
        expected = [Annotation(mark.kind, mark.points, mark.color, 9)]
    assert field._origin is None and field._mode is None
    QTest.mouseMove(field, QPoint(180,130))
    QTest.mouseRelease(field, Qt.MouseButton.LeftButton, pos=QPoint(180,130))
    assert field.annotations == expected
    if action == 'undo':
        field.redo()
        assert field.annotations == [mark]
    else:
        field.undo()
        assert field.annotations == ([] if action == 'switch' else [mark])


@pytest.mark.parametrize('action', ['undo', 'delete', 'switch', 'redo'])
def test_whole_selection_drag_interrupted_by_edit_does_not_transform_history(field, action):
    mark = Annotation('redact', (QPointF(120,120), QPointF(220,200)), '#111111')
    later = Annotation('line', (QPointF(500,400), QPointF(600,430)))
    field.add_annotation(mark)
    if action == 'redo':
        field.add_annotation(later)
        field.undo()
    before = QRect(field.selection)
    QTest.mousePress(field, Qt.MouseButton.LeftButton, pos=QPoint(300,160))
    QTest.mouseMove(field, QPoint(330,180))
    assert field._mode == 'move' and field.selection != before
    if action == 'undo':
        field.undo()
        expected = []
    elif action == 'delete':
        # No object is selected, but this still cancels the live gesture.
        field.delete_selected()
        expected = [mark]
    elif action == 'switch':
        field.set_tool('arrow')
        expected = [mark]
    else:
        field.redo()
        expected = [mark, later]
    assert field.selection == before and field._mode is None
    QTest.mouseMove(field, QPoint(350,190))
    QTest.mouseRelease(field, Qt.MouseButton.LeftButton, pos=QPoint(350,190))
    assert field.selection == before and field.annotations == expected
    if action == 'undo':
        field.redo()
        assert field.annotations == [mark]
    elif action == 'redo':
        field.undo()
        assert field.annotations == [mark]
    else:
        field.undo()
        assert field.annotations == []
        field.redo()
        assert field.annotations == [mark]


def test_switching_tool_mid_stroke_discards_uncommitted_points(field):
    field.set_tool('rectangle')
    QTest.mousePress(field, Qt.MouseButton.LeftButton, pos=QPoint(160,120))
    QTest.mouseMove(field, QPoint(190,160))
    assert field._mode == 'draw' and field._points
    field.set_tool('arrow')
    QTest.mouseMove(field, QPoint(220,180))
    QTest.mouseRelease(field, Qt.MouseButton.LeftButton, pos=QPoint(220,180))
    assert not field.annotations and not field._points and field._mode is None
    assert not field.document.can_undo
