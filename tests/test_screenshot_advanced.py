"""Frozen synthetic pixels exercise selection edits and inline annotation."""
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
import pytest

from snapvault.annotation import Annotation
from snapvault.screenshot import ScreenshotOverlay, ScreenshotSession
from test_screenshot import app, flush, frozen


@pytest.fixture
def field(app):
    widget = ScreenshotOverlay(QRect(-800, 50, 400, 300), frozen())
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    flush(app)
    yield widget
    widget.close()
    flush(app)


def drag(widget, start, end):
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*start))
    QTest.mouseMove(widget, QPoint(*end))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*end))


def test_move_carries_marks_and_preserves_source(field):
    field.set_selection(QRect(40, 40, 120, 100))
    field.add_annotation(Annotation('redact', (QPointF(120, 120), QPointF(160, 160)), '#111111'))
    before = field.crop()
    drag(field, (100, 90), (150, 120))
    assert field.selection == QRect(90, 70, 120, 100)
    assert field.annotations[0].points == (QPointF(220, 180), QPointF(260, 220))
    after = field.crop()
    assert before.pixelColor(50, 50) == after.pixelColor(50, 50) == QColor('#111111')
    assert field.source.pixelColor(130, 130) == QColor('red')
    assert field.source.pixelColor(230, 190) == QColor('red')
    field.undo()
    assert field.crop().pixelColor(50, 50) == QColor('red')
    field.redo()
    assert field.crop().pixelColor(50, 50) == QColor('#111111')


@pytest.mark.parametrize('handle,end,expected', [
    ('nw', (60, 60), QRect(60, 60, 120, 100)),
    ('n', (129, 60), QRect(80, 60, 100, 100)),
    ('ne', (199, 60), QRect(80, 60, 120, 100)),
    ('e', (199, 119), QRect(80, 80, 120, 80)),
    ('se', (199, 179), QRect(80, 80, 120, 100)),
    ('s', (129, 179), QRect(80, 80, 100, 100)),
    ('sw', (60, 179), QRect(60, 80, 120, 100)),
    ('w', (60, 119), QRect(60, 80, 120, 80)),
])
def test_all_resize_handles_leave_marks_in_source_pixels(field, handle, end, expected):
    field.set_selection(QRect(80, 80, 100, 80))
    mark = Annotation('rectangle', (QPointF(200, 200), QPointF(240, 240)))
    field.add_annotation(mark)
    p = field._handles()[handle]
    drag(field, (p.x(), p.y()), end)
    assert field.selection == expected
    assert field.annotations == [mark]
    assert field.crop().size().toTuple() == (expected.width()*2, expected.height()*2)


def test_move_and_resize_cannot_escape_screen(field):
    field.set_selection(QRect(80, 80, 100, 80))
    drag(field, (120, 120), (399, 299))
    assert field.selection == QRect(300, 220, 100, 80)
    drag(field, (399, 299), (500, 500))
    assert field.rect().contains(field.selection)
    assert field.selection.width() >= 2 and field.selection.height() >= 2
    # A west handle cannot cross the opposite edge and invert the selection.
    drag(field, (300, 259), (399, 259))
    assert field.selection.width() == 2


def test_reselect_clears_marks_and_redo(field):
    field.set_selection(QRect(40, 40, 100, 90))
    field.add_annotation(Annotation('line', (QPointF(120, 120), QPointF(180, 180))))
    field.undo()
    assert field._redo
    drag(field, (230, 20), (330, 70))
    assert field.annotations == [] and field._redo == []
    assert field.crop().pixelColor(0, 0) == QColor('blue')


def test_resize_and_dpi_rounding_use_independent_physical_edges(app):
    image = QPixmap(500, 330)
    image.fill(QColor('white'))
    widget = ScreenshotOverlay(QRect(-100, -200, 400, 300), image)
    widget.set_selection(QRect(1, 3, 10, 20))
    assert widget.source_selection() == QRect(1, 3, 13, 22)
    assert widget.crop().size().toTuple() == (13, 22)
    assert widget.source_point(QPoint(200, 100)) == QPointF(250, 110)
    widget.close()


@pytest.mark.parametrize('tool', ['rectangle', 'ellipse', 'arrow', 'line', 'pen', 'highlight', 'mosaic', 'redact'])
def test_tools_draw_directly_without_finishing_capture(field, tool):
    field.set_selection(QRect(20, 20, 200, 150))
    finished = []
    field.finished.connect(lambda *args: finished.append(args))
    field.set_tool(tool)
    drag(field, (60, 60), (150, 120))
    assert len(field.annotations) == 1 and field.annotations[0].kind == tool
    assert finished == []
    field.undo()
    assert field.annotations == [] and field.redo_button.isEnabled()
    field.redo()
    assert len(field.annotations) == 1
    field.grab()


def test_numbering_and_branching_redo(field):
    field.set_selection(QRect(20, 20, 240, 160))
    field.set_tool('number')
    for point in (QPoint(60, 60), QPoint(120, 100)):
        QTest.mouseClick(field, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert [m.text for m in field.annotations] == ['1', '2']
    field.undo()
    QTest.mouseClick(field, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(160, 120))
    assert [m.text for m in field.annotations] == ['1', '2']
    assert not field._redo and not field.redo_button.isEnabled()


def test_inline_text_does_not_activate_shortcuts(field):
    field.set_selection(QRect(20, 20, 240, 160))
    field.set_tool('text')
    finished = []
    field.finished.connect(lambda *args: finished.append(args))
    QTest.mouseClick(field, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(70, 70))
    QTest.keyClicks(field.text_editor, 'PHOTOS M')
    assert field.tool == 'text' and finished == [] and field.annotations == []
    QTest.keyClick(field.text_editor, Qt.Key.Key_Return)
    assert field.annotations[-1].text == 'PHOTOS M' and finished == []
    QTest.mouseClick(field, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(80, 100))
    QTest.keyClicks(field.text_editor, 'discard')
    QTest.keyClick(field.text_editor, Qt.Key.Key_Escape)
    assert len(field.annotations) == 1 and field.isVisible() and not field.text_editor.isVisible()


@pytest.mark.parametrize('key,mods,action', [(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier, 'copy'),
                                           (Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier, 'save_done'),
                                           (Qt.Key.Key_P, Qt.KeyboardModifier.NoModifier, 'pin'),
                                           (Qt.Key.Key_O, Qt.KeyboardModifier.NoModifier, 'ocr')])
def test_output_actions_only_export_flattened_result(field, key, mods, action, monkeypatch, tmp_path):
    from snapvault import screenshot
    monkeypatch.setattr(screenshot.QFileDialog, 'getSaveFileName', lambda *_a, **_k: (str(tmp_path/'result.png'), 'PNG'))
    field.set_selection(QRect(20, 20, 100, 100))
    field.add_annotation(Annotation('redact', (QPointF(60, 60), QPointF(180, 180)), '#111111'))
    results = []
    field.finished.connect(lambda image, kind: results.append((image, kind)))
    QTest.keyClick(field, key, mods)
    image, kind = results[-1]
    assert kind == action and image.size().toTuple() == (200, 200)
    assert image.pixelColor(50, 50) == QColor('#111111')
    assert image.devicePixelRatio() == 1.


def test_keyboard_nudge_and_clear_cancel_preserve_session_contract(field):
    field.set_selection(QRect(100, 100, 80, 50))
    QTest.keyClick(field, Qt.Key.Key_Left)
    QTest.keyClick(field, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert field.selection.topLeft() == QPoint(99, 90)
    field.add_annotation(Annotation('redact', (QPointF(210, 200), QPointF(230, 220))))
    field.undo()
    QTest.mouseClick(field, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(110, 110))
    assert field.selection.isEmpty() and not field.annotations and not field._redo
    results, cancels = [], []
    field.finished.connect(lambda *args: results.append(args))
    field.cancelled.connect(lambda: cancels.append(True))
    QTest.keyClick(field, Qt.Key.Key_Escape)
    assert not results and cancels == [True]


def test_toolbar_is_inside_small_screen_and_initial_tool_session(app):
    session = ScreenshotSession([(QRect(0, 0, 400, 300), frozen())], initial_tool='rectangle')
    widget = session.overlays[0]
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    widget.set_selection(QRect(0, 0, 400, 300))
    flush(app)
    assert widget.tool == 'rectangle' and widget.tool_buttons['rectangle'].isChecked()
    assert widget.rect().contains(widget.toolbar.geometry())
    for button in widget.tool_buttons.values():
        assert widget.toolbar.rect().contains(button.geometry())
    for button in widget.action_buttons.values():
        assert widget.toolbar.rect().contains(button.geometry())
    session.close()
    flush(app)

def test_moving_mosaic_never_falls_back_to_unmasked_source(field):
    # An alternating source would be clearly readable without flattened mosaic.
    from PySide6.QtGui import QPainter
    painter = QPainter(field.source)
    for x in range(80, 280):
        painter.fillRect(x, 80, 1, 120, QColor('black' if x % 2 else 'white'))
    painter.end()
    field._rendered = field.source.copy()
    field.set_selection(QRect(40, 40, 100, 80))
    field.add_annotation(Annotation('mosaic', (QPointF(90, 90), QPointF(250, 190)), width=8))
    before = field.crop()
    QTest.mousePress(field, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(85, 105))
    assert field._move_preview == before
    QTest.mouseMove(field, QPoint(115, 130))
    assert field._mode == 'move' and field._move_preview == before
    # Actual widget paint contains the cached, opaque pixelated region, not
    # the raw source. Stay away from handles, caption and tool panel.
    preview = field.grab().toImage()
    ratio = preview.width()/field.width()
    point = QPoint(round(100*ratio), round(85*ratio))
    assert preview.pixelColor(point).alpha() == 255
    cached = field._move_preview.pixelColor(60, 40)
    assert cached != QColor('black') and cached != QColor('white')
    assert preview.pixelColor(point) == cached
    QTest.mouseRelease(field, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(115, 130))
    assert field._move_preview is None and len(field.annotations) == 1
