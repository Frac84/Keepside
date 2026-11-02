"""Source-pixel selection and object-edit history without a real screen."""
from dataclasses import replace

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from snapvault.annotation import Annotation, render_annotations
from snapvault.annotation_document import (AnnotationDocument, AnnotationHistory,
    annotation_bounds, hit_annotation, translate_annotation)


@pytest.fixture(scope="session", autouse=True)
def app():
    value = QApplication.instance() or QApplication([])
    yield value


def mark(kind, *points, **style):
    return Annotation(kind, tuple(QPointF(*p) for p in points), **style)


def test_topmost_object_hit_and_line_does_not_select_its_entire_box():
    rectangle = mark("rectangle", (10, 10), (90, 90), width=4)
    diagonal = mark("arrow", (10, 10), (90, 90), width=4)
    assert hit_annotation([rectangle, diagonal], QPointF(50, 50), 2) == 1
    assert hit_annotation([rectangle, diagonal], QPointF(20, 70), 2) == 0
    assert hit_annotation([diagonal], QPointF(20, 70), 2) is None
    assert hit_annotation([diagonal], QPointF(44, 47), 1) == 0
    assert hit_annotation([rectangle], QPointF(200, 200)) is None


def test_ellipse_uses_ellipse_shape_and_paths_use_segment_distance():
    ellipse = mark("ellipse", (10, 10), (110, 70), width=2)
    assert hit_annotation([ellipse], QPointF(60, 40), 0) == 0
    assert hit_annotation([ellipse], QPointF(11, 11), 0) is None
    path = mark("pen", (10, 10), (10, 90), (90, 90), width=4)
    assert hit_annotation([path], QPointF(10, 45), 0) == 0
    assert hit_annotation([path], QPointF(45, 90), 0) == 0
    assert hit_annotation([path], QPointF(50, 50), 4) is None
    point = mark("pen", (30, 30), width=6)
    assert hit_annotation([point], QPointF(32, 31), 0) == 0


def test_wide_highlighter_and_arrow_head_can_be_selected():
    highlight = mark("highlight", (20, 50), (80, 50), width=6)
    assert hit_annotation([highlight], QPointF(50, 60), 0) == 0
    arrow = mark("arrow", (20, 60), (100, 60), width=6)
    assert hit_annotation([arrow], QPointF(85, 65), 0) == 0  # inside filled arrow wing
    assert annotation_bounds(arrow).contains(QPointF(100, 60))


def test_text_and_number_bounds_agree_with_rendered_pixels(app):
    image = QImage(400, 180, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    text = mark("text", (35, 30), text="Edit 你好", width=4, color="#000000")
    bounds = annotation_bounds(text)
    assert bounds.width() > 60 and bounds.height() > 10
    assert hit_annotation([text], bounds.center(), 0) == 0
    rendered = render_annotations(image, [text])
    ink = [QPointF(x + .5, y + .5) for y in range(rendered.height())
           for x in range(rendered.width()) if rendered.pixelColor(x, y) != QColor("white")]
    assert ink and all(bounds.adjusted(-1, -1, 1, 1).contains(p) for p in ink)
    number = mark("number", (200, 80), text="1234", width=4)
    badge = annotation_bounds(number)
    assert badge.width() > badge.height()
    assert hit_annotation([number], badge.center(), 0) == 0


def test_redaction_bounds_round_out_and_translation_preserves_every_point():
    redaction = mark("redact", (10.2, 20.8), (40.1, 50.2))
    assert annotation_bounds(redaction) == QRectF(10, 20, 31, 31)
    moved = translate_annotation(redaction, QPointF(7, -3))
    assert moved.points == (QPointF(17.2, 17.8), QPointF(47.1, 47.2))
    assert moved.color == redaction.color and moved.width == redaction.width
    assert redaction.points[0] == QPointF(10.2, 20.8)


def test_translation_clamps_common_offset_without_distorting_shape():
    shape = mark("rectangle", (10, 10), (40, 40), width=4)
    moved = translate_annotation(shape, QPointF(-100, 100), QRectF(0, 0, 100, 100))
    assert annotation_bounds(moved) == QRectF(0, 66, 34, 34)
    assert moved.points[1] - moved.points[0] == QPointF(30, 30)
    oversize = mark("rectangle", (10, 10), (150, 150), width=4)
    moved = translate_annotation(oversize, QPointF(500, 500), QRectF(0, 0, 100, 100))
    assert annotation_bounds(moved).topLeft() == QPointF(0, 0)
    assert moved.points[1] - moved.points[0] == QPointF(140, 140)


def test_edit_delete_and_move_are_independent_undo_steps():
    first = mark("text", (10, 10), text="before")
    document = AnnotationDocument()
    assert document.add(first)
    changed = replace(first, text="after", color="#0078d4")
    assert document.replace_at(0, changed)
    assert document.move_at(0, QPointF(5, 6))
    moved = document.snapshot()
    assert document.remove_at(0) and document.annotations == []
    assert document.undo() and document.snapshot() == moved
    assert document.undo() and document.annotations == [changed]
    assert document.undo() and document.annotations == [first]
    assert document.undo() and document.annotations == []
    assert not document.undo()
    for expected in ([first], [changed], list(moved), []):
        assert document.redo() and document.annotations == expected
    assert not document.redo()


def test_drag_preview_commits_once_and_history_snapshots_mutable_point_values():
    first = mark("line", (10, 10), (30, 30))
    document = AnnotationDocument([first])
    before = document.snapshot()
    for offset in range(1, 30):
        document.annotations = [translate_annotation(first, QPointF(offset, offset))]
    after = document.snapshot()
    assert document.commit(after, before=before)
    assert document.undo() and document.snapshot() == before
    assert not document.undo()  # not 29 mouse frames in the stack
    assert document.redo() and document.snapshot() == after
    # Mutating an externally held mark cannot rewrite an older history entry.
    document.annotations[0].points[0].setX(999)
    assert document.undo() and document.snapshot() == before


def test_noop_edit_keeps_redo_and_new_branch_discards_redo():
    first = mark("rectangle", (10, 10), (30, 30))
    second = mark("number", (60, 60), text="1")
    document = AnnotationDocument()
    document.add(first)
    document.add(second)
    document.undo()
    assert document.can_redo and not document.replace_at(0, first)
    assert document.can_redo
    document.replace_at(0, replace(first, color="#000000"))
    assert not document.can_redo
    assert document.clear() and document.annotations == []
    assert document.undo() and document.annotations[0].color == "#000000"
    document.reset()
    assert not document.annotations and not document.can_undo and not document.can_redo


def test_history_compatibility_does_not_mutate_input_lists():
    history = AnnotationHistory()
    empty = []
    one = [mark("line", (1, 2), (3, 4))]
    assert history.record(empty, one)
    assert history.undo(one) == [] and one[0].points[0] == QPointF(1, 2)
    assert history.redo(empty) == one and empty == []
    history.clear()
    assert not history.can_undo and not history.can_redo


def test_translating_selection_moves_all_undo_and_redo_coordinates_once():
    first = mark("rectangle", (10, 10), (30, 30))
    second = mark("number", (60, 60), text="1")
    document = AnnotationDocument()
    document.add(first)
    document.add(second)
    document.undo()
    delta = QPointF(50, 20)
    # The screenshot already rendered a live move; commit history coordinates.
    document.annotations = [translate_annotation(item, delta) for item in document.annotations]
    document.transform(lambda item: translate_annotation(item, delta), include_current=False)
    assert document.annotations[0].points[0] == QPointF(60, 30)
    assert document.redo() and document.annotations[1].points[0] == QPointF(110, 80)
    assert document.undo() and document.annotations[0].points[0] == QPointF(60, 30)
    assert document.undo() and document.annotations == []
