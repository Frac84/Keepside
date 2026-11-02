"""Small local image editor. Marks are kept separate from the original pixels."""
from __future__ import annotations

from .i18n import tr
from .branding import app_name

from dataclasses import dataclass, replace
from math import atan2, cos, isfinite, sin
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSaveFile, QSignalBlocker, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QImage, QImageWriter, QKeySequence,
                          QPainter, QPainterPath, QPen, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QAbstractSpinBox, QButtonGroup, QComboBox, QDialog, QFileDialog,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from .glass import configure_window_glass, refresh_window_glass
from .theme import native_titlebar, theme
from .annotation_document import (AnnotationDocument, annotation_bounds, hit_annotation,
                                  translate_annotation)


TOOLS = (("rectangle", "矩形"), ("ellipse", "椭圆"), ("arrow", "箭头"),
         ("line", "直线"), ("pen", "画笔"), ("highlight", "荧光笔"),
         ("text", "文字"), ("number", "序号"), ("mosaic", "马赛克"),
         ("redact", "遮挡"))

TOOL_KEYS = {Qt.Key.Key_Q: "select", Qt.Key.Key_R: "rectangle", Qt.Key.Key_E: "ellipse",
             Qt.Key.Key_A: "arrow", Qt.Key.Key_L: "line", Qt.Key.Key_B: "pen",
             Qt.Key.Key_H: "highlight", Qt.Key.Key_T: "text", Qt.Key.Key_N: "number",
             Qt.Key.Key_M: "mosaic", Qt.Key.Key_X: "redact"}


@dataclass(frozen=True)
class Annotation:
    """One mark in source-image pixels.

    ``number`` uses one centre point and a numeric ``text`` value. ``highlight``
    uses a freehand path and a translucent stroke four times ``width``.
    ``redact`` is an opaque, flattened rectangle; alpha in ``color`` is ignored.
    """
    kind: str
    points: tuple[QPointF, ...]
    color: str = "#e81123"
    width: float = 4
    text: str = ""


def _draw_mark(painter, mark, *, preview=False):
    """Paint in original-image coordinates; the canvas supplies its transform."""
    if not mark.points:
        return
    color = QColor(mark.color)
    width = max(.5, float(mark.width))
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(color, width, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    first, last = mark.points[0], mark.points[-1]
    if mark.kind == "rectangle":
        painter.drawRect(QRectF(first, last).normalized())
    elif mark.kind == "ellipse":
        painter.drawEllipse(QRectF(first, last).normalized())
    elif mark.kind == "line":
        painter.drawLine(first, last)
    elif mark.kind == "arrow":
        painter.drawLine(first, last)
        if first != last:
            angle = atan2(last.y() - first.y(), last.x() - first.x())
            length = max(12., width * 3.5)
            wing = .48
            head = QPolygonF([last,
                QPointF(last.x() - length * cos(angle - wing), last.y() - length * sin(angle - wing)),
                QPointF(last.x() - length * cos(angle + wing), last.y() - length * sin(angle + wing))])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(head)
    elif mark.kind in {"pen", "highlight"}:
        if mark.kind == "highlight":
            color.setAlpha(85)
            painter.setPen(QPen(color, max(4., width * 4), Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if len(mark.points) == 1:
            painter.drawPoint(first)
        else:
            stroke = QPainterPath(first)
            for point in mark.points[1:]:
                stroke.lineTo(point)
            painter.drawPath(stroke)
    elif mark.kind == "text" and mark.text:
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(16, round(width * 7)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(first + QPointF(0, painter.fontMetrics().ascent()), mark.text)
    elif mark.kind == "number":
        radius = max(12., width * 3)
        label = mark.text if mark.text else "1"
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(14, round(radius * 1.1)))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        # Extra digits widen the badge instead of being silently clipped.
        badge_width = max(radius * 2, painter.fontMetrics().horizontalAdvance(label) + radius)
        badge = QRectF(first.x() - badge_width / 2, first.y() - radius, badge_width, radius * 2)
        color.setAlpha(255)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(badge, radius, radius)
        # Keep numbers legible when the chosen marker colour is white/yellow.
        lightness = .2126 * color.redF() + .7152 * color.greenF() + .0722 * color.blueF()
        painter.setPen(QColor("#111111" if lightness > .65 else "#ffffff"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)
    elif mark.kind == "redact":
        color.setAlpha(255)
        # Integer outward rounding covers every selected source pixel, including
        # fractional boundaries, without leaving antialiased transparent edges.
        painter.fillRect(QRectF(first, last).normalized().toAlignedRect(), color)
    elif mark.kind == "mosaic" and preview:
        shade = QColor(color)
        shade.setAlpha(45)
        painter.setBrush(shade)
        painter.setPen(QPen(color, width, Qt.PenStyle.DashLine))
        painter.drawRect(QRectF(first, last).normalized())


def render_annotations(image: QImage, annotations) -> QImage:
    """Return flattened export pixels without altering the source or marks.

    A mosaic samples the pixels already drawn below it. The exported PNG holds
    the flattened result only, so covered details do not remain in extra layers.
    """
    if image.isNull():
        raise ValueError(tr('图片为空，无法标注'))
    result = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied).copy()
    result.setDevicePixelRatio(1.)
    for mark in annotations:
        if mark.kind == "mosaic" and len(mark.points) > 1:
            area = QRectF(mark.points[0], mark.points[-1]).normalized().toAlignedRect().intersected(result.rect())
            if area.width() < 2 or area.height() < 2:
                continue
            block = max(6, round(mark.width * 3))
            tile = result.copy(area).scaled(max(1, area.width() // block), max(1, area.height() // block),
                                           Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            tile = tile.scaled(area.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
            painter = QPainter(result)
            painter.drawImage(area.topLeft(), tile)
            painter.end()
        else:
            painter = QPainter(result)
            _draw_mark(painter, mark)
            painter.end()
    return result


class AnnotationCanvas(QWidget):
    changed = Signal()
    selectionChanged = Signal(object)
    toolChanged = Signal(str)
    editingChanged = Signal(bool)

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        if image.isNull():
            raise ValueError(tr('图片为空，无法标注'))
        self.source = image.copy()
        self.source.setDevicePixelRatio(1.)
        self.document = AnnotationDocument()
        self._rendered = render_annotations(self.source, ())
        self.tool = "rectangle"
        self.color = "#e81123"
        self.stroke_width = 4
        self._points = []
        self.selected_index = None
        self._drag_origin = self._drag_before = self._drag_mark = None
        self._drag_base = self._drag_pixels = self._drag_rect = None
        self._text_point = self._text_index = None
        self.setObjectName("annotationCanvas")
        self.setAccessibleName(tr('图片标注画布'))
        self.setAccessibleDescription(tr('选择工具可点选、拖动或删除单个标注；双击文字修改。Ctrl+Z 撤销，Ctrl+Y 重做。'))
        self.setMinimumSize(360, 230)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.text_editor = QLineEdit(self)
        self.text_editor.setObjectName("annotationTextEditor")
        self.text_editor.setPlaceholderText(tr('输入文字，Enter 确认，Esc 取消'))
        self.text_editor.setMaxLength(2000)
        self.text_editor.setAccessibleName(tr('标注文字'))
        self.text_editor.installEventFilter(self)
        self.text_editor.returnPressed.connect(self.commit_text)
        self.text_editor.editingFinished.connect(self.commit_text)
        self.text_editor.hide()

    @property
    def annotations(self):
        return self.document.annotations

    @annotations.setter
    def annotations(self, value):
        self.document.annotations = list(value)

    @property
    def selected_annotation(self):
        if self.selected_index is not None and 0 <= self.selected_index < len(self.annotations):
            return self.annotations[self.selected_index]
        return None

    def image_rect(self):
        available = QRectF(self.rect()).adjusted(18, 18, -18, -18)
        ratio = min(available.width() / self.source.width(), available.height() / self.source.height())
        width, height = self.source.width() * ratio, self.source.height() * ratio
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def image_point(self, point, *, clamp=False):
        area = self.image_rect()
        if not clamp and not area.contains(point):
            return None
        x = (point.x() - area.x()) * self.source.width() / area.width()
        y = (point.y() - area.y()) * self.source.height() / area.height()
        return QPointF(max(0., min(self.source.width() - 1., x)), max(0., min(self.source.height() - 1., y)))

    def rendered_image(self):
        self.commit_text()
        return self._rendered.copy()

    def add_annotation(self, mark):
        if mark.kind not in {name for name, _label in TOOLS} or not mark.points:
            raise ValueError(tr('不支持的标注'))
        self._cancel_drag()
        self.document.add(mark)
        self.select_annotation(len(self.annotations) - 1)
        self._refresh_pixels()

    def _refresh_pixels(self):
        self._rendered = render_annotations(self.source, self.annotations)
        self.changed.emit()
        self.update()

    def select_annotation(self, index):
        self._cancel_drag()
        self.selected_index = index if index is not None and 0 <= index < len(self.annotations) else None
        mark = self.selected_annotation
        if mark is not None:
            self.color, self.stroke_width = mark.color, mark.width
        self.selectionChanged.emit(mark)
        self.update()

    def set_color(self, value):
        self._cancel_drag()
        self.color = value
        mark = self.selected_annotation
        if mark is not None and mark.color != value:
            self.document.replace_at(self.selected_index, replace(mark, color=value))
            self.selectionChanged.emit(self.selected_annotation)
            self._refresh_pixels()

    def set_width(self, value):
        self._cancel_drag()
        self.stroke_width = value
        mark = self.selected_annotation
        if mark is not None and mark.width != value:
            self.document.replace_at(self.selected_index, replace(mark, width=value))
            self.selectionChanged.emit(self.selected_annotation)
            self._refresh_pixels()

    def delete_selected(self):
        self._cancel_drag()
        self.commit_text()
        if self.selected_annotation is not None:
            self.document.remove_at(self.selected_index)
            self.select_annotation(None)
            self._refresh_pixels()

    def undo(self):
        self._cancel_drag()
        self.commit_text()
        self._points = []
        if self.document.undo():
            self.select_annotation(None)
            self._refresh_pixels()

    def redo(self):
        self._cancel_drag()
        self.commit_text()
        self._points = []
        if self.document.redo():
            self.select_annotation(None)
            self._refresh_pixels()

    @property
    def can_undo(self):
        return self.document.can_undo

    @property
    def can_redo(self):
        return self.document.can_redo

    def next_number(self):
        """Continue the visible numbering, so undoing a badge reuses its number."""
        values = [int(mark.text) for mark in self.annotations
                  if mark.kind == "number" and mark.text.isdecimal()]
        return max(values, default=0) + 1

    def reset(self):
        self._cancel_drag()
        self.discard_text()
        self._points = []
        self.document.reset()
        self.select_annotation(None)
        self._refresh_pixels()

    def set_tool(self, tool):
        if tool != "select" and tool not in {name for name, _label in TOOLS}:
            raise ValueError(tr('不支持的标注工具'))
        self._cancel_drag()
        self.commit_text()
        self._points = []
        self.tool = tool
        self.select_annotation(None)
        self.setCursor(Qt.CursorShape.IBeamCursor if tool == "text" else
                       Qt.CursorShape.ArrowCursor if tool == "select" else Qt.CursorShape.CrossCursor)
        self.toolChanged.emit(tool)
        self.update()

    def _cancel_drag(self):
        """Roll back a transient gesture before another command edits history."""
        before = self._drag_before
        self._drag_origin = self._drag_before = self._drag_mark = None
        self._drag_base = self._drag_pixels = self._drag_rect = None
        if before is not None:
            self.document.restore(before)
            self._rendered = render_annotations(self.source, self.annotations)
            self.update()

    def _valid_drag(self):
        return (self._drag_origin is not None and self._drag_before is not None
                and self._drag_mark is not None and self.selected_annotation is not None)

    def _widget_point(self, point):
        area = self.image_rect()
        return area.topLeft() + QPointF(point.x() * area.width() / self.source.width(),
                                        point.y() * area.height() / self.source.height())

    def _begin_text(self, point, index=None):
        self.commit_text()
        self._text_point, self._text_index = QPointF(point), index
        mark = self.annotations[index] if index is not None else None
        self._text_color = mark.color if mark is not None else self.color
        self._text_width = mark.width if mark is not None else self.stroke_width
        self.text_editor.setText(mark.text if mark is not None else "")
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(12, round(max(16, self._text_width * 7) *
                                       self.image_rect().width() / self.source.width())))
        self.text_editor.setFont(font)
        at = self._widget_point(point)
        width = max(180, min(360, self.width() - 12))
        self.text_editor.setGeometry(round(max(6, min(at.x(), self.width() - width - 6))),
                                     round(max(6, min(at.y(), self.height() - 48))), width,
                                     max(36, font.pixelSize() + 12))
        self.text_editor.show()
        self.text_editor.raise_()
        self.editingChanged.emit(True)
        self.text_editor.setFocus()
        self.text_editor.selectAll()

    def edit_selected_text(self):
        self._cancel_drag()
        mark = self.selected_annotation
        if mark is not None and mark.kind == "text":
            self._begin_text(mark.points[0], self.selected_index)

    def discard_text(self):
        if self._text_point is None:
            return
        self._text_point = self._text_index = None
        self.text_editor.hide()
        self.text_editor.clear()
        self.editingChanged.emit(False)
        self.setFocus()

    def commit_text(self):
        if self._text_point is None:
            return
        point, index, value = self._text_point, self._text_index, self.text_editor.text()
        self._text_point = self._text_index = None
        self.text_editor.hide()
        self.text_editor.clear()
        self.editingChanged.emit(False)
        if value.strip():
            mark = Annotation("text", (point,), self._text_color, self._text_width, value)
            if index is None:
                self.add_annotation(mark)
            elif index < len(self.annotations):
                self.document.replace_at(index, mark)
                self.select_annotation(index)
                self._refresh_pixels()
        elif index is not None and 0 <= index < len(self.annotations):
            self.document.remove_at(index)
            self.select_annotation(None)
            self._refresh_pixels()
        self.setFocus()

    def eventFilter(self, watched, event):
        if watched is self.text_editor and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.discard_text()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.commit_text()
                return True
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme()["viewer_canvas"]))
        area = self.image_rect()
        # A checkerboard makes transparent source images legible without
        # painting any background into the exported original pixels.
        painter.save()
        painter.setClipRect(area)
        painter.fillRect(area, QColor("#efefef"))
        for y in range(int(area.top()), int(area.bottom()) + 1, 16):
            for x in range(int(area.left()), int(area.right()) + 1, 16):
                if ((x - int(area.left())) // 16 + (y - int(area.top())) // 16) % 2:
                    painter.fillRect(x, y, 16, 16, QColor("#dedede"))
        painter.restore()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(area, self._drag_base if self._drag_base is not None else self._rendered)
        if self._drag_mark is not None and self.selected_annotation is not None:
            painter.save()
            painter.setClipRect(area)
            painter.translate(area.topLeft())
            painter.scale(area.width() / self.source.width(), area.height() / self.source.height())
            mark = self.selected_annotation
            if mark.kind == "mosaic" and self._drag_pixels is not None:
                delta = mark.points[0] - self._drag_mark.points[0]
                painter.drawImage(QRectF(self._drag_rect).translated(delta), self._drag_pixels)
            else:
                _draw_mark(painter, mark, preview=True)
            painter.restore()
        if self._points:
            painter.save()
            painter.setClipRect(area)
            painter.translate(area.topLeft())
            painter.scale(area.width() / self.source.width(), area.height() / self.source.height())
            _draw_mark(painter, Annotation(self.tool, tuple(self._points), self.color, self.stroke_width), preview=True)
            painter.restore()
        mark = self.selected_annotation
        if mark is not None:
            bounds = annotation_bounds(mark)
            bounds = QRectF(self._widget_point(bounds.topLeft()), self._widget_point(bounds.bottomRight()))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(theme()["accent"]), 1, Qt.PenStyle.DashLine))
            painter.drawRect(bounds.adjusted(-3, -3, 3, 3))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.commit_text()
            self.setFocus()
            point = self.image_point(event.position())
            if point is not None:
                if self.tool == "select":
                    tolerance = 6 * self.source.width() / max(1, self.image_rect().width())
                    index = hit_annotation(self.annotations, point, tolerance)
                    self.select_annotation(index)
                    if index is not None:
                        self._drag_origin = point
                        self._drag_before = self.document.snapshot()
                        self._drag_mark = self.annotations[index]
                        # Covered pixels remain covered for the complete gesture;
                        # the old location is revealed only after the user drops it.
                        if self._drag_mark.kind in {"mosaic", "redact"}:
                            self._drag_base = self._rendered.copy()
                        else:
                            self._drag_base = render_annotations(self.source,
                                [mark for at, mark in enumerate(self.annotations) if at != index])
                        self._drag_rect = annotation_bounds(self._drag_mark).toAlignedRect().intersected(self.source.rect())
                        self._drag_pixels = self._rendered.copy(self._drag_rect)
                    event.accept()
                    return
                self.select_annotation(None)
                self._points = [point]
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if not self._valid_drag():
                self._cancel_drag()
                event.accept()
                return
            point = self.image_point(event.position(), clamp=True)
            self.annotations[self.selected_index] = translate_annotation(self._drag_mark,
                point - self._drag_origin, QRectF(self.source.rect()))
            self.update()
            event.accept()
            return
        if self._points and event.buttons() & Qt.MouseButton.LeftButton:
            point = self.image_point(event.position(), clamp=True)
            if self.tool in {"pen", "highlight"}:
                self._points.append(point)
            elif self.tool not in {"text", "number"}:
                self._points = [self._points[0], point]
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_origin is not None and event.button() == Qt.MouseButton.LeftButton:
            if not self._valid_drag():
                self._cancel_drag()
                event.accept()
                return
            point = self.image_point(event.position(), clamp=True)
            self.annotations[self.selected_index] = translate_annotation(self._drag_mark,
                point - self._drag_origin, QRectF(self.source.rect()))
            self.document.commit(self.document.snapshot(), before=self._drag_before)
            self._drag_origin = self._drag_before = self._drag_mark = None
            self._drag_base = self._drag_pixels = self._drag_rect = None
            self._refresh_pixels()
            event.accept()
            return
        if self._points and event.button() == Qt.MouseButton.LeftButton:
            points = list(self._points)
            self._points = []
            if self.tool == "text":
                self._begin_text(points[0])
            elif self.tool == "number":
                self.add_annotation(Annotation("number", (points[0],), self.color,
                                               self.stroke_width, str(self.next_number())))
            else:
                end = self.image_point(event.position(), clamp=True)
                if self.tool in {"pen", "highlight"}:
                    if points[-1] != end:
                        points.append(end)
                else:
                    points = [points[0], end]
                if self.tool in {"pen", "highlight"} or (points[-1] - points[0]).manhattanLength() >= 1:
                    self.add_annotation(Annotation(self.tool, tuple(points), self.color, self.stroke_width))
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            point = self.image_point(event.position())
            if point is not None:
                index = hit_annotation(self.annotations, point,
                    6 * self.source.width() / max(1, self.image_rect().width()))
                if index is not None and self.annotations[index].kind == "text":
                    self._points = []
                    self._drag_origin = self._drag_before = self._drag_mark = None
                    self._drag_base = self._drag_pixels = self._drag_rect = None
                    self.select_annotation(index)
                    self.edit_selected_text()
                    event.accept()
                    return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        if self._text_point is not None:
            return super().keyPressEvent(event)
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
        elif not mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier) and key in TOOL_KEYS:
            self.set_tool(TOOL_KEYS[key])
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and self.selected_annotation is not None:
            step = 10 if mods & Qt.KeyboardModifier.ShiftModifier else 1
            dx = -step if key == Qt.Key.Key_Left else step if key == Qt.Key.Key_Right else 0
            dy = -step if key == Qt.Key.Key_Up else step if key == Qt.Key.Key_Down else 0
            self.document.replace_at(self.selected_index, translate_annotation(self.selected_annotation,
                QPointF(dx, dy), QRectF(self.source.rect())))
            self._refresh_pixels()
        elif mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_Z:
            self.redo() if mods & Qt.KeyboardModifier.ShiftModifier else self.undo()
        elif mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_Y:
            self.redo()
        else:
            return super().keyPressEvent(event)
        event.accept()


class AnnotationDialog(QDialog):
    """Editor requests clipboard output through its host-owned copy policy."""
    copyRequested = Signal(QImage)
    saveToLibraryRequested = Signal(QImage)
    preferencesChanged = Signal(dict)

    def __init__(self, image: QImage, parent=None, preferences=None):
        super().__init__(parent)
        configure_window_glass(self)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setObjectName("annotationDialog")
        self.setWindowTitle(tr('截图标注 · {app}', app=app_name()))
        self.resize(1080, 760)
        self.setMinimumSize(780, 500)
        self._loading_preferences = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.root = QWidget(self)
        self.root.setObjectName("annotationRoot")
        self.root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self.root)
        layout = QVBoxLayout(self.root)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        toolbar = QGridLayout()
        toolbar.setHorizontalSpacing(6)
        toolbar.setVerticalSpacing(6)
        self.tool_group = QButtonGroup(self)
        self.tool_buttons = {}
        tool_tips = {"number": tr('单击添加递增序号；撤销后会继续当前序号'),
                     "redact": tr('拖动实色覆盖区域；导出图片只包含覆盖后的像素'),
                     "highlight": tr('拖动荧光笔，保留底下文字可读'),
                     "text": tr('单击图片输入文字')}
        for index, (tool, label) in enumerate((("select", tr('选择')), *TOOLS)):
            label = tr(label)
            button = QPushButton(label)
            button.setCheckable(True)
            button.setMinimumHeight(34)
            button.setAutoDefault(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(label)
            key = next(chr(key.value) for key, name in TOOL_KEYS.items() if name == tool)
            button.setToolTip((tr('点选或拖动单个标注，Delete 删除，双击文字修改') if tool == "select" else
                               tool_tips.get(tool, tr('拖动添加') + label)) + " · " + key)
            button.clicked.connect(lambda _checked=False, name=tool: self.canvas.set_tool(name))
            self.tool_group.addButton(button)
            self.tool_buttons[tool] = button
            toolbar.addWidget(button, index // 6, index % 6)
        self.tool_buttons["rectangle"].setChecked(True)
        layout.addLayout(toolbar)
        options = QHBoxLayout()
        options.setSpacing(8)
        options.addWidget(QLabel(tr('颜色')))
        self.color_combo = QComboBox()
        self.color_combo.setAccessibleName(tr('标注颜色'))
        for label, value in ((tr('红色'), "#e81123"), (tr('黄色'), "#ffb900"), (tr('蓝色'), "#0078d4"),
                             (tr('绿色'), "#10893e"), (tr('白色'), "#ffffff"), (tr('黑色'), "#111111")):
            self.color_combo.addItem(label, value)
        self.color_combo.currentIndexChanged.connect(self._update_color)
        options.addWidget(self.color_combo)
        options.addWidget(QLabel(tr('粗细')))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 24)
        self.width_spin.setValue(4)
        self.width_spin.setSuffix(" px")
        self.width_spin.setAccessibleName(tr('标注线宽'))
        self.width_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.width_spin.setMaximumWidth(96)
        self.width_spin.valueChanged.connect(self._update_width)
        self.width_minus = self._button("−", lambda: self.width_spin.stepDown(), tr('减小线宽'))
        self.width_minus.setAccessibleName(tr('减小标注线宽'))
        options.addWidget(self.width_minus)
        options.addWidget(self.width_spin)
        self.width_plus = self._button("+", lambda: self.width_spin.stepUp(), tr('增大线宽'))
        self.width_plus.setAccessibleName(tr('增大标注线宽'))
        options.addWidget(self.width_plus)
        options.addStretch()
        self.undo_button = self._button(tr('撤销'), lambda: self.canvas.undo(), tr('撤销添加、编辑、移动或删除 · Ctrl+Z'))
        self.redo_button = self._button(tr('重做'), lambda: self.canvas.redo(), tr('重做上一步 · Ctrl+Y 或 Ctrl+Shift+Z'))
        self.reset_button = self._button(tr('重置'), lambda: self.canvas.reset(), tr('移除本次所有标注，恢复原图'))
        options.addWidget(self.undo_button)
        options.addWidget(self.redo_button)
        options.addWidget(self.reset_button)
        layout.addLayout(options)
        self.canvas = AnnotationCanvas(image)
        layout.addWidget(self.canvas, 1)
        self.status = QLabel(tr('{value0} × {value1} · Q 选择并移动标注，Delete 删除，双击文字修改。原图保持原样。', value0=image.width(), value1=image.height()))
        self.status.setWordWrap(True)
        self.status.setObjectName("annotationHint")
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.edit_text_button = self._button(tr('修改文字'), self.canvas.edit_selected_text, tr('选中文字后修改，也可以双击文字'))
        self.delete_button = self._button(tr('删除标注'), self.canvas.delete_selected, tr('删除当前选中的单个标注 · Delete'))
        footer.addWidget(self.edit_text_button)
        footer.addWidget(self.delete_button)
        footer.addStretch()
        footer.addWidget(self._button(tr('取消'), self.close))
        self.save_button = self._button(tr('另存为…'), self.save_as, tr('将标注结果另存为 PNG · Ctrl+S'))
        footer.addWidget(self.save_button)
        self.library_button = self._button(tr('存入内容库'), self.save_to_library, tr('将标注结果作为新图片存入内容库，原图保持原样'))
        footer.addWidget(self.library_button)
        self.copy_button = self._button(tr('仅复制'), self.copy_image, tr('仅复制标注后的图片，不存入内容库 · Ctrl+C'))
        self.copy_button.setObjectName("annotationPrimary")
        footer.addWidget(self.copy_button)
        layout.addLayout(footer)
        self.canvas.changed.connect(self._update_actions)
        self.canvas.selectionChanged.connect(self._selection_changed)
        self.canvas.toolChanged.connect(self._tool_changed)
        self.canvas.editingChanged.connect(self._editing_changed)
        self._shortcuts = [QShortcut(QKeySequence(chord), self, activated=slot) for chord, slot in
                           (("Ctrl+Z", self.canvas.undo), ("Ctrl+Y", self.canvas.redo),
                            ("Ctrl+Shift+Z", self.canvas.redo), ("Ctrl+C", self.copy_image),
                            ("Ctrl+S", self.save_as), ("Delete", self.canvas.delete_selected),
                            ("Backspace", self.canvas.delete_selected))]
        self._shortcuts.extend(QShortcut(QKeySequence(chr(key.value)), self,
            activated=lambda tool=tool: self.canvas.set_tool(tool)) for key, tool in TOOL_KEYS.items())
        self.apply_preferences(preferences or {})
        self._loading_preferences = False
        self._update_actions()
        self.refresh_theme()

    @staticmethod
    def _button(text, slot, tip=""):
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.setMinimumHeight(36)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    def _update_color(self):
        self.canvas.set_color(self.color_combo.currentData())
        self._emit_preferences()

    def _update_width(self, value):
        self.canvas.set_width(value)
        self.width_minus.setEnabled(value > self.width_spin.minimum())
        self.width_plus.setEnabled(value < self.width_spin.maximum())
        self._emit_preferences()

    def _emit_preferences(self):
        if not self._loading_preferences:
            self.preferencesChanged.emit(dict(tool=self.canvas.tool, color=self.canvas.color,
                                             width=self.canvas.stroke_width))

    def apply_preferences(self, preferences):
        tool = preferences.get("tool", "rectangle")
        if tool != "select" and tool not in dict(TOOLS):
            tool = "rectangle"
        color = preferences.get("color", "#e81123")
        if not isinstance(color, str) or not QColor(color).isValid():
            color = "#e81123"
        width = preferences.get("width", 4)
        if not isinstance(width, (int, float)) or isinstance(width, bool) or not isfinite(width):
            width = 4
        width = max(1, min(24, round(width)))
        index = self.color_combo.findData(color)
        if index < 0:
            self.color_combo.addItem(tr('自定颜色'), color)
            index = self.color_combo.count() - 1
        self.color_combo.setCurrentIndex(index)
        self.width_spin.setValue(width)
        self.canvas.color, self.canvas.stroke_width = color, width
        self.canvas.set_tool(tool)

    def _tool_changed(self, tool):
        self.tool_buttons[tool].setChecked(True)
        self._emit_preferences()

    def _selection_changed(self, mark):
        if mark is not None:
            color_block = QSignalBlocker(self.color_combo)
            width_block = QSignalBlocker(self.width_spin)
            index = self.color_combo.findData(mark.color)
            if index < 0:
                self.color_combo.addItem(tr('自定颜色'), mark.color)
                index = self.color_combo.count() - 1
            self.color_combo.setCurrentIndex(index)
            self.width_spin.setValue(round(mark.width))
            del color_block, width_block
            self.width_minus.setEnabled(self.width_spin.value() > self.width_spin.minimum())
            self.width_plus.setEnabled(self.width_spin.value() < self.width_spin.maximum())
        self._update_actions()

    def _editing_changed(self, editing):
        for shortcut in getattr(self, "_shortcuts", ()):
            shortcut.setEnabled(not editing)

    def _update_actions(self):
        self.undo_button.setEnabled(self.canvas.can_undo)
        self.redo_button.setEnabled(self.canvas.can_redo)
        self.reset_button.setEnabled(self.canvas.can_undo or self.canvas.can_redo)
        mark = self.canvas.selected_annotation
        self.delete_button.setEnabled(mark is not None)
        self.edit_text_button.setEnabled(mark is not None and mark.kind == "text")

    def rendered_image(self):
        return self.canvas.rendered_image()

    def copy_image(self):
        self.copyRequested.emit(self.rendered_image())
        self.status.setText(tr('已复制标注图，可粘贴到聊天或文档中。原图保持原样。'))

    def save_to_library(self):
        self.saveToLibraryRequested.emit(self.rendered_image())
        self.status.setText(tr('已请求将标注图存入内容库，原图保持原样。'))

    def save_as(self):
        filename, _filter = QFileDialog.getSaveFileName(self, tr('标注图另存为'), tr('标注图片.png'), tr('PNG 图片 (*.png)'))
        if not filename:
            return False
        path = Path(filename)
        if path.suffix.lower() != ".png":
            path = path.with_name(path.name + ".png")
        output = QSaveFile(str(path))
        if not output.open(QSaveFile.OpenModeFlag.WriteOnly):
            self.status.setText(tr('无法保存，请选择有写入权限的文件夹。'))
            return False
        writer = QImageWriter(output, b"png")
        if not writer.write(self.rendered_image()):
            output.cancelWriting()
            self.status.setText(tr('未能写入图片：') + writer.errorString())
            return False
        if not output.commit():
            self.status.setText(tr('未能完成保存，请检查磁盘空间和文件权限。'))
            return False
        self.status.setText(tr('已另存标注图：') + path.name)
        return True

    def refresh_theme(self):
        p = theme()
        self.setStyleSheet(f"""
QDialog#annotationDialog {{ background:transparent; color:{p['text']}; }}
QWidget#annotationRoot {{ background:{p['surface']}; }}
QWidget#annotationRoot[nativeGlass="true"] {{ background:{p['material_canvas']}; }}
QDialog#annotationDialog QLabel {{ background:transparent; color:{p['text']}; }}
QDialog#annotationDialog QLabel#annotationHint {{ color:{p['secondary']}; }}
QDialog#annotationDialog QPushButton, QDialog#annotationDialog QComboBox,
QDialog#annotationDialog QSpinBox, QDialog#annotationDialog QLineEdit#annotationTextEditor {{ background:{p['card']}; color:{p['text']};
 border:1px solid {p['border']}; border-radius:5px; padding:5px 10px; }}
QDialog#annotationDialog QPushButton:hover {{ background:{p['hover']}; border-color:{p['border_hover']}; }}
QDialog#annotationDialog QPushButton:checked {{ background:{p['accent_soft']}; color:{p['accent_text']};
 border-color:{p['accent_border']}; }}
QDialog#annotationDialog QPushButton:disabled {{ color:{p['disabled_fg']}; background:{p['disabled_bg']}; }}
QDialog#annotationDialog QPushButton#annotationPrimary {{ background:{p['accent']};
 color:{p['on_accent']}; border-color:{p['accent']}; }}
QDialog#annotationDialog QPushButton:focus, QDialog#annotationDialog QComboBox:focus,
QDialog#annotationDialog QSpinBox:focus {{ border:2px solid {p['accent']}; }}
""")
        native_titlebar(self)
        if self.isVisible():
            refresh_window_glass(self, p["dark"], root=self.root, enabled=p["glass"])
        self.canvas.update()

    def showEvent(self, event):
        super().showEvent(event)
        p = theme()
        native_titlebar(self)
        refresh_window_glass(self, p["dark"], root=self.root, enabled=p["glass"])
