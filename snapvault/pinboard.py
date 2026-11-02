"""Independent, always-on-top image references. No clipboard reads or input hooks."""
from __future__ import annotations

from .i18n import tr

import math
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSlider, QWidget,
)


from .theme import pin_style, theme
from .branding import app_name


class ClickThroughControl(QFrame):
    """An input-enabled companion that stays reachable while its image ignores clicks."""

    def __init__(self, pin):
        # A child of the transparent native image window could inherit its
        # input behavior. This is an independent tool window instead.
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setObjectName("pinRestoreControl")
        self.setWindowTitle(app_name() + tr(' · 恢复贴图操作'))
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 6, 6)
        row.setSpacing(8)
        self.explanation = QLabel(tr('点击会到下层窗口'))
        row.addWidget(self.explanation)
        self.restore_button = QPushButton(tr('恢复贴图操作'))
        self.restore_button.setAccessibleName(tr('恢复贴图鼠标操作'))
        self.restore_button.setToolTip(tr('恢复后可以拖动、缩放和右键操作这张贴图'))
        self.restore_button.setMinimumHeight(32)
        self.restore_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restore_button.clicked.connect(lambda: pin.set_click_through(False))
        row.addWidget(self.restore_button)
        self.close_button = QPushButton("×")
        self.close_button.setAccessibleName(tr('关闭这张贴图'))
        self.close_button.setToolTip(tr('关闭这张贴图'))
        self.close_button.setFixedSize(28, 32)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(pin.close)
        row.addWidget(self.close_button)
        self.refresh_theme()
        pin.destroyed.connect(self.deleteLater)

    def refresh_theme(self):
        p = theme()
        self.setStyleSheet(f"""
QFrame#pinRestoreControl {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 8px; }}
QLabel {{ color: {p['secondary']}; background: transparent; border: none; font-size: 12px; }}
QPushButton {{ color: {p['text']}; background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 5px; padding: 3px 8px; font-size: 12px; }}
QPushButton:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
""")
        self.adjustSize()


class PinnedImageWindow(QWidget):
    """A movable image reference, independent of the main window's visibility.

    scale_factor is physical display pixels per original image pixel: 1.0 is
    pixel-accurate even on a high-DPI monitor. Very large zooms are clipped by a
    bounded viewport rather than creating unbounded native windows.
    """

    MIN_SCALE = 0.05
    MAX_SCALE = 8.0
    MIN_OPACITY = 0.20
    MAX_OPACITY = 1.0
    MAX_VIEWPORT = QSize(4096, 3072)
    closed = Signal(object)
    # The host owns the clipboard so its own copies are never re-collected.
    copy_requested = Signal(object)
    click_through_changed = Signal(object)

    EDGE = 7  # pixels of each border that resize instead of move

    def __init__(self, image_path=None, title="", parent=None, item_id=None, pixmap=None):
        """Pin a saved file, or an in-memory pixmap such as the clipboard image."""
        # Deliberately ownerless: hiding the library must not hide its references.
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.image_path = Path(image_path) if image_path else None
        if isinstance(pixmap, QImage):
            pixmap = QPixmap.fromImage(pixmap)
        self._pixmap = QPixmap(pixmap) if pixmap is not None else QPixmap(str(self.image_path or ""))
        if self._pixmap.isNull():
            self.deleteLater()
            raise OSError(tr('无法打开贴图：{value0}', value0=self.image_path or tr('剪贴板图片')))
        # File names containing @2x must not silently reinterpret source pixels.
        self._pixmap.setDevicePixelRatio(1.0)
        self._scale_factor = 1.0
        self._opacity = 1.0
        self._drag_offset = None
        self._resize = None
        self._hover = False
        self._closing = False
        self._click_through = False
        self._restore_control = None
        self.item_id = item_id
        self._positioned = False
        self._dpr_refresh_pending = False
        self.setObjectName("pinnedImageWindow")
        self.setWindowTitle(app_name() + " · " + (title or (self.image_path.stem if self.image_path else tr('剪贴板贴图'))))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip(tr('拖动移动 · 拖边缘或角落调整大小 · 滚轮缩放 · 悬停调整不透明度 · 右键查看更多 · Esc 关闭'))
        self.setMinimumSize(48, 48)
        self._build_toolbar()
        self._escape = QShortcut(QKeySequence("Esc"), self, activated=self.close)
        self._escape.setContext(Qt.ShortcutContext.WindowShortcut)
        self._copy = QShortcut(QKeySequence.StandardKey.Copy, self, activated=lambda: self.copy_requested.emit(self))
        self._copy.setContext(Qt.ShortcutContext.WindowShortcut)
        self._scale_factor = max(self.MIN_SCALE, min(1.0, self._fit_scale()))
        self._resize_for_scale()
        self._center_in_workarea()

    def refresh_theme(self):
        self.toolbar.setStyleSheet(pin_style())
        if self._restore_control is not None:
            self._restore_control.refresh_theme()
            self._position_restore_control()
        self.update()

    @property
    def image_size(self):
        return QSize(self._pixmap.size())

    @property
    def scale_factor(self):
        return self._scale_factor

    @property
    def opacity(self):
        return self._opacity

    def source_image(self):
        """Full original pixels: the saved file when there is one, else the pinned pixmap."""
        if self.image_path and self.image_path.is_file():
            image = QImage(str(self.image_path))
            if not image.isNull():
                return image
        return self._pixmap.toImage()

    @property
    def click_through(self):
        return self._click_through

    def set_click_through(self, enabled):
        """Let clicks reach the window below, keeping a visible restore button."""
        enabled = bool(enabled)
        if enabled == self._click_through:
            return
        geometry = self.geometry()
        visible = self.isVisible()
        self._click_through = enabled
        if enabled and self._restore_control is None:
            self._restore_control = ClickThroughControl(self)
            self._restore_control.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,
                self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen))
        if not enabled and self._restore_control is not None:
            self._restore_control.hide()
        self.toolbar.hide()
        self._drag_offset = None
        # Changing window flags recreates the native window; restore it in place.
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, enabled)
        self.setGeometry(geometry)
        if visible:
            self.show()
        self._position_restore_control()
        self.update()
        self.click_through_changed.emit(self)

    def _position_restore_control(self):
        control = self._restore_control
        if control is None:
            return
        if not self._click_through or not self.isVisible() or self._closing:
            control.hide()
            return
        area = self._workarea()
        x = max(area.left() + 4, min(self.geometry().right() - control.width() + 1,
                                    area.right() - control.width() - 3))
        y = self.y() - control.height() - 6
        if y < area.top() + 4:
            y = self.y() + 6
        y = max(area.top() + 4, min(y, area.bottom() - control.height() - 3))
        control.move(x, y)
        control.show()
        control.raise_()

    def state(self):
        """Position, zoom and opacity, enough to recreate this pin after restart."""
        return {"id": self.item_id, "x": self.x(), "y": self.y(),
                "scale": round(self._scale_factor, 4), "opacity": round(self._opacity, 2)}

    def restore_state(self, state):
        try:
            self.set_scale(float(state.get("scale", self._scale_factor)))
            self.set_opacity(float(state.get("opacity", self._opacity)))
            self.move(int(state["x"]), int(state["y"]))
        except (KeyError, TypeError, ValueError):
            return
        self._positioned = True
        self._keep_visible()

    def _build_toolbar(self):
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("pinToolbar")
        self.toolbar.setCursor(Qt.CursorShape.ArrowCursor)
        self.toolbar.setStyleSheet(pin_style())
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(8, 5, 5, 5)
        row.setSpacing(7)
        self.scale_label = QLabel("100%")
        self.scale_label.setObjectName("pinScale")
        self.scale_label.setMinimumWidth(36)
        row.addWidget(self.scale_label)
        self.opacity_label = QLabel(tr('不透明度'))
        row.addWidget(self.opacity_label)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setObjectName("pinOpacity")
        self.opacity_slider.setRange(20, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.setFixedWidth(85)
        self.opacity_slider.setAccessibleName(tr('贴图不透明度'))
        self.opacity_slider.setToolTip(tr('不透明度：100%'))
        self.opacity_slider.valueChanged.connect(lambda value: self.set_opacity(value / 100))
        row.addWidget(self.opacity_slider)
        close = QPushButton("×")
        close.setObjectName("closePin")
        close.setFixedSize(24, 24)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip(tr('关闭贴图（Esc）'))
        close.setAccessibleName(tr('关闭贴图'))
        close.clicked.connect(self.close)
        row.addWidget(close)
        self.toolbar.hide()

    def _dpr(self):
        return max(0.1, float(self.devicePixelRatioF()))

    def _workarea(self):
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)

    def _viewport_limit(self):
        area = self._workarea()
        return QSize(min(self.MAX_VIEWPORT.width(), max(320, area.width() * 2)),
                     min(self.MAX_VIEWPORT.height(), max(240, area.height() * 2)))

    def _display_size(self):
        ratio = self._scale_factor / self._dpr()
        return self._pixmap.width() * ratio, self._pixmap.height() * ratio

    def _image_rect(self):
        width, height = self._display_size()
        dpr = self._dpr()
        # Align the destination to physical pixels so 100% remains crisp at 150%/200% DPI.
        left = round((self.width() - width) * dpr / 2) / dpr
        top = round((self.height() - height) * dpr / 2) / dpr
        return QRectF(left, top, width, height)

    def _resize_for_scale(self):
        width, height = self._display_size()
        limit = self._viewport_limit()
        self.resize(max(48, min(limit.width(), math.ceil(width))),
                    max(48, min(limit.height(), math.ceil(height))))
        self.scale_label.setText(f"{self._scale_factor * 100:.0f}%")
        self.scale_label.setToolTip(tr('滚轮缩放；右键可恢复 100% 或适合屏幕'))
        self._position_toolbar()
        self.update()

    def set_scale(self, factor, anchor=None):
        """Set zoom, optionally keeping a local mouse position over the same pixel."""
        factor = float(factor)
        if not math.isfinite(factor):
            raise ValueError(tr('贴图缩放比例必须是有限数值'))
        factor = max(self.MIN_SCALE, min(self.MAX_SCALE, factor))
        if math.isclose(factor, self._scale_factor):
            return
        anchor = QPointF(anchor) if anchor is not None else QPointF(self.rect().center())
        old_rect = self._image_rect()
        relative = QPointF((anchor.x() - old_rect.x()) / old_rect.width(),
                          (anchor.y() - old_rect.y()) / old_rect.height())
        global_anchor = QPointF(self.pos()) + anchor
        self._scale_factor = factor
        self._resize_for_scale()
        new_rect = self._image_rect()
        next_anchor = QPointF(new_rect.x() + relative.x() * new_rect.width(),
                             new_rect.y() + relative.y() * new_rect.height())
        next_pos = global_anchor - next_anchor
        self.move(round(next_pos.x()), round(next_pos.y()))
        self._keep_visible()

    def set_opacity(self, value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(tr('贴图不透明度必须是有限数值'))
        self._opacity = max(self.MIN_OPACITY, min(self.MAX_OPACITY, value))
        self.setWindowOpacity(self._opacity)
        percentage = round(self._opacity * 100)
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(percentage)
        self.opacity_slider.blockSignals(False)
        self.opacity_slider.setToolTip(tr('不透明度：{value0}%', value0=percentage))

    def _fit_scale(self):
        area, dpr = self._workarea(), self._dpr()
        return min(area.width() * 0.88 * dpr / self._pixmap.width(),
                   area.height() * 0.88 * dpr / self._pixmap.height())

    def fit_to_screen(self):
        self.set_scale(self._fit_scale())
        self._center_in_workarea()

    def actual_size(self):
        self.set_scale(1.0)

    def _center_in_workarea(self):
        area = self._workarea()
        self.move(area.center() - self.rect().center())
        self._positioned = True
        self._position_toolbar()

    def _keep_visible(self):
        area = self._workarea()
        visible_width = min(96, self.width())
        visible_height = min(36, self.height())
        x = max(area.left() - self.width() + visible_width,
                min(self.x(), area.right() - visible_width + 1))
        y = max(area.top(), min(self.y(), area.bottom() - visible_height + 1))
        self.move(x, y)
        self._position_toolbar()

    def _position_toolbar(self):
        if not hasattr(self, "toolbar"):
            return
        area = self._workarea()
        left = max(0, area.left() - self.x())
        right = min(self.width(), area.right() - self.x() + 1)
        visible_width = max(36, right - left - 12)
        show_opacity = visible_width >= 230
        self.opacity_label.setVisible(show_opacity)
        self.opacity_slider.setVisible(show_opacity)
        self.scale_label.setVisible(visible_width >= 80)
        self.toolbar.adjustSize()
        x = max(left + 4, right - self.toolbar.width() - 6)
        self.toolbar.move(x, 6)
        self.toolbar.raise_()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme()["card"]))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                              not math.isclose(self._scale_factor, 1.0))
        painter.drawPixmap(self._image_rect(), self._pixmap, QRectF(self._pixmap.rect()))
        if self._click_through:
            # A dashed accent edge marks a reference that ignores the mouse.
            pen = QPen(QColor(theme()["accent"]), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(self.rect().adjusted(1, 1, -1, -1))
        else:
            painter.setPen(QPen(QColor(theme()["border"]), 1))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            if self._hover and min(self.width(), self.height()) >= 60:
                # A small corner grip shows that the edges can be dragged.
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(QColor(theme()["secondary"]), 1.4))
                right, bottom = self.width() - 4, self.height() - 4
                for offset in (5, 9, 13):
                    painter.drawLine(QPointF(right - offset, bottom), QPointF(right, bottom - offset))

    def enterEvent(self, event):
        self._hover = True
        self.update()
        self._position_toolbar()
        if not self._click_through:
            self.toolbar.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        self.toolbar.hide()
        super().leaveEvent(event)

    def _edges_at(self, point):
        """Which borders a local point is on: a subset of "lrtb"."""
        margin = self.EDGE if min(self.width(), self.height()) >= 4 * self.EDGE else 3
        edges = ""
        if point.x() < margin:
            edges += "l"
        elif point.x() >= self.width() - margin:
            edges += "r"
        if point.y() < margin:
            edges += "t"
        elif point.y() >= self.height() - margin:
            edges += "b"
        return edges

    @staticmethod
    def _edge_cursor(edges):
        if edges in ("lt", "rb"):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in ("rt", "lb"):
            return Qt.CursorShape.SizeBDiagCursor
        if edges in ("l", "r"):
            return Qt.CursorShape.SizeHorCursor
        if edges in ("t", "b"):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.OpenHandCursor

    def resize_from_edges(self, edges, start_geometry, start_scale, dx, dy):
        """Resize by dragging the given borders, keeping the image's proportions.

        The opposite border or corner stays where it was, like resizing a
        normal window. The zoom stays within MIN_SCALE and MAX_SCALE.
        """
        width, height = start_geometry.width(), start_geometry.height()
        new_width = width + (dx if "r" in edges else -dx if "l" in edges else 0)
        new_height = height + (dy if "b" in edges else -dy if "t" in edges else 0)
        horizontal, vertical = any(edge in edges for edge in "lr"), any(edge in edges for edge in "tb")
        if horizontal and (not vertical or abs(new_width - width) / width >= abs(new_height - height) / height):
            ratio = max(1, new_width) / width
        else:
            ratio = max(1, new_height) / height
        self._scale_factor = max(self.MIN_SCALE, min(self.MAX_SCALE, start_scale * ratio))
        self._resize_for_scale()
        x = start_geometry.x() + start_geometry.width() - self.width() if "l" in edges else start_geometry.x()
        y = start_geometry.y() + start_geometry.height() - self.height() if "t" in edges else start_geometry.y()
        self.move(x, y)
        self._position_toolbar()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._edges_at(event.position().toPoint())
            if edges:
                self._resize = (edges, event.globalPosition().toPoint(), self.geometry(), self._scale_factor)
            else:
                self._drag_offset = event.globalPosition().toPoint() - self.pos()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize is not None and event.buttons() & Qt.MouseButton.LeftButton:
            edges, start, geometry, scale = self._resize
            delta = event.globalPosition().toPoint() - start
            self.resize_from_edges(edges, geometry, scale, delta.x(), delta.y())
            event.accept()
        elif self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._keep_visible()
            event.accept()
        else:
            if not event.buttons():
                self.setCursor(self._edge_cursor(self._edges_at(event.position().toPoint())))
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resize is not None:
                self._resize = None
                self._keep_visible()
            self._drag_offset = None
            self.setCursor(self._edge_cursor(self._edges_at(event.position().toPoint())))
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            delta = event.pixelDelta().y()
        if delta:
            steps = max(-4.0, min(4.0, delta / 120))
            self.set_scale(self._scale_factor * (1.15 ** steps), event.position())
            event.accept()
        else:
            super().wheelEvent(event)

    def _context_menu(self):
        # The close action may delete this window while QMenu.exec is running.
        # Keep the temporary menu independent so its cleanup remains valid.
        menu = QMenu()
        menu.addAction(tr('100% · 原始像素'), self.actual_size)
        menu.addAction(tr('适合屏幕'), self.fit_to_screen)
        opacity_menu = menu.addMenu(tr('不透明度'))
        group = QActionGroup(opacity_menu)
        group.setExclusive(True)
        for percentage in (100, 90, 80, 70, 60, 50, 40, 30, 20):
            action = opacity_menu.addAction(f"{percentage}%")
            action.setCheckable(True)
            action.setChecked(round(self.opacity * 100) == percentage)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=percentage: self.set_opacity(value / 100))
        menu.addSeparator()
        menu.addAction(tr('复制图片\tCtrl+C'), lambda: self.copy_requested.emit(self))
        menu.addAction(tr('让鼠标点击下层窗口（显示恢复按钮）'), lambda: self.set_click_through(True))
        menu.addSeparator()
        menu.addAction(tr('关闭贴图\tEsc'), self.close)
        return menu

    def contextMenuEvent(self, event):
        position = event.globalPos()
        event.accept()
        menu = self._context_menu()
        try:
            menu.exec(position)
        finally:
            menu.deleteLater()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        self._position_toolbar()
        self._position_restore_control()
        super().resizeEvent(event)

    def moveEvent(self, event):
        self._position_toolbar()
        self._position_restore_control()
        super().moveEvent(event)

    def showEvent(self, event):
        if not self._positioned:
            self._center_in_workarea()
        self._keep_visible()
        super().showEvent(event)
        self._position_restore_control()

    def hideEvent(self, event):
        if self._restore_control is not None:
            self._restore_control.hide()
        super().hideEvent(event)

    def _refresh_dpr(self):
        self._dpr_refresh_pending = False
        if not self._closing:
            self._resize_for_scale()
            self._keep_visible()

    def event(self, event):
        if (event.type() in (QEvent.Type.ScreenChangeInternal, QEvent.Type.DevicePixelRatioChange)
                and hasattr(self, "_pixmap") and not self._dpr_refresh_pending):
            self._dpr_refresh_pending = True
            QTimer.singleShot(0, self._refresh_dpr)
        return super().event(event)

    def closeEvent(self, event):
        if not self._closing:
            self._closing = True
            if self._restore_control is not None:
                self._restore_control.hide()
                self._restore_control.deleteLater()
                self._restore_control = None
            self.closed.emit(self)
        super().closeEvent(event)


PinWindow = PinnedImageWindow
