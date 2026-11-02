"""Geometry and snapshot history shared by the two annotation surfaces.

This module accepts the immutable mark dataclass structurally. It deliberately
does not import the renderer module, so both editors can use it without a cycle.
All positions and tolerances are in source-image pixels.
"""
from __future__ import annotations

from dataclasses import replace
from math import atan2, cos, hypot, sin

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPolygonF


def _width(mark):
    return max(.5, float(mark.width))


def _text_metrics(mark):
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(max(16, round(_width(mark) * 7)))
    font.setWeight(QFont.Weight.DemiBold)
    return QFontMetrics(font)


def _arrow_head(mark):
    first, last = mark.points[0], mark.points[-1]
    if first == last:
        return QPolygonF()
    angle = atan2(last.y() - first.y(), last.x() - first.x())
    length = max(12., _width(mark) * 3.5)
    wing = .48
    return QPolygonF([last,
        QPointF(last.x() - length * cos(angle - wing), last.y() - length * sin(angle - wing)),
        QPointF(last.x() - length * cos(angle + wing), last.y() - length * sin(angle + wing))])


def _number_rect(mark):
    radius = max(12., _width(mark) * 3)
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(max(14, round(radius * 1.1)))
    font.setWeight(QFont.Weight.Bold)
    width = max(radius * 2, QFontMetrics(font).horizontalAdvance(mark.text or "1") + radius)
    point = mark.points[0]
    return QRectF(point.x() - width / 2, point.y() - radius, width, radius * 2)


def annotation_bounds(mark):
    """Visible source-pixel bounds, including stroke and rendered text metrics."""
    if not mark.points:
        return QRectF()
    first, last = mark.points[0], mark.points[-1]
    if mark.kind == "text":
        metrics = _text_metrics(mark)
        # The renderer uses this baseline rather than QPointF as a baseline.
        baseline = first + QPointF(0, metrics.ascent())
        area = QRectF(metrics.boundingRect(mark.text))
        # Qt's Windows offscreen font engine can return the sentinel origin
        # (100000,100000) when no fonts were enumerated, although drawText still
        # paints missing-glyph boxes at the requested baseline. Keep their
        # selectable geometry useful using the same engine's advance/ascent.
        if abs(area.left()) > 10000 or abs(area.top()) > 10000:
            area = QRectF(0, -metrics.ascent(), metrics.horizontalAdvance(mark.text), metrics.height())
        return area.translated(baseline)
    if mark.kind == "number":
        return _number_rect(mark)
    if mark.kind == "redact":
        return QRectF(QRectF(first, last).normalized().toAlignedRect())
    if mark.kind == "mosaic":
        return QRectF(first, last).normalized()
    xs = [point.x() for point in mark.points]
    ys = [point.y() for point in mark.points]
    if mark.kind == "arrow":
        for point in _arrow_head(mark):
            xs.append(point.x())
            ys.append(point.y())
    extent = max(4., _width(mark) * 4) / 2 if mark.kind == "highlight" else _width(mark) / 2
    return QRectF(min(xs) - extent, min(ys) - extent,
                  max(xs) - min(xs) + 2 * extent, max(ys) - min(ys) + 2 * extent)


def _segment_distance(point, first, last):
    dx, dy = last.x() - first.x(), last.y() - first.y()
    squared = dx * dx + dy * dy
    if squared == 0:
        return hypot(point.x() - first.x(), point.y() - first.y())
    t = max(0., min(1., ((point.x() - first.x()) * dx + (point.y() - first.y()) * dy) / squared))
    return hypot(point.x() - first.x() - t * dx, point.y() - first.y() - t * dy)


def _hits(mark, point, tolerance):
    if not mark.points:
        return False
    bounds = annotation_bounds(mark).adjusted(-tolerance, -tolerance, tolerance, tolerance)
    if not bounds.contains(point):
        return False
    if mark.kind in {"text", "number", "rectangle", "redact", "mosaic"}:
        # Selecting a shape's interior is intentional: it is easier to grab a
        # rectangle than requiring a pixel-perfect hit on its outline.
        return True
    if mark.kind == "ellipse":
        area = QRectF(mark.points[0], mark.points[-1]).normalized()
        rx, ry = area.width() / 2 + tolerance + _width(mark) / 2, area.height() / 2 + tolerance + _width(mark) / 2
        if rx <= 0 or ry <= 0:
            return False
        center = area.center()
        return ((point.x() - center.x()) / rx) ** 2 + ((point.y() - center.y()) / ry) ** 2 <= 1
    threshold = tolerance + (max(4., _width(mark) * 4) / 2 if mark.kind == "highlight" else _width(mark) / 2)
    if mark.kind == "arrow" and _arrow_head(mark).containsPoint(point, Qt.FillRule.OddEvenFill):
        return True
    points = mark.points
    if len(points) == 1:
        return _segment_distance(point, points[0], points[0]) <= threshold
    return any(_segment_distance(point, first, last) <= threshold for first, last in zip(points, points[1:]))


def hit_annotation(marks, point, tolerance=6):
    """Return the topmost mark hit by a source-pixel point, or ``None``."""
    point = QPointF(point)
    tolerance = max(0., float(tolerance))
    for index in range(len(marks) - 1, -1, -1):
        if _hits(marks[index], point, tolerance):
            return index
    return None


def translate_annotation(mark, delta, bounds=None):
    """Copy a mark with one common translation; preserve its shape and style.

    ``bounds`` optionally limits the visible mark to a source-image rectangle.
    A mark larger than that rectangle keeps its dimensions and aligns its
    leading edge rather than being distorted to fit.
    """
    delta = QPointF(delta)
    if bounds is not None and mark.points:
        boundary = QRectF(bounds)
        area = annotation_bounds(mark)
        if boundary.isValid() and area.isValid():
            min_x, max_x = boundary.left() - area.left(), boundary.right() - area.right()
            min_y, max_y = boundary.top() - area.top(), boundary.bottom() - area.bottom()
            delta.setX(max(min_x, min(max_x, delta.x())) if min_x <= max_x else min_x)
            delta.setY(max(min_y, min(max_y, delta.y())) if min_y <= max_y else min_y)
    return replace(mark, points=tuple(QPointF(point) + delta for point in mark.points))


def _snapshot(marks):
    # QPointF is mutable even inside a frozen dataclass. Snapshot its values so
    # a gesture or external list assignment cannot mutate an earlier undo step.
    return tuple(replace(mark, points=tuple(QPointF(point) for point in mark.points)) for mark in marks)


class AnnotationHistory:
    """Snapshot stack usable by editors which retain their own mark list."""
    def __init__(self):
        self._undo = []
        self._redo = []

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def record(self, before, after):
        before, after = _snapshot(before), _snapshot(after)
        if before == after:
            return False
        self._undo.append(before)
        self._redo.clear()
        return True

    def undo(self, current):
        if not self._undo:
            return None
        self._redo.append(_snapshot(current))
        return list(_snapshot(self._undo.pop()))

    def redo(self, current):
        if not self._redo:
            return None
        self._undo.append(_snapshot(current))
        return list(_snapshot(self._redo.pop()))

    def clear(self):
        self._undo.clear()
        self._redo.clear()

    def transform(self, mapper):
        """Map every historical mark without creating an object-edit step."""
        self._undo = [_snapshot(mapper(mark) for mark in state) for state in self._undo]
        self._redo = [_snapshot(mapper(mark) for mark in state) for state in self._redo]


class AnnotationDocument:
    """Editable marks with one undo step per committed user action.

    Editors may assign ``annotations`` while previewing a drag, then call
    ``commit(current_marks, before=pressed_snapshot)`` once on mouse release.
    No image or original pixels are stored in this document.
    """
    def __init__(self, annotations=()):
        self.annotations = list(_snapshot(annotations))
        self.history = AnnotationHistory()

    @property
    def marks(self):
        return self.annotations

    @marks.setter
    def marks(self, value):
        self.annotations = list(value)

    @property
    def can_undo(self):
        return self.history.can_undo

    @property
    def can_redo(self):
        return self.history.can_redo

    def snapshot(self):
        return _snapshot(self.annotations)

    def restore(self, snapshot):
        """Restore a transient preview without adding or deleting history."""
        self.annotations = list(_snapshot(snapshot))

    def commit(self, after, before=None):
        before = self.annotations if before is None else before
        after = _snapshot(after)
        changed = self.history.record(before, after)
        self.annotations = list(_snapshot(after))
        return changed

    def transform(self, mapper, *, include_current=True):
        """Move all coordinate history with a translated screenshot selection.

        Use ``include_current=False`` if the live drag has already moved the
        current mark list, and only its older undo/redo states need the offset.
        """
        if include_current:
            self.annotations = list(_snapshot(mapper(mark) for mark in self.annotations))
        self.history.transform(mapper)

    def add(self, mark):
        return self.commit([*self.annotations, mark])

    def replace_at(self, index, mark):
        values = list(self.annotations)
        values[index] = mark
        return self.commit(values)

    def remove_at(self, index):
        values = list(self.annotations)
        del values[index]
        return self.commit(values)

    def move_at(self, index, delta, bounds=None):
        return self.replace_at(index, translate_annotation(self.annotations[index], delta, bounds))

    def clear(self):
        return self.commit([])

    def reset(self, annotations=()):
        self.restore(annotations)
        self.history.clear()

    def undo(self):
        values = self.history.undo(self.annotations)
        if values is None:
            return False
        self.annotations = values
        return True

    def redo(self):
        values = self.history.redo(self.annotations)
        if values is None:
            return False
        self.annotations = values
        return True
