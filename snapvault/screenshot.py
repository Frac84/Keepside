"""Screen capture: one-key full screen, or freeze every screen and drag a region.

The screens are grabbed once, before any overlay appears, so menus and
tooltips that were open at the moment of the shortcut are captured too.
"""
from __future__ import annotations

from .i18n import tr

from dataclasses import replace

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                              QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .annotation import Annotation, TOOLS, _draw_mark, render_annotations
from .annotation_document import (AnnotationDocument, annotation_bounds, hit_annotation,
                                  translate_annotation)
from .theme import screenshot_style, theme

MIN_DRAG = 4  # logical pixels; a shorter drag is a click that takes the whole screen


def grab_screens():
    """[(logical screen geometry, frozen pixmap)] for every connected screen."""
    shots = []
    for screen in QGuiApplication.screens():
        pixmap = screen.grabWindow(0)
        if not pixmap.isNull():
            shots.append((screen.geometry(), pixmap))
    return shots


def grab_screen_at_cursor():
    """The whole screen under the mouse, at its full resolution, or None."""
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    if screen is None:
        return None
    pixmap = screen.grabWindow(0)
    return None if pixmap.isNull() else pixmap


class Toast(QWidget):
    """A short confirmation in the screen corner that never takes focus or clicks."""

    def __init__(self, text, milliseconds=1600):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setObjectName("shotToast")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.text = text
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(13)
        self.setFont(font)
        self.resize(self.fontMetrics().horizontalAdvance(text) + 44, 40)
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 20, area.bottom() - self.height() - 20)
        QTimer.singleShot(milliseconds, self.close)

    def paintEvent(self, event):
        colors = theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(colors["border"]), 1))
        painter.setBrush(QColor(colors["card"]))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors["success"]))
        painter.drawEllipse(QRectF(16, self.height() / 2 - 4, 8, 8))
        painter.setPen(QColor(colors["text"]))
        painter.drawText(QRectF(32, 0, self.width() - 40, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text)


class ScreenshotOverlay(QWidget):
    """Frozen-screen editing surface; only the confirmed flattened pixels leave it.

    Selection geometry is logical, while all marks are stored in source pixels.
    Moving a selection carries its marks; resizing crops/extends the same pixels.
    Rendering is cached between completed marks, never repeated for each pointer move.
    """

    finished = Signal(object, str)
    cancelled = Signal()
    selecting = Signal(object)
    preferencesChanged = Signal(object)
    regionSelected = Signal(object)
    TOOL_KEYS = {Qt.Key.Key_Q: "select", Qt.Key.Key_R: "rectangle", Qt.Key.Key_E: "ellipse",
                 Qt.Key.Key_A: "arrow", Qt.Key.Key_L: "line", Qt.Key.Key_B: "pen",
                 Qt.Key.Key_H: "highlight", Qt.Key.Key_T: "text", Qt.Key.Key_N: "number",
                 Qt.Key.Key_M: "mosaic", Qt.Key.Key_X: "redact"}
    HANDLE_SIZE = 7

    def __init__(self, geometry, pixmap, *, initial_tool=None, preferences=None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("screenshotOverlay")
        self.setAccessibleName(tr('截图与标注'))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(geometry)
        self.pixmap = pixmap
        self.source = pixmap.toImage()
        self.source.setDevicePixelRatio(1.)
        self._rendered = self.source
        self.selection = QRect()
        self.document = AnnotationDocument()
        self.selected_index = None
        options = preferences if isinstance(preferences, dict) else {}
        remembered_tool = options.get("tool", "select")
        self.tool = initial_tool if initial_tool in dict(TOOLS) else remembered_tool if remembered_tool in {"select", *dict(TOOLS)} else "select"
        self.color = options.get("color", "#e81123")
        if not isinstance(self.color, str) or not QColor(self.color).isValid():
            self.color = "#e81123"
        width = options.get("width", 4)
        self.stroke_width = max(1, min(24, width)) if isinstance(width, int) and not isinstance(width, bool) else 4
        self.copy_save = options.get("copy_save", True) is not False
        self._origin = None
        self._mode = None
        self._points = []
        self._pointer = None
        self._text_point = None
        self._text_index = None
        self._move_preview = None
        self._object_preview = None
        self._object_preview_rect = None
        self._last_preferences = None
        self._build_toolbar()
        self.text_editor = QLineEdit(self)
        self.text_editor.setObjectName("screenshotTextEditor")
        self.text_editor.setPlaceholderText(tr('输入文字，Enter 确认'))
        self.text_editor.setMaxLength(2000)
        self.text_editor.installEventFilter(self)
        self.text_editor.returnPressed.connect(self.commit_text)
        self.text_editor.editingFinished.connect(self.commit_text)
        self.text_editor.hide()
        self.refresh_theme()
        self.set_tool(self.tool)

    @property
    def annotations(self):
        return self.document.annotations

    @annotations.setter
    def annotations(self, values):
        self.document.annotations = list(values)

    @property
    def _redo(self):
        # Compatibility for callers that only inspect whether redo is available.
        return self.document.can_redo and [True] or []

    def preferences(self):
        return dict(tool=self.tool, color=self.color, width=self.stroke_width,
                    copy_save=self.copy_save)

    def _preferences_changed(self):
        values = self.preferences()
        if values != self._last_preferences:
            self._last_preferences = dict(values)
            self.preferencesChanged.emit(values)

    @property
    def scale_x(self):
        return self.source.width() / max(1, self.width())

    @property
    def scale_y(self):
        return self.source.height() / max(1, self.height())

    def source_point(self, point):
        return QPointF(max(0., min(self.source.width()-1., point.x()*self.scale_x)),
                       max(0., min(self.source.height()-1., point.y()*self.scale_y)))

    def source_selection(self):
        # Round both edges, rather than origin and width independently, so the
        # preview, dimension readout and exported pixels share the same bounds.
        r = self.selection
        left, top = round(r.x()*self.scale_x), round(r.y()*self.scale_y)
        right = round((r.x()+r.width())*self.scale_x)
        bottom = round((r.y()+r.height())*self.scale_y)
        return QRect(left, top, max(1, right-left), max(1, bottom-top)).intersected(self.source.rect())

    def _button(self, text, callback, name="", tip=""):
        button = QPushButton(text, self.toolbar)
        button.setObjectName(name)
        button.setAutoDefault(False)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setMinimumHeight(30)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tip)
        button.setAccessibleName(text)
        button.clicked.connect(callback)
        return button

    def _build_toolbar(self):
        self.toolbar = QWidget(self)
        self.toolbar.setObjectName("screenshotTools")
        self.toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.toolbar.setCursor(Qt.CursorShape.ArrowCursor)
        layout = QVBoxLayout(self.toolbar)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(5)
        self.tool_grid = QGridLayout()
        self.tool_grid.setSpacing(3)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_buttons = {}
        reverse_keys = {value: chr(key.value) for key, value in self.TOOL_KEYS.items()}
        for tool, title in (("select", tr('选择')), *TOOLS):
            title = tr(title)
            if tool == "redact":
                title = tr('遮挡')
            button = self._button(title, lambda _checked=False, tool=tool: self.set_tool(tool),
                                  "shotTool_" + tool,
                                  (tr('选择标注后可拖动、改色或删除；空白处拖动选区，Alt+拖动始终移动选区 · Q') if tool == "select"
                                   else title + " · " + reverse_keys[tool]))
            button.setCheckable(True)
            self.tool_group.addButton(button)
            self.tool_buttons[tool] = button
        layout.addLayout(self.tool_grid)
        options = QHBoxLayout()
        options.setSpacing(4)
        self.color_combo = QComboBox()
        self.color_combo.setAccessibleName(tr('标注颜色'))
        self.color_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for title, color in ((tr('红色'), "#e81123"), (tr('黄色'), "#ffb900"), (tr('蓝色'), "#0078d4"),
                             (tr('绿色'), "#10893e"), (tr('白色'), "#ffffff"), (tr('黑色'), "#111111")):
            self.color_combo.addItem(title, color)
        self.color_combo.setMaximumWidth(90)
        color_index = self.color_combo.findData(self.color)
        if color_index < 0:
            self.color_combo.addItem(tr('自定义'), self.color)
            color_index = self.color_combo.count() - 1
        self.color_combo.setCurrentIndex(color_index)
        self.color_combo.currentIndexChanged.connect(self._change_color)
        options.addWidget(self.color_combo)
        width_label = QLabel(tr('粗细'))
        options.addWidget(width_label)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 24)
        self.width_spin.setValue(self.stroke_width)
        self.width_spin.setAccessibleName(tr('笔画粗细（原图像素）'))
        self.width_spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.width_spin.setMaximumWidth(58)
        self.width_spin.valueChanged.connect(self._change_width)
        options.addWidget(self.width_spin)
        options.addStretch(1)
        self.undo_button = self._button(tr('撤销'), self.undo, "shotUndo", tr('撤销上一步 · Ctrl+Z'))
        self.redo_button = self._button(tr('重做'), self.redo, "shotRedo", tr('重做 · Ctrl+Y / Ctrl+Shift+Z'))
        options.addWidget(self.undo_button)
        options.addWidget(self.redo_button)
        self.delete_button = self._button(tr('删除'), self.delete_selected, "shotDelete", tr('删除选中标注 · Delete'))
        options.addWidget(self.delete_button)
        layout.addLayout(options)
        actions = self.action_grid = QGridLayout()
        actions.setSpacing(3)
        self.action_buttons = {}
        for text, action, tip in ((tr('取消'), "cancel", tr('取消截图 · Esc')),
                                  (tr('另存为'), "save", tr('保存当前结果 · Ctrl+S')),
                                  (tr('识字'), "ocr", tr('本地识别选区文字 · O')),
                                  (tr('贴图'), "pin", tr('贴到屏幕 · P')),
                                  (tr('仅复制'), "copy_only", tr('仅复制，不存入内容库；记住此选择')),
                                  (tr('复制并入库'), "copy", tr('复制并存入内容库；记住此选择'))):
            callback = (self.cancel if action == "cancel" else self.save_as if action == "save" else
                        lambda _checked=False, action=action: self.finish(action))
            button = self._button(text, callback, action + "Shot", tip)
            self.action_buttons[action] = button
        layout.addLayout(actions)
        self._layout_tools()
        self.toolbar.hide()
        self._update_actions()

    def _layout_tools(self):
        available = max(240, min(720, self.width()-8))
        self.toolbar.setFixedWidth(available)
        # Leave room around the selection on short displays without making
        # the controls or their labels smaller.
        compact = self.height() < 500
        self.toolbar.layout().setContentsMargins(8, 4 if compact else 7, 8, 4 if compact else 7)
        self.toolbar.layout().setSpacing(3 if compact else 5)
        tool_width = max(58, max(button.sizeHint().width() for button in self.tool_buttons.values()))
        columns = max(3, min(11, (available-16)//tool_width))
        while self.tool_grid.count():
            self.tool_grid.takeAt(0)
        for index, button in enumerate(self.tool_buttons.values()):
            self.tool_grid.addWidget(button, index//columns, index%columns)
        while self.action_grid.count():
            self.action_grid.takeAt(0)
        action_width = max(button.sizeHint().width() for button in self.action_buttons.values())
        action_columns = max(2, min(6, (available-16)//max(60, action_width)))
        for index, button in enumerate(self.action_buttons.values()):
            self.action_grid.addWidget(button, index//action_columns, index%action_columns)
        self.toolbar.adjustSize()

    def refresh_theme(self):
        p = theme()
        self.toolbar.setStyleSheet(screenshot_style() + f"""
QWidget#screenshotTools QPushButton {{ padding:4px 5px; font-size:12px; }}
QWidget#screenshotTools QPushButton:checked {{ color:{p['accent_text']}; background:{p['accent_soft']}; border-color:{p['accent_border']}; }}
QWidget#screenshotTools QPushButton:disabled {{ color:{p['disabled_fg']}; }}
QWidget#screenshotTools QComboBox, QWidget#screenshotTools QSpinBox {{ background:{p['card']}; color:{p['text']}; border:1px solid {p['border']}; border-radius:4px; padding:3px; }}
QWidget#screenshotTools QSpinBox QLineEdit {{ color:{p['text']}; background:transparent; border:none; padding:0; }}
QWidget#screenshotTools QComboBox QAbstractItemView {{ color:{p['text']}; background:{p['card']}; selection-color:{p['accent_text']}; selection-background-color:{p['accent_soft']}; }}
QWidget#screenshotTools QLabel {{ color:{p['secondary']}; background:transparent; font-size:12px; }}
""")
        if hasattr(self, "text_editor"):
            self.text_editor.setStyleSheet(f"background:{p['card']}; color:{p['text']}; border:2px solid {p['accent']}; padding:3px;")
        self.update()

    def _update_actions(self):
        self.undo_button.setEnabled(self.document.can_undo)
        self.redo_button.setEnabled(self.document.can_redo)
        self.delete_button.setEnabled(self.selected_index is not None)
        for action in ("copy", "copy_only"):
            button = self.action_buttons[action]
            button.setObjectName("primary" if (action == "copy") == self.copy_save else action + "Shot")
            button.setToolTip((tr('默认完成操作 · Enter / Ctrl+C；') if (action == "copy") == self.copy_save else "") +
                              (tr('复制并存入内容库') if action == "copy" else tr('仅复制，不存入内容库')))
            button.style().unpolish(button)
            button.style().polish(button)

    def _change_color(self):
        self._cancel_gesture()
        self.color = self.color_combo.currentData()
        if self.selected_index is not None:
            mark = self.annotations[self.selected_index]
            if mark.color != self.color:
                self.document.replace_at(self.selected_index, replace(mark, color=self.color))
                self._rendered = render_annotations(self.source, self.annotations)
                self._update_actions()
                self.update()
        self._preferences_changed()

    def _change_width(self, value):
        self._cancel_gesture()
        self.stroke_width = value
        if self.selected_index is not None:
            mark = self.annotations[self.selected_index]
            if mark.width != value:
                self.document.replace_at(self.selected_index, replace(mark, width=value))
                self._rendered = render_annotations(self.source, self.annotations)
                self._update_actions()
                self.update()
        self._preferences_changed()

    def select_annotation(self, index):
        self.selected_index = index if index is not None and 0 <= index < len(self.annotations) else None
        if self.selected_index is not None:
            mark = self.annotations[self.selected_index]
            self.color, self.stroke_width = mark.color, max(1, min(24, round(mark.width)))
            blocked = self.color_combo.blockSignals(True)
            color_index = self.color_combo.findData(self.color)
            if color_index < 0:
                self.color_combo.addItem(tr('自定义'), self.color)
                color_index = self.color_combo.count()-1
            self.color_combo.setCurrentIndex(color_index)
            self.color_combo.blockSignals(blocked)
            blocked = self.width_spin.blockSignals(True)
            self.width_spin.setValue(self.stroke_width)
            self.width_spin.blockSignals(blocked)
        self._update_actions()
        self.update()

    def delete_selected(self):
        self._cancel_gesture()
        if self.selected_index is not None:
            self.document.remove_at(self.selected_index)
            self.selected_index = None
            self._rendered = render_annotations(self.source, self.annotations)
            self._update_actions()
            self.update()

    def set_tool(self, tool):
        if tool != "select" and tool not in dict(TOOLS):
            raise ValueError(tr('不支持的截图工具'))
        self._cancel_gesture()
        if hasattr(self, "text_editor"):
            self.commit_text()
        self.tool = tool
        self.selected_index = None
        self._points = []
        self.tool_buttons[tool].setChecked(True)
        self.setCursor(Qt.CursorShape.IBeamCursor if tool == "text" else Qt.CursorShape.CrossCursor)
        self.update()
        self._update_actions()
        self._preferences_changed()

    def add_annotation(self, mark):
        if mark.kind not in dict(TOOLS) or not mark.points:
            raise ValueError(tr('不支持的标注'))
        self.document.add(mark)
        self.selected_index = None
        self._rendered = render_annotations(self._rendered, (mark,))
        self._update_actions()
        self.update()

    def undo(self):
        self._cancel_gesture()
        self.commit_text()
        self._points = []
        if self.document.undo():
            self._rendered = render_annotations(self.source, self.annotations)
        self.selected_index = None
        self._update_actions()
        self.update()

    def redo(self):
        self._cancel_gesture()
        self.commit_text()
        self._points = []
        if self.document.redo():
            self._rendered = render_annotations(self.source, self.annotations)
        self.selected_index = None
        self._update_actions()
        self.update()

    def next_number(self):
        return max((int(mark.text) for mark in self.annotations
                    if mark.kind == "number" and mark.text.isdecimal()), default=0) + 1

    def _cancel_gesture(self):
        """Rollback an uncommitted pointer preview before another edit acts.

        A later mouse release must never commit the old press snapshot after
        undo, delete or a tool change has already changed the document.
        """
        mode = self._mode
        if mode is None and self._origin is None:
            return False
        if mode in {"move", "object_move"}:
            self.document.restore(self._before_marks)
        if mode == "move" or (mode or "").startswith("resize:"):
            self.selection = QRect(self._before_selection)
        self._origin = self._mode = None
        self._points = []
        self._move_preview = None
        self._object_preview = None
        self._object_preview_rect = None
        self._before_marks = None
        self._before_selection = None
        if self.selected_index is not None and self.selected_index >= len(self.annotations):
            self.selected_index = None
        self._rendered = render_annotations(self.source, self.annotations)
        self._place_toolbar()
        self.update()
        return True

    def clear_selection(self):
        self._discard_text()
        self.selection = QRect()
        self._origin = self._mode = None
        self._move_preview = None
        self._points = []
        self.document.reset()
        self.selected_index = None
        self._rendered = self.source
        self.toolbar.hide()
        self._update_actions()
        self.update()

    def set_selection(self, rect):
        self.selection = QRect(rect).normalized().intersected(self.rect())
        self._place_toolbar()
        self.update()

    def select_whole_screen(self):
        self.set_selection(self.rect())

    def _place_toolbar(self):
        if self.selection.isEmpty():
            self.toolbar.hide()
            return
        self.toolbar.adjustSize()
        width, height = self.toolbar.width(), self.toolbar.height()
        x = max(4, min(self.selection.right()-width+1, self.width()-width-4))
        y = self.selection.bottom()+9
        if y+height > self.height()-4:
            y = self.selection.top()-height-9
        if y < 4:
            y = max(4, self.height()-height-4)
        self.toolbar.move(x, y)
        self.toolbar.show()
        self.toolbar.raise_()
        if self.text_editor.isVisible():
            self.text_editor.raise_()

    def crop(self):
        """Confirmed edited pixels, excluding selection handles and controls."""
        if self.selection.isEmpty():
            return None
        image = self._rendered.copy(self.source_selection())
        image.setDevicePixelRatio(1.)
        return image

    def finish(self, action="copy"):
        self._cancel_gesture()
        self.commit_text()
        image = self.crop()
        if image is not None and not image.isNull():
            if action in {"copy", "copy_only"}:
                changed = self.copy_save != (action == "copy")
                self.copy_save = action == "copy"
                self._update_actions()
                if changed:
                    self._preferences_changed()
            self.regionSelected.emit({"geometry": [self.geometry().x(), self.geometry().y(), self.width(), self.height()],
                                      "selection": [self.selection.x(), self.selection.y(), self.selection.width(), self.selection.height()]})
            self.finished.emit(image, action)

    def save_as(self):
        self._cancel_gesture()
        self.commit_text()
        image = self.crop()
        if image is None or image.isNull():
            return False
        from .screenshot_output import write_screenshot_png
        filename, _ = QFileDialog.getSaveFileName(self, tr('截图另存为'), tr('截图.png'), tr('PNG 图片 (*.png)'))
        self.setFocus()
        if not filename:
            return False
        try:
            write_screenshot_png(image, filename)
        except (OSError, ValueError) as exc:
            self.save_error = str(exc)
            self.action_buttons["save"].setToolTip(tr('保存失败：') + str(exc))
            self._draw_status = tr('保存失败，请选择其他位置重试。')
            self.update()
            return False
        self.finish("save_done")
        return True

    def cancel(self):
        self._discard_text()
        self.cancelled.emit()

    def _handles(self):
        r = self.selection
        if r.isEmpty():
            return {}
        return {"nw": r.topLeft(), "n": QPoint(r.center().x(), r.top()),
                "ne": r.topRight(), "e": QPoint(r.right(), r.center().y()),
                "se": r.bottomRight(), "s": QPoint(r.center().x(), r.bottom()),
                "sw": r.bottomLeft(), "w": QPoint(r.left(), r.center().y())}

    def _handle_at(self, point):
        half = self.HANDLE_SIZE
        for name, center in self._handles().items():
            if QRect(center-QPoint(half, half), center+QPoint(half, half)).contains(point):
                return name
        return None

    def _clamp(self, point):
        return QPoint(max(0, min(self.width()-1, point.x())), max(0, min(self.height()-1, point.y())))

    def _begin_text(self, point, index=None):
        self.commit_text()
        self._text_index = index
        existing = self.annotations[index] if index is not None else None
        self._text_point = existing.points[0] if existing else self.source_point(point)
        self._text_color, self._text_width = (existing.color, existing.width) if existing else (self.color, self.stroke_width)
        scale = (self.scale_x+self.scale_y)/2
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(max(12, round(max(16, self.stroke_width*7)/scale)))
        self.text_editor.setFont(font)
        self.text_editor.setGeometry(max(2, min(point.x(), self.width()-242)),
                                     max(2, min(point.y(), self.height()-40)), 240, max(36, font.pixelSize()+14))
        self.text_editor.setText(existing.text if existing else "")
        self.text_editor.show()
        self.text_editor.raise_()
        self.text_editor.setFocus()
        if existing:
            self.text_editor.selectAll()

    def _discard_text(self):
        self._text_point = None
        self._text_index = None
        if hasattr(self, "text_editor"):
            self.text_editor.hide()
            self.text_editor.clear()

    def commit_text(self):
        if self._text_point is None:
            return
        point, text, index = self._text_point, self.text_editor.text(), self._text_index
        self._text_point = None
        self._text_index = None
        self.text_editor.hide()
        self.text_editor.clear()
        if text.strip():
            mark = Annotation("text", (point,), self._text_color, self._text_width, text)
            if index is None:
                self.add_annotation(mark)
            else:
                self.document.replace_at(index, mark)
                self._rendered = render_annotations(self.source, self.annotations)
                self.select_annotation(index)
        elif index is not None:
            self.document.remove_at(index)
            self.selected_index = None
            self._rendered = render_annotations(self.source, self.annotations)
        self._update_actions()
        self.update()
        self.setFocus()

    def eventFilter(self, watched, event):
        if watched is self.text_editor and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._discard_text()
                self.setFocus()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # QLineEdit may propagate Return after emitting returnPressed.
                # Consume it here, so confirming text never confirms the capture.
                self.commit_text()
                return True
            # All letters and edit shortcuts belong to the input, never to tools.
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        point = self._clamp(event.position().toPoint())
        self._pointer = point
        if event.button() == Qt.MouseButton.RightButton:
            if self._text_point is not None:
                self._discard_text()
            elif self.selection.isEmpty():
                self.cancel()
            else:
                self.clear_selection()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self._cancel_gesture()
        self.commit_text()
        self._origin = point
        self._before_selection = QRect(self.selection)
        self._before_marks = self.document.snapshot()
        handle = self._handle_at(point)
        if handle is not None:
            self._mode = "resize:"+handle
        elif self.selection.contains(point):
            if self.tool == "select":
                hit = None if event.modifiers() & Qt.KeyboardModifier.AltModifier else hit_annotation(
                    self.annotations, self.source_point(point), tolerance=6*max(self.scale_x, self.scale_y))
                self.select_annotation(hit)
                if hit is not None:
                    self._mode = "object_move"
                    self._object_original = self.annotations[hit]
                    self._object_preview_rect = annotation_bounds(self._object_original).toAlignedRect().intersected(self.source.rect())
                    self._object_preview = self._rendered.copy(self._object_preview_rect)
                    self._object_preview.setDevicePixelRatio(1.)
                else:
                    self._mode = "move"
                    # Keep already-redacted pixels covered for the whole gesture.
                    self._move_preview = self._rendered.copy(self.source_selection())
                    self._move_preview.setDevicePixelRatio(1.)
            elif self.tool == "text":
                self._origin = None
                self._begin_text(point)
            elif self.tool == "number":
                self._origin = None
                self.add_annotation(Annotation("number", (self.source_point(point),), self.color,
                                                self.stroke_width, str(self.next_number())))
            else:
                self._mode = "draw"
                self._points = [self.source_point(point)]
        else:
            self.clear_selection()
            self._origin = point
            self._mode = "select"
            self.selecting.emit(self)
        if self._mode in {"move", "object_move", "select"} or (self._mode or "").startswith("resize:"):
            self.toolbar.hide()
        self.update()
        event.accept()

    def _moved_marks(self, marks, delta):
        offset = QPointF(delta.x()*self.scale_x, delta.y()*self.scale_y)
        return [Annotation(m.kind, tuple(p+offset for p in m.points), m.color, m.width, m.text) for m in marks]

    def _update_drag(self, point):
        if self._origin is None:
            return
        if self._mode == "select":
            self.selection = QRect(self._origin, point).normalized().intersected(self.rect())
        elif self._mode == "move":
            delta = point-self._origin
            rect = self._before_selection
            delta.setX(max(-rect.x(), min(self.width()-rect.x()-rect.width(), delta.x())))
            delta.setY(max(-rect.y(), min(self.height()-rect.y()-rect.height(), delta.y())))
            self.selection = rect.translated(delta)
            self.annotations = self._moved_marks(self._before_marks, delta)
        elif self._mode == "object_move" and self.selected_index is not None:
            delta = QPointF((point.x()-self._origin.x())*self.scale_x,
                            (point.y()-self._origin.y())*self.scale_y)
            updated = list(self._before_marks)
            updated[self.selected_index] = translate_annotation(self._object_original, delta,
                                                               QRectF(self.source_selection()))
            self.annotations = updated
        elif (self._mode or "").startswith("resize:"):
            handle = self._mode.partition(":")[2]
            rect = QRect(self._before_selection)
            if "w" in handle:
                rect.setLeft(min(point.x(), rect.right()-1))
            if "e" in handle:
                rect.setRight(max(point.x(), rect.left()+1))
            if "n" in handle:
                rect.setTop(min(point.y(), rect.bottom()-1))
            if "s" in handle:
                rect.setBottom(max(point.y(), rect.top()+1))
            self.selection = rect.intersected(self.rect())
        elif self._mode == "draw":
            # The marks stay inside the selection even if the pointer leaves it.
            point = QPoint(max(self.selection.left(), min(self.selection.right(), point.x())),
                           max(self.selection.top(), min(self.selection.bottom(), point.y())))
            source = self.source_point(point)
            if self.tool in {"pen", "highlight"}:
                if not self._points or (source-self._points[-1]).manhattanLength() >= .5:
                    self._points.append(source)
            else:
                self._points = [self._points[0], source]

    def mouseMoveEvent(self, event):
        point = self._clamp(event.position().toPoint())
        self._pointer = point
        if self._origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._update_drag(point)
        else:
            handle = self._handle_at(point)
            cursor = {"nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
                      "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor,
                      "n": Qt.CursorShape.SizeVerCursor, "s": Qt.CursorShape.SizeVerCursor,
                      "e": Qt.CursorShape.SizeHorCursor, "w": Qt.CursorShape.SizeHorCursor}.get(handle)
            if cursor is None:
                cursor = (Qt.CursorShape.SizeAllCursor if self.tool == "select" and self.selection.contains(point)
                          else Qt.CursorShape.IBeamCursor if self.tool == "text" and self.selection.contains(point)
                          else Qt.CursorShape.CrossCursor)
            self.setCursor(cursor)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            point = self._clamp(event.position().toPoint())
            self._update_drag(point)
            mode, origin = self._mode, self._origin
            self._origin = self._mode = None
            if mode == "select":
                if abs(point.x()-origin.x()) < MIN_DRAG and abs(point.y()-origin.y()) < MIN_DRAG:
                    self.select_whole_screen()
            elif mode == "draw" and self._points:
                points = tuple(self._points)
                self._points = []
                if self.tool in {"pen", "highlight"} or (len(points)>1 and points[0] != points[-1]):
                    self.add_annotation(Annotation(self.tool, points, self.color, self.stroke_width))
            elif mode == "move":
                offset = self.selection.topLeft()-self._before_selection.topLeft()
                self.document.restore(self._before_marks)
                self.document.transform(lambda mark: self._moved_marks((mark,), offset)[0])
                self._rendered = render_annotations(self.source, self.annotations)
                self._move_preview = None
            elif mode == "object_move":
                self.document.commit(self.annotations, before=self._before_marks)
                self._rendered = render_annotations(self.source, self.annotations)
                self._object_preview = None
                self._object_preview_rect = None
                self._update_actions()
            self._place_toolbar()
            self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        point = event.position().toPoint()
        hit = hit_annotation(self.annotations, self.source_point(point),
                             tolerance=6*max(self.scale_x, self.scale_y)) if self.selection.contains(point) else None
        if event.button() == Qt.MouseButton.LeftButton and hit is not None:
            self._origin = self._mode = None
            self._object_preview = None
            self.select_annotation(hit)
            if self.annotations[hit].kind == "text":
                self._begin_text(point, hit)
        elif (event.button() == Qt.MouseButton.LeftButton and self.tool == "select" and
                self.selection.contains(point)):
            self._origin = self._mode = None
            self.finish("copy" if self.copy_save else "copy_only")
        event.accept()

    def _nudge(self, dx, dy):
        self._cancel_gesture()
        if self.selected_index is not None:
            mark = self.annotations[self.selected_index]
            moved = translate_annotation(mark, QPointF(dx*self.scale_x, dy*self.scale_y),
                                          QRectF(self.source_selection()))
            self.document.replace_at(self.selected_index, moved)
            self._rendered = render_annotations(self.source, self.annotations)
            self._update_actions()
            self.update()
            return
        before = QRect(self.selection)
        after = before.translated(dx, dy)
        after.moveLeft(max(0, min(self.width()-after.width(), after.left())))
        after.moveTop(max(0, min(self.height()-after.height(), after.top())))
        offset = after.topLeft()-before.topLeft()
        self.document.transform(lambda mark: self._moved_marks((mark,), offset)[0])
        self._rendered = render_annotations(self.source, self.annotations)
        self.set_selection(after)

    def keyPressEvent(self, event):
        if self._text_point is not None:
            return super().keyPressEvent(event)
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Escape:
            self.cancel()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
        elif ctrl and key == Qt.Key.Key_Z:
            self.redo() if shift else self.undo()
        elif ctrl and key == Qt.Key.Key_Y:
            self.redo()
        elif ctrl and key in {Qt.Key.Key_C, Qt.Key.Key_S}:
            if not self.selection.isEmpty():
                self.finish("copy" if self.copy_save else "copy_only") if key == Qt.Key.Key_C else self.save_as()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.selection.isEmpty():
                self.select_whole_screen()
            self.finish("copy" if self.copy_save else "copy_only")
        elif not ctrl and key in {Qt.Key.Key_P, Qt.Key.Key_O} and not self.selection.isEmpty():
            self.finish("pin" if key == Qt.Key.Key_P else "ocr")
        elif not ctrl and key in self.TOOL_KEYS:
            self.set_tool(self.TOOL_KEYS[key])
        elif key in {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down} and not self.selection.isEmpty():
            step = 10 if shift else 1
            dx = -step if key == Qt.Key.Key_Left else step if key == Qt.Key.Key_Right else 0
            dy = -step if key == Qt.Key.Key_Up else step if key == Qt.Key.Key_Down else 0
            self._nudge(dx, dy)
        else:
            return super().keyPressEvent(event)
        event.accept()

    def paintEvent(self, event):
        colors = theme()
        painter = QPainter(self)
        moving = self._mode == "move"
        painter.drawImage(QRectF(self.rect()), self.source if moving else self._rendered)
        selection = self.selection
        if not selection.isEmpty():
            if moving and self._move_preview is not None:
                painter.drawImage(QRectF(selection), self._move_preview)
            if self._mode == "object_move" and self._object_preview is not None and self.selected_index is not None:
                mark = self.annotations[self.selected_index]
                delta = mark.points[0]-self._object_original.points[0]
                target = QRectF(self._object_preview_rect).translated(delta)
                target = QRectF(target.x()/self.scale_x, target.y()/self.scale_y,
                                target.width()/self.scale_x, target.height()/self.scale_y)
                painter.save()
                painter.setClipRect(QRectF(selection))
                painter.drawImage(target, self._object_preview)
                painter.restore()
            painter.save()
            painter.setClipRect(QRectF(selection))
            painter.scale(1/self.scale_x, 1/self.scale_y)
            if self._points:
                _draw_mark(painter, Annotation(self.tool, tuple(self._points), self.color, self.stroke_width), preview=True)
            painter.restore()
            if self.selected_index is not None:
                selected = annotation_bounds(self.annotations[self.selected_index])
                selected = QRectF(selected.x()/self.scale_x, selected.y()/self.scale_y,
                                  selected.width()/self.scale_x, selected.height()/self.scale_y)
                painter.save()
                painter.setClipRect(QRectF(selection))
                painter.setPen(QPen(QColor(colors["accent"]), 1, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(selected.adjusted(-3, -3, 3, 3))
                painter.restore()
        shade = QColor(0, 0, 0, 120)
        if selection.isEmpty():
            painter.fillRect(self.rect(), shade)
            self._draw_hint(painter, tr('拖动框选 · 单击选择全屏 · Esc 取消'))
            self._draw_magnifier(painter)
            return
        for area in (QRect(0, 0, self.width(), selection.top()),
                     QRect(0, selection.bottom()+1, self.width(), self.height()-selection.bottom()-1),
                     QRect(0, selection.top(), selection.left(), selection.height()),
                     QRect(selection.right()+1, selection.top(), self.width()-selection.right()-1, selection.height())):
            if not area.isEmpty():
                painter.fillRect(area, shade)
        painter.setPen(QPen(QColor(colors["accent"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(selection).adjusted(-.5, -.5, .5, .5))
        painter.setBrush(QColor(colors["card"]))
        for point in self._handles().values():
            painter.drawRect(QRectF(point.x()-3, point.y()-3, 6, 6))
        area = self.source_selection()
        size = f"{area.width()} × {area.height()} px"
        if self.tool == "select":
            size += tr(' · 拖动移动 / 边缘缩放')
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(12)
        painter.setFont(font)
        box = QRectF(max(0, min(selection.left(), self.width()-painter.fontMetrics().horizontalAdvance(size)-18)),
                     max(0, selection.top()-26), painter.fontMetrics().horizontalAdvance(size)+16, 22)
        painter.fillRect(box, QColor(20, 22, 28, 235))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, size)
        if getattr(self, "_draw_status", ""):
            self._draw_hint(painter, self._draw_status)
        if self._mode == "select" or (self._mode or "").startswith("resize:"):
            self._draw_magnifier(painter)

    def _draw_magnifier(self, painter):
        if self._pointer is None:
            return
        point = self.source_point(self._pointer)
        x, y = int(point.x()), int(point.y())
        color = self.source.pixelColor(x, y)
        width, height = 142, 128
        left = self._pointer.x()+24
        top = self._pointer.y()+24
        if left+width > self.width()-4:
            left = self._pointer.x()-width-24
        if top+height > self.height()-4:
            top = self._pointer.y()-height-24
        box = QRect(max(4, left), max(4, top), width, height)
        painter.fillRect(box, QColor(26, 26, 26, 245))
        target = QRect(box.left()+5, box.top()+5, width-10, 82)
        sample = QRect(x-7, y-4, 15, 9).intersected(self.source.rect())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        # Full image source rect avoids any QPixmap DPR implicit scaling.
        painter.drawImage(QRectF(target), self.source, QRectF(sample))
        cross_x = target.left()+(x-sample.left()+.5)*target.width()/sample.width()
        cross_y = target.top()+(y-sample.top()+.5)*target.height()/sample.height()
        painter.setPen(QPen(QColor("#0078d4"), 1))
        painter.drawLine(QPointF(cross_x, target.top()), QPointF(cross_x, target.bottom()))
        painter.drawLine(QPointF(target.left(), cross_y), QPointF(target.right(), cross_y))
        font = QFont("Segoe UI")
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(QRect(box.left()+6, box.top()+88, width-12, 16), Qt.AlignmentFlag.AlignLeft,
                         f"{x}, {y}   {color.name().upper()}")
        painter.drawText(QRect(box.left()+6, box.top()+105, width-12, 16), Qt.AlignmentFlag.AlignLeft,
                         f"RGB {color.red()}, {color.green()}, {color.blue()}")

    def _draw_hint(self, painter, text):
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(14)
        painter.setFont(font)
        width = min(self.width()-16, painter.fontMetrics().horizontalAdvance(text)+36)
        box = QRectF((self.width()-width)/2, 28, width, 36)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 28, 235))
        painter.drawRoundedRect(box, 10, 10)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)


class ScreenshotSession(QObject):
    """One capture across all screens; emits the result once, then closes."""

    captured = Signal(object, str)
    ended = Signal()
    preferencesChanged = Signal(object)
    regionSelected = Signal(object)

    def __init__(self, shots, *, initial_tool=None, preferences=None, last_region=None):
        super().__init__()
        self.overlays = []
        self._done = False
        for geometry, pixmap in shots:
            overlay = ScreenshotOverlay(geometry, pixmap, initial_tool=initial_tool, preferences=preferences)
            overlay.finished.connect(self._finish)
            overlay.cancelled.connect(self.close)
            overlay.selecting.connect(self._only)
            overlay.preferencesChanged.connect(self._preferences_changed)
            overlay.regionSelected.connect(self.regionSelected.emit)
            self.overlays.append(overlay)
        if isinstance(last_region, dict):
            screen = last_region.get("geometry")
            area = last_region.get("selection")
            if (isinstance(screen, (list, tuple)) and len(screen) == 4 and
                    isinstance(area, (list, tuple)) and len(area) == 4 and
                    all(isinstance(value, int) and not isinstance(value, bool) for value in (*screen, *area))):
                for overlay in self.overlays:
                    if overlay.geometry() == QRect(*screen):
                        region = QRect(*area)
                        if region.width() >= 2 and region.height() >= 2 and overlay.rect().contains(region):
                            overlay.set_selection(region)
                        break

    def _preferences_changed(self, values):
        # Keep screen-local toolbars consistent without altering selected marks
        # on another screen or producing recursive setting notifications.
        self.preferencesChanged.emit(values)

    def start(self):
        for overlay in self.overlays:
            overlay.show()
        # Give keyboard focus to the screen under the mouse.
        cursor = QCursor.pos()
        target = next((overlay for overlay in self.overlays if overlay.geometry().contains(cursor)),
                      self.overlays[0] if self.overlays else None)
        if target is not None:
            target.raise_()
            target.activateWindow()
            target.setFocus()
        return bool(self.overlays)

    def _only(self, active):
        for overlay in self.overlays:
            if overlay is not active:
                overlay.clear_selection()

    def _finish(self, image, action):
        if self._done:
            return
        self.close()
        self.captured.emit(image, action)

    def close(self):
        if self._done:
            return
        self._done = True
        for overlay in self.overlays:
            overlay.close()
        self.overlays = []
        self.ended.emit()
