"""Native-widget edge interactions, isolated from real desktop input/data."""
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QContextMenuEvent, QEnterEvent, QMouseEvent
from PySide6.QtWidgets import QApplication
import pytest

from snapvault import floating_ball as module


class Panel(QObject):
    def __init__(self):
        super().__init__()
        self.shelf_dark = True
        self._interaction_depth = 0
        self.opens = 0
        self.saved = []

    def open_from_ball(self):
        self.opens += 1

    def save_ball_position(self):
        self.saved.append((self.ball.persistent_position(), self.ball.dock_edge))

    def open(self, **kwargs):
        self.open_from_ball()

    def hide_ball(self, **kwargs):
        self.ball.hide()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ball(app, monkeypatch):
    monkeypatch.setattr(module, "_available_area", lambda *_: QRect(0, 0, 1200, 800))
    monkeypatch.setattr(module, "_motion_allowed", lambda: False)
    panel = Panel()
    panel.ball = widget = module.FloatingBall(panel)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.set_position(QPoint(500, 300))
    widget.show()
    widget._hover = False
    yield widget, panel
    widget.hide()
    widget.deleteLater()
    panel.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("point,edge,anchor", [
    (QPoint(23, 300), "left", QPoint(0, 300)),
    (QPoint(1116, 300), "right", QPoint(1140, 300)),
    (QPoint(500, 24), "top", QPoint(500, 0)),
    (QPoint(500, 716), "bottom", QPoint(500, 740)),
    (QPoint(500, 300), "", QPoint(500, 300)),
])
def test_release_snaps_only_within_edge_threshold(point, edge, anchor):
    assert module.dock_position(point, QRect(0, 0, 1200, 800)) == (anchor, edge)


@pytest.mark.parametrize("edge", module.DOCK_EDGES)
def test_tucked_handle_stays_in_work_area_on_negative_coordinate_monitor(edge):
    # The work area excludes a 40 px taskbar, and this monitor is left of the
    # primary display. No part of the native handle spills onto its neighbour.
    area = QRect(-1280, -100, 1280, 984)
    anchor, restored_edge = module.dock_position(QPoint(-500, 300), area, edge)
    rect = module.dock_geometry(anchor, restored_edge, True)
    assert area.contains(rect)
    assert rect.size() in (QSize(14, 60), QSize(60, 14))
    assert module.dock_geometry(anchor, edge).size() == QSize(60, 60)


def test_distant_restore_clamps_to_new_monitor_work_area():
    anchor, edge = module.dock_position(QPoint(-6000, 9000), QRect(0, 0, 1200, 760), "right")
    assert edge == "right" and anchor == QPoint(1140, 700)


def test_nonprimary_screen_is_chosen_by_ball_centre_and_nearest_distance(monkeypatch):
    class Screen:
        def __init__(self, area):
            self.area = area

        def availableGeometry(self):
            return self.area

    left, primary = Screen(QRect(-1280, 0, 1280, 984)), Screen(QRect(0, 0, 1920, 1040))

    class Application:
        @staticmethod
        def screenAt(point):
            return next((screen for screen in (left, primary) if screen.area.contains(point)), None)

        @staticmethod
        def screens():
            return [left, primary]

    monkeypatch.setattr(module, "QGuiApplication", Application)
    assert module._screen_for(QPoint(-400, 500)) is left
    assert module._screen_for(QPoint(-8000, 500)) is left
    assert module._available_area(QPoint(-45, 300)) == left.area


def test_idle_retract_hover_reveal_and_leave_rearm_without_polling(ball):
    widget, _panel = ball
    widget.restore_position(QPoint(1130, 300))
    assert widget.dock_edge == "right" and widget._hide_timer.isActive()
    widget.retract(animate=False)
    assert widget.retracted and widget.geometry() == QRect(1186, 300, 14, 60)
    assert not widget._hide_timer.isActive()
    assert widget.persistent_position() == QPoint(1140, 300)
    widget.enterEvent(QEnterEvent(QPointF(5, 30), QPointF(5, 30), QPointF(1191, 330)))
    assert not widget.retracted and widget.geometry() == QRect(1140, 300, 60, 60)
    assert not widget._hide_timer.isActive()
    widget.leaveEvent(QEvent(QEvent.Type.Leave))
    assert widget._hide_timer.isActive()


def test_timer_retracts_once_then_stays_quiet(ball, app):
    from PySide6.QtTest import QTest
    widget, _panel = ball
    widget.restore_position(QPoint(0, 300), "left")
    widget._hide_timer.start(1)
    QTest.qWait(30)
    app.processEvents()
    assert widget.retracted and widget.width() == 14
    assert not widget._hide_timer.isActive()


@pytest.mark.parametrize("blocker", ["_hover", "_menu_open", "_drop_hover"])
def test_no_retraction_during_hover_menu_or_file_drag(ball, blocker):
    widget, _panel = ball
    widget.restore_position(QPoint(0, 300), "left")
    setattr(widget, blocker, True)
    widget.retract(animate=False)
    assert not widget.retracted and widget.width() == 60


def test_hidden_capture_surface_cancels_pending_retract_and_normalizes_anchor(ball, app):
    widget, panel = ball
    widget.restore_position(QPoint(0, 300), "left")
    widget.retract(animate=False)
    assert widget.width() == 14
    widget.hide()
    assert widget.width() == 60 and widget.pos() == QPoint(0, 300)
    assert not widget._hide_timer.isActive() and not widget.retracted
    widget.retract(animate=False)
    assert widget.width() == 60 and not widget.isVisible()
    widget.show()
    widget._hover = False
    widget._schedule_hide()
    assert widget._hide_timer.isActive()
    assert panel.opens == 0


def mouse_event(kind, local, global_point, button, buttons):
    return QMouseEvent(kind, QPointF(local), QPointF(global_point), button, buttons,
                       Qt.KeyboardModifier.NoModifier)


def drag(widget, start, end):
    local = QPoint(30, 30)
    widget.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, local, start,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    widget.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, local, end,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
    widget.mouseReleaseEvent(mouse_event(QEvent.Type.MouseButtonRelease, local, end,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))


def test_dragging_to_edge_snaps_and_dragging_away_undocks_without_opening(ball):
    widget, panel = ball
    drag(widget, QPoint(530, 330), QPoint(1160, 330))
    assert widget.pos() == QPoint(1140, 300) and widget.dock_edge == "right"
    assert panel.saved == [(QPoint(1140, 300), "right")] and panel.opens == 0
    drag(widget, QPoint(1170, 330), QPoint(830, 330))
    assert widget.pos() == QPoint(800, 300) and widget.dock_edge == ""
    assert panel.saved[-1] == (QPoint(800, 300), "") and panel.opens == 0
    assert not widget._hide_timer.isActive()


def test_press_interrupts_retraction_animation_then_release_can_click(ball, monkeypatch):
    widget, panel = ball
    monkeypatch.setattr(module, "_motion_allowed", lambda: True)
    widget.restore_position(QPoint(0, 300), "left")
    widget.retract()
    assert widget._motion.state() == module.QPropertyAnimation.State.Running
    widget.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, QPoint(5, 30), QPoint(5, 330),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    assert widget._motion.state() == module.QPropertyAnimation.State.Stopped
    assert widget.geometry() == QRect(0, 300, 60, 60) and not widget.retracted
    widget.retract(False)
    assert widget.width() == 60
    widget.mouseReleaseEvent(mouse_event(QEvent.Type.MouseButtonRelease, QPoint(5, 30), QPoint(5, 330),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
    assert panel.opens == 1 and panel.saved == []


def test_rapid_opposite_transitions_keep_latest_final_state(ball, monkeypatch):
    widget, _panel = ball
    monkeypatch.setattr(module, "_motion_allowed", lambda: True)
    widget.restore_position(QPoint(1140, 300), "right")
    widget.retract()
    widget._motion.setCurrentTime(60)
    widget.reveal()
    widget._motion.setCurrentTime(widget._motion.duration())
    assert not widget.retracted and widget.geometry() == QRect(1140, 300, 60, 60)
    widget.retract(False)
    assert widget.retracted and widget.geometry() == QRect(1186, 300, 14, 60)


def test_context_menu_keeps_ball_expanded_until_dismissed(ball, monkeypatch):
    widget, panel = ball
    widget.restore_position(QPoint(0, 300), "left")
    widget.retract(False)

    class Menu:
        def __init__(self, parent):
            pass

        def addAction(self, *args):
            pass

        def addSeparator(self):
            pass

        def exec(self, position):
            widget.retract(False)
            assert widget.width() == 60 and panel._interaction_depth == 1
            assert not widget._hide_timer.isActive()

        def deleteLater(self):
            pass

    monkeypatch.setattr(module, "QMenu", Menu)
    widget.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 30), QPoint(5, 330)))
    assert not widget._menu_open and panel._interaction_depth == 0


def test_context_actions_offer_dragging_alternatives_and_persist_anchor(ball):
    widget, panel = ball
    widget.dock_to_nearest_edge()
    assert widget.dock_edge == "top" and widget.pos() == QPoint(500, 0)
    widget.undock()
    assert widget.dock_edge == "" and widget.pos() == QPoint(500, 36)
    assert panel.saved[-1] == (QPoint(500, 36), "")


def test_work_area_change_reanchors_tucked_ball_and_persists_full_position(ball, monkeypatch):
    widget, panel = ball
    widget.restore_position(QPoint(1140, 300), "right")
    widget.retract(False)
    monkeypatch.setattr(module, "_available_area", lambda *_: QRect(-1280, -100, 1280, 900))
    widget._screen_layout_changed()
    assert widget.dock_edge == "right" and widget.pos() == QPoint(-60, 300)
    assert widget.size() == QSize(60, 60)
    assert panel.saved[-1] == (QPoint(-60, 300), "right")


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("edge", module.DOCK_EDGES)
def test_all_edge_handles_render_at_native_size(ball, dark, edge):
    widget, panel = ball
    panel.shelf_dark = dark
    widget.restore_position(QPoint(500, 300), edge)
    widget.retract(False)
    pixmap = widget.grab()
    assert not pixmap.isNull()
    assert pixmap.size() == widget.size()


def test_reduced_motion_skips_transition(ball, monkeypatch):
    widget, _panel = ball
    monkeypatch.setattr(module, "_motion_allowed", lambda: False)
    widget.restore_position(QPoint(0, 300), "left")
    widget.retract()
    assert widget.geometry() == QRect(0, 300, 14, 60)
    assert widget._motion.state() == module.QPropertyAnimation.State.Stopped


@pytest.mark.parametrize("edge", module.DOCK_EDGES)
def test_real_animation_completion_and_hover_reversal_preserve_dock(ball, app, monkeypatch, edge):
    """Let Qt's event loop finish transitions, including stop/finish ordering."""
    from PySide6.QtTest import QTest
    widget, _panel = ball
    monkeypatch.setattr(module, "_motion_allowed", lambda: True)
    widget.restore_position(QPoint(500, 300), edge)
    anchor = widget.persistent_position()
    expanded = module.dock_geometry(anchor, edge)
    tucked = module.dock_geometry(anchor, edge, True)
    completions = []
    widget._motion.finished.connect(lambda: completions.append(widget.geometry()))

    widget.retract()
    QTest.qWait(190)
    app.processEvents()
    assert widget._motion.state() == module.QPropertyAnimation.State.Stopped
    assert widget.geometry() == tucked and widget.retracted
    assert widget.dock_edge == edge and widget.persistent_position() == anchor
    assert completions == [tucked] and not widget._hide_timer.isActive()

    widget.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(tucked.topLeft())))
    QTest.qWait(55)
    assert widget._motion.state() == module.QPropertyAnimation.State.Running
    assert not widget.retracted and widget.dock_edge == edge
    # Mouse leaves again before expansion completes: the replacement retract
    # must win, with no late expanded geometry/anchor from the stopped motion.
    widget.leaveEvent(QEvent(QEvent.Type.Leave))
    widget._hide_timer.start(1)
    QTest.qWait(190)
    app.processEvents()
    assert widget.geometry() == tucked and widget.retracted
    assert widget.dock_edge == edge and widget.persistent_position() == anchor
    assert not widget._hide_timer.isActive()

    widget.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(tucked.topLeft())))
    QTest.qWait(235)
    app.processEvents()
    assert widget.geometry() == expanded and not widget.retracted
    assert widget.dock_edge == edge and widget.persistent_position() == anchor
    assert not widget._hide_timer.isActive()
    assert widget._motion.state() == module.QPropertyAnimation.State.Stopped


@pytest.mark.parametrize("edge,delta", [
    ("left", QPoint(150, 90)),
    ("right", QPoint(-150, 90)),
    ("top", QPoint(150, 150)),
    ("bottom", QPoint(150, -150)),
])
def test_drag_interrupts_running_animation_without_late_geometry_or_dock_reset(ball, app, monkeypatch, edge, delta):
    from PySide6.QtTest import QTest
    widget, panel = ball
    monkeypatch.setattr(module, "_motion_allowed", lambda: True)
    widget.restore_position(QPoint(500, 300), edge)
    anchor = widget.persistent_position()
    widget.retract()
    QTest.qWait(45)
    assert widget._motion.state() == module.QPropertyAnimation.State.Running
    press = anchor + QPoint(30, 30)
    drag(widget, press, press + delta)
    assert widget.dock_edge == "" and widget.persistent_position() == anchor + delta
    assert widget.geometry() == QRect(anchor + delta, module.BALL_SIZE)
    assert not widget._hide_timer.isActive()
    QTest.qWait(250)
    app.processEvents()
    assert widget.geometry() == QRect(anchor + delta, module.BALL_SIZE)
    assert widget.dock_edge == "" and not widget.retracted
    assert widget._motion.state() == module.QPropertyAnimation.State.Stopped
    assert panel.saved == [(anchor + delta, "")] and panel.opens == 0


@pytest.mark.parametrize("edge", module.DOCK_EDGES)
def test_capture_hide_interrupts_real_animation_without_late_show_or_move(ball, app, monkeypatch, edge):
    from PySide6.QtTest import QTest
    widget, panel = ball
    monkeypatch.setattr(module, "_motion_allowed", lambda: True)
    widget.restore_position(QPoint(500, 300), edge)
    anchor = widget.persistent_position()
    widget.retract()
    QTest.qWait(45)
    assert widget._motion.state() == module.QPropertyAnimation.State.Running
    widget.hide()  # ShelfPanel.suspend_for_capture() uses this exact path.
    assert widget._motion.state() == module.QPropertyAnimation.State.Stopped
    assert not widget._hide_timer.isActive()
    QTest.qWait(250)
    app.processEvents()
    assert not widget.isVisible() and not widget.retracted
    assert widget.geometry() == QRect(anchor, module.BALL_SIZE)
    assert widget.dock_edge == edge and widget.persistent_position() == anchor
    assert panel.opens == 0 and panel.saved == []
    widget.show()
    assert widget.geometry() == QRect(anchor, module.BALL_SIZE)
    assert widget.dock_edge == edge and not widget.retracted
