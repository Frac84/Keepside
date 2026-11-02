"""A draggable shelf entry with event-driven edge docking and auto-hide.

The tucked handle stays *inside* the selected screen's work area. Moving a
window outside one monitor would otherwise expose it on the neighbouring one.
Only the short hover/leave transitions animate; there is no idle polling.
"""
from __future__ import annotations
from .i18n import tr
from .branding import app_name

import ctypes
from ctypes import wintypes
import os

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QPushButton


BALL_SIZE = QSize(60, 60)
SNAP_DISTANCE = 24
HANDLE_SIZE = 14
HIDE_DELAY_MS = 1100
DOCK_EDGES = ("left", "right", "top", "bottom")


def _clamp(point, size, area):
    return QPoint(max(area.left(), min(point.x(), max(area.left(), area.right() - size.width() + 1))),
                  max(area.top(), min(point.y(), max(area.top(), area.bottom() - size.height() + 1))))


def _screen_for(point):
    screen = QGuiApplication.screenAt(point)
    if screen is not None:
        return screen
    screens = QGuiApplication.screens()
    if not screens:
        return None
    # A disconnected monitor's saved coordinates should land on the nearest
    # remaining screen, including screens left/above the primary display.
    def distance(candidate):
        area = candidate.availableGeometry()
        nearest = _clamp(point, QSize(1, 1), area)
        return (nearest - point).manhattanLength()
    return min(screens, key=distance)


def _available_area(point, size=BALL_SIZE):
    screen = _screen_for(point + QPoint(size.width() // 2, size.height() // 2))
    return screen.availableGeometry() if screen is not None else QRect(point, size)


def clamp_position(point, size):
    # Shared by the panel, which keeps its normal 8 px desktop margin.
    area = _available_area(point, size).adjusted(8, 8, -8, -8)
    return _clamp(point, size, area)


def dock_position(point, area, edge="", threshold=SNAP_DISTANCE):
    """Return a fully visible anchor and an optional nearest work-area edge."""
    point = _clamp(point, BALL_SIZE, area)
    distances = {
        "left": point.x() - area.left(),
        "right": area.right() - point.x() - BALL_SIZE.width() + 1,
        "top": point.y() - area.top(),
        "bottom": area.bottom() - point.y() - BALL_SIZE.height() + 1,
    }
    if edge not in DOCK_EDGES:
        edge = min(distances, key=distances.get)
        if distances[edge] > threshold:
            return point, ""
    if edge == "left":
        point.setX(area.left())
    elif edge == "right":
        point.setX(max(area.left(), area.right() - BALL_SIZE.width() + 1))
    elif edge == "top":
        point.setY(area.top())
    else:
        point.setY(max(area.top(), area.bottom() - BALL_SIZE.height() + 1))
    return point, edge


def dock_geometry(anchor, edge, retracted=False):
    """A narrow in-bounds hit target instead of an off-screen full window."""
    rect = QRect(anchor, BALL_SIZE)
    if retracted:
        if edge == "left":
            rect.setWidth(HANDLE_SIZE)
        elif edge == "right":
            rect.setLeft(rect.right() - HANDLE_SIZE + 1)
        elif edge == "top":
            rect.setHeight(HANDLE_SIZE)
        elif edge == "bottom":
            rect.setTop(rect.bottom() - HANDLE_SIZE + 1)
    return rect


def _motion_allowed():
    """Read the Windows animation preference; never change system settings.

    SPI_GETCLIENTAREAANIMATION:
    https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-systemparametersinfow
    """
    if QGuiApplication.platformName() != "windows" or os.name != "nt":
        return False
    try:
        user = ctypes.WinDLL("user32.dll", winmode=0x00000800)
        query = user.SystemParametersInfoW
        query.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        query.restype = wintypes.BOOL
        enabled = wintypes.BOOL()
        return bool(query(0x1042, 0, ctypes.byref(enabled), 0) and enabled.value)
    except (OSError, AttributeError):
        return False


class FloatingBall(QPushButton):
    """Click opens the shelf; a drag changes position without opening it."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAutoFillBackground(False)
        self.setAcceptDrops(True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")
        self.setWindowTitle(tr('{app} · 悬浮球', app=app_name()))
        self.setAccessibleName(tr('{app}悬浮球', app=app_name()))
        self.setAccessibleDescription(tr('拖入文件直接打开中转篮。篮中有文件时点击继续中转，空篮时点击打开最近文件。拖动可移动，靠近屏幕边缘自动吸附和收起，鼠标移入展开。'))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resize(BALL_SIZE)
        self._press_global = self._press_position = None
        self._did_drag = self._hover = False
        self._dock_edge = ""
        self._dock_area = QRect()
        self._anchor = QPoint(self.pos())
        self._retracted = self._menu_open = self._drop_hover = False
        self._basket_count = 0
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self.retract)
        self._motion = QPropertyAnimation(self, b"geometry", self)
        self._motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._motion.finished.connect(self.update)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._screen_layout_changed)
        application = QGuiApplication.instance()
        application.screenAdded.connect(self._screen_added)
        application.screenRemoved.connect(self._queue_screen_layout)
        for screen in QGuiApplication.screens():
            screen.availableGeometryChanged.connect(self._queue_screen_layout)
        self.clicked.connect(panel.open_from_ball)
        panel.destroyed.connect(self.deleteLater)
        basket = getattr(getattr(panel, "host", None), "file_basket", None)
        if basket is not None:
            basket.changed.connect(self.refresh_basket_count)
        if getattr(panel, "lock_button", None) is not None:
            panel.lock_button.toggled.connect(lambda _checked: self.refresh_basket_count())
        self.refresh_basket_count()

    @property
    def basket_count(self):
        return self._basket_count

    def refresh_basket_count(self):
        locked = bool(getattr(self.panel, "files_locked", False))
        self._basket_count = 0 if locked else max(0, int(getattr(self.panel, "basket_count", 0)))
        status = tr('最近文件和中转篮已锁定') if locked else (
            tr('中转篮已暂存 {v0} 个文件', v0=self._basket_count) if self._basket_count else tr('拖入文件放进中转篮'))
        action = tr('点击继续使用中转篮') if self._basket_count else tr('点击打开最近文件')
        self.setToolTip(tr('{app}\n{status}\n{action}；拖入文件直接加入中转篮\n拖动调整位置，靠近屏幕边缘自动吸附', app=app_name(), status=status, action=action))
        self.update()

    @property
    def dock_edge(self):
        return self._dock_edge

    @property
    def retracted(self):
        return self._retracted

    def persistent_position(self):
        return QPoint(self._anchor if self._dock_edge else self.pos())

    def restore_position(self, point, edge=""):
        """Restore a full-size anchor, not the transient tucked handle's x/y."""
        self._stop_motion()
        self._dock_area = _available_area(point)
        self._anchor, self._dock_edge = dock_position(point, self._dock_area, edge)
        self._retracted = False
        self.setGeometry(QRect(self._anchor, BALL_SIZE))
        self._schedule_hide()

    def set_position(self, point, snap=True):
        self._stop_motion()
        self._dock_area = _available_area(point)
        if snap:
            self._anchor, self._dock_edge = dock_position(point, self._dock_area)
        else:
            self._anchor, self._dock_edge = _clamp(point, BALL_SIZE, self._dock_area), ""
        self._retracted = False
        self.setGeometry(QRect(self._anchor, BALL_SIZE))
        self._schedule_hide()

    def _stop_motion(self):
        self._hide_timer.stop()
        self._motion.stop()

    def _queue_screen_layout(self, *_args):
        self._layout_timer.start(0)

    def _screen_added(self, screen):
        screen.availableGeometryChanged.connect(self._queue_screen_layout)
        self._queue_screen_layout()

    def _screen_layout_changed(self):
        point = self.persistent_position()
        self._press_global = self._press_position = None
        self.setDown(False)
        self.restore_position(point, self._dock_edge)
        self._hover = self.isVisible() and self.geometry().contains(QCursor.pos())
        self._schedule_hide()
        self.panel.save_ball_position()

    def _blocked(self):
        return (not self.isVisible() or self._press_global is not None or self._hover or
                self._menu_open or self._drop_hover or getattr(self.panel, "_interaction_depth", 0) > 0)

    def _schedule_hide(self):
        self._hide_timer.stop()
        if self._dock_edge and not self._retracted and not self._blocked():
            self._hide_timer.start()

    def _transition(self, retracted, animate=True):
        self._stop_motion()
        self._retracted = bool(retracted)
        target = dock_geometry(self._anchor, self._dock_edge, self._retracted)
        if animate and self.isVisible() and _motion_allowed() and self.geometry() != target:
            self._motion.setDuration(130 if retracted else 180)
            self._motion.setStartValue(self.geometry())
            self._motion.setEndValue(target)
            self._motion.start()
        else:
            self.setGeometry(target)
            self.update()

    def retract(self, animate=True):
        if self._dock_edge and not self._blocked():
            self._transition(True, animate)

    def reveal(self, animate=True):
        if self._dock_edge:
            self._transition(False, animate)

    def showEvent(self, event):
        super().showEvent(event)
        # Work-area changes (taskbar, resolution or removed display) are
        # reconciled each time the ball returns from the shelf/capture.
        if self._dock_edge:
            self.restore_position(self._anchor, self._dock_edge)
        else:
            self.set_position(self.pos())
        self._hover = self.geometry().contains(QCursor.pos())
        self._schedule_hide()

    def hideEvent(self, event):
        self._stop_motion()
        self._layout_timer.stop()
        self._hover = self._drop_hover = False
        self._press_global = self._press_position = None
        self.setDown(False)
        # Panel placement reads this anchor after hide(); keep it stable.
        if self._dock_edge:
            self._retracted = False
            self.setGeometry(QRect(self._anchor, BALL_SIZE))
        super().hideEvent(event)

    def moveEvent(self, event):
        # Respect callers using QWidget.move() on a visible, expanded ball.
        # Internal transitions already update the anchor before geometry.
        if (hasattr(self, "_motion") and not self._retracted and self.size() == BALL_SIZE
                and self._motion.state() != QPropertyAnimation.State.Running
                and self.pos() != self._anchor):
            self._anchor = QPoint(self.pos())
            self._dock_edge = ""
            self._hide_timer.stop()
        super().moveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = getattr(self.panel, "shelf_dark", True)
        if self._retracted and self._motion.state() != QPropertyAnimation.State.Running:
            # A calm edge tab offers an easy hit area while showing only a
            # slim accent, rather than an unexplained fragment of the icon.
            if self._dock_edge in ("left", "right"):
                handle = QRectF(3, 10, 8, 40)
                grip = QRectF(6, 24, 2, 12)
            else:
                handle = QRectF(10, 3, 40, 8)
                grip = QRectF(24, 6, 12, 2)
            painter.setPen(QPen(QColor("#71849E") if dark else QColor("#A3BAD9"), 1))
            painter.setBrush(QColor("#2D405A") if dark else QColor("#E5EEFA"))
            painter.drawRoundedRect(handle, 4, 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#DCEEFF") if dark else QColor("#2862A6"))
            painter.drawRoundedRect(grip, 1, 1)
            return
        # Reveal/conceal by clipping the painted ball at the selected edge;
        # the whole native hit rectangle stays inside its work area.
        if self._dock_edge == "left":
            painter.translate(self.width() - BALL_SIZE.width(), 0)
        elif self._dock_edge == "top":
            painter.translate(0, self.height() - BALL_SIZE.height())
        # Small painted shadow, recalculated only on hover/interaction.
        for inset in range(6):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(4, 12, 26, 2 + inset * 2))
            painter.drawEllipse(QRectF(inset + 1, inset + 3, 58 - inset * 2, 56 - inset * 2))
        gradient = QLinearGradient(10, 8, 50, 53)
        gradient.setColorAt(0, QColor("#45576F") if dark else QColor("#FFFFFF"))
        gradient.setColorAt(1, QColor("#1C2C42") if dark else QColor("#DCE9F9"))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor("#A6CEFF") if self._hover else QColor("#71849E") if dark else QColor("#C3D5ED"), 1.2))
        painter.drawEllipse(QRectF(6, 6, 48, 48))
        color = QColor("#DCEEFF") if dark else QColor("#2862A6")
        painter.setPen(QPen(color, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Capture corners and a small breeze: the sole visual accent.
        path = QPainterPath()
        path.moveTo(20, 25); path.lineTo(20, 20); path.lineTo(25, 20)
        path.moveTo(35, 20); path.lineTo(40, 20); path.lineTo(40, 25)
        path.moveTo(40, 35); path.lineTo(40, 40); path.lineTo(35, 40)
        path.moveTo(25, 40); path.lineTo(20, 40); path.lineTo(20, 35)
        path.moveTo(24, 29); path.cubicTo(28, 24, 31, 33, 36, 27)
        path.moveTo(25, 34); path.cubicTo(28, 30, 32, 37, 35, 32)
        painter.drawPath(path)
        if self._drop_hover:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#4CC2FF" if dark else "#0067C0"), 2.4))
            painter.drawEllipse(QRectF(4, 4, 52, 52))
        if self._basket_count:
            badge = QRectF(36 if self._basket_count > 99 else 41, 40,
                           23 if self._basket_count > 99 else 18, 18)
            painter.setPen(QPen(QColor("#202020" if dark else "#FFFFFF"), 1))
            painter.setBrush(QColor("#4CC2FF" if dark else "#0067C0"))
            painter.drawRoundedRect(badge, 9, 9)
            painter.setPen(QColor("#202020" if dark else "#FFFFFF"))
            font = painter.font()
            font.setPixelSize(10)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter,
                             "99+" if self._basket_count > 99 else str(self._basket_count))

    def enterEvent(self, event):
        self._hover = True
        self.reveal()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._schedule_hide()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.reveal(animate=False)
            self._stop_motion()
            self._press_global = event.globalPosition().toPoint()
            self._press_position = self.pos()
            self._did_drag = False
            self.setDown(True)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_global is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._press_global
            threshold = QGuiApplication.styleHints().startDragDistance()
            if self._did_drag or delta.manhattanLength() >= threshold:
                self._did_drag = True
                self._dock_edge = ""
                self._retracted = False
                point = self._press_position + delta
                self._dock_area = _available_area(point)
                self._anchor = _clamp(point, BALL_SIZE, self._dock_area)
                self.setGeometry(QRect(self._anchor, BALL_SIZE))
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._press_global is not None:
            dragged = self._did_drag
            self._press_global = self._press_position = None
            self.setDown(False)
            if dragged:
                self.set_position(self.pos())
                self.panel.save_ball_position()
            elif self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            self._hover = self.geometry().contains(QCursor.pos())
            self._schedule_hide()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self._menu_open = True
        self.reveal(animate=False)
        self._stop_motion()
        menu = QMenu(self)
        menu.addAction(tr('展开最近文件'), lambda: self.panel.open(files=True, from_ball=True))
        if hasattr(self.panel, "open_basket"):
            menu.addAction(tr('打开中转篮'), lambda: self.panel.open_basket(from_ball=True))
        if not getattr(self.panel, "files_locked", False) and hasattr(self.panel, "lock_files"):
            menu.addAction(tr('锁定最近文件'), self.panel.lock_files)
        menu.addAction(tr('打开内容库'), lambda: self.panel.open(files=False, from_ball=True))
        menu.addSeparator()
        if self._dock_edge:
            menu.addAction(tr('取消贴边吸附'), self.undock)
        else:
            menu.addAction(tr('吸附到最近边缘'), self.dock_to_nearest_edge)
        menu.addAction(tr('隐藏悬浮球'), lambda: self.panel.hide_ball(permanent=True))
        self.panel._interaction_depth += 1
        try:
            menu.exec(event.globalPos())
        finally:
            self.panel._interaction_depth -= 1
            self._menu_open = False
            self._hover = self.isVisible() and self.geometry().contains(QCursor.pos())
            self._schedule_hide()
            menu.deleteLater()

    def undock(self):
        """Context-menu alternative to dragging away from the screen edge."""
        area = _available_area(self._anchor)
        point = QPoint(self._anchor)
        inset = SNAP_DISTANCE + 12
        if self._dock_edge == "left":
            point.setX(point.x() + inset)
        elif self._dock_edge == "right":
            point.setX(point.x() - inset)
        elif self._dock_edge == "top":
            point.setY(point.y() + inset)
        elif self._dock_edge == "bottom":
            point.setY(point.y() - inset)
        self.set_position(_clamp(point, BALL_SIZE, area), snap=False)
        self.panel.save_ball_position()

    def dock_to_nearest_edge(self):
        area = _available_area(self.pos())
        point, edge = dock_position(self.pos(), area, threshold=max(area.width(), area.height()))
        self.restore_position(point, edge)
        self.panel.save_ball_position()

    def dragEnterEvent(self, event):
        if (getattr(self.panel, "files_locked", False)
                or not event.possibleActions() & Qt.DropAction.CopyAction):
            event.ignore()
            return
        if self.panel.drop_paths(event.mimeData()):
            self._drop_hover = True
            self.reveal(animate=False)
            self._stop_motion()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.update()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dragLeaveEvent(self, event):
        self._drop_hover = False
        self._hover = self.geometry().contains(QCursor.pos())
        self._schedule_hide()
        event.accept()

    def dropEvent(self, event):
        self._drop_hover = False
        if (getattr(self.panel, "files_locked", False)
                or not event.possibleActions() & Qt.DropAction.CopyAction):
            event.ignore()
            self._schedule_hide()
            self.update()
            return
        paths = self.panel.drop_paths(event.mimeData())
        if paths:
            self.panel.open_basket(from_ball=True)
            self.panel.add_basket_paths(paths)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
        self.refresh_basket_count()
        self._schedule_hide()
