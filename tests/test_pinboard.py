"""Floating-image tests use only isolated PNGs and synthetic Qt events."""
import hashlib
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QImage, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget

from snapvault.pinboard import PinnedImageWindow


@pytest.fixture(scope="session")
def pin_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "reference@2x.png"
    image = QImage(320, 180, QImage.Format.Format_ARGB32)
    image.fill(0xFFB8D9F3)
    assert image.save(str(path))
    return path


@pytest.fixture
def make_pin(pin_app, image_path):
    windows = []

    def create(path=image_path, **kwargs):
        window = PinnedImageWindow(path, **kwargs)
        windows.append(window)
        return window

    yield create
    for window in windows:
        if shiboken6.isValid(window):
            window.close()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    pin_app.processEvents()


def test_independent_topmost_window_keeps_original_pixels(make_pin, pin_app, image_path):
    host = QWidget()
    pin = make_pin(title="测试贴图", parent=host)
    assert pin.parentWidget() is None
    assert pin.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert pin.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert pin.windowFlags() & Qt.WindowType.Tool
    assert pin.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert pin.image_size == QSize(320, 180)  # @2x file name must not halve source pixels.
    assert "捕风捉影" in pin.windowTitle() and "测试贴图" in pin.windowTitle()
    host.show()
    pin.show()
    pin_app.processEvents()
    host.hide()
    pin_app.processEvents()
    assert pin.isVisible(), "Hiding the library must not hide its references"
    host.close()


def test_scale_is_bounded_and_actual_size_obeys_high_dpi(make_pin, monkeypatch):
    pin = make_pin()
    pin.set_scale(10**30)
    assert pin.scale_factor == pin.MAX_SCALE
    assert pin.width() <= pin.MAX_VIEWPORT.width()
    assert pin.height() <= pin.MAX_VIEWPORT.height()
    pin.set_scale(-10)
    assert pin.scale_factor == pin.MIN_SCALE
    assert pin.width() >= 48 and pin.height() >= 36
    with pytest.raises(ValueError):
        pin.set_scale(float("nan"))
    monkeypatch.setattr(pin, "_dpr", lambda: 2.0)
    pin.actual_size()
    rect = pin._image_rect()
    assert pin.scale_factor == 1.0
    assert rect.width() * 2 == 320 and rect.height() * 2 == 180
    assert (rect.x() * 2).is_integer() and (rect.y() * 2).is_integer()


def test_fit_to_screen_places_window_in_workarea(make_pin):
    pin = make_pin()
    pin.set_scale(pin.MAX_SCALE)
    pin.fit_to_screen()
    area = pin._workarea()
    assert area.contains(pin.geometry())
    assert pin.MIN_SCALE <= pin.scale_factor <= pin.MAX_SCALE


def test_opacity_slider_and_limits_stay_in_sync(make_pin):
    pin = make_pin()
    pin.set_opacity(-1)
    assert pin.opacity == 0.2 and pin.opacity_slider.value() == 20
    pin.set_opacity(2)
    assert pin.opacity == 1.0 and pin.opacity_slider.value() == 100
    pin.opacity_slider.setValue(45)
    assert pin.opacity == pytest.approx(0.45)
    with pytest.raises(ValueError):
        pin.set_opacity(float("inf"))


def test_wheel_and_drag_are_local_qt_events(make_pin, pin_app):
    pin = make_pin()
    pin.show()
    pin_app.processEvents()
    pin.actual_size()
    local = QPointF(30, 30)
    global_point = QPointF(pin.mapToGlobal(local.toPoint()))
    wheel = QWheelEvent(local, global_point, QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(pin, wheel)
    assert pin.scale_factor == pytest.approx(1.15)
    start = pin.pos()
    global_point = QPointF(pin.mapToGlobal(local.toPoint()))
    press = QMouseEvent(QEvent.Type.MouseButtonPress, local, global_point,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(pin, press)
    delta = QPoint(24, 18)
    move = QMouseEvent(QEvent.Type.MouseMove, local + QPointF(delta), global_point + QPointF(delta),
                       Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(pin, move)
    assert pin.pos() == start + delta
    release = QMouseEvent(QEvent.Type.MouseButtonRelease, local, global_point + QPointF(delta),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(pin, release)
    assert pin._drag_offset is None


def test_context_actions_and_transformations_do_not_modify_source(make_pin, image_path):
    original = hashlib.sha256(image_path.read_bytes()).digest()
    pin = make_pin()
    pin.set_scale(3)
    menu = pin._context_menu()
    menu.actions()[0].trigger()
    assert pin.scale_factor == 1.0
    opacity_menu = next(action.menu() for action in menu.actions() if action.menu())
    next(action for action in opacity_menu.actions() if action.text() == "60%").trigger()
    assert pin.opacity == pytest.approx(0.6)
    pin.fit_to_screen()
    assert hashlib.sha256(image_path.read_bytes()).digest() == original
    menu.deleteLater()


def test_multiple_windows_close_and_release_independently(make_pin, pin_app):
    first, second = make_pin(), make_pin()
    first.show()
    second.show()
    pin_app.processEvents()
    closed = []
    first.closed.connect(closed.append)
    first.close()
    assert closed == [first]
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    pin_app.processEvents()
    assert not shiboken6.isValid(first)
    assert shiboken6.isValid(second) and second.isVisible()
    escape = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(second, escape)
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    pin_app.processEvents()
    assert not shiboken6.isValid(second)


def test_small_image_keeps_close_toolbar_inside_window(make_pin):
    pin = make_pin()
    pin.set_scale(pin.MIN_SCALE)
    pin.toolbar.show()
    pin._position_toolbar()
    assert pin.rect().contains(pin.toolbar.geometry())


def test_context_menu_survives_window_deletion_inside_nested_loop(make_pin, pin_app, monkeypatch):
    pin = make_pin()
    pin.show()
    pin_app.processEvents()
    menu = pin._context_menu()
    assert menu.parentWidget() is None
    monkeypatch.setattr(pin, "_context_menu", lambda: menu)

    def close_inside_loop():
        pin.close()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        menu.close()

    QTimer.singleShot(0, close_inside_loop)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(10, 10), pin.pos() + QPoint(10, 10))
    pin.contextMenuEvent(event)
    assert not shiboken6.isValid(pin)


def test_missing_image_reports_a_read_error(make_pin, tmp_path):
    with pytest.raises(OSError, match="无法打开贴图"):
        make_pin(tmp_path / "missing.png")


def test_copy_click_through_and_state_round_trip(make_pin, pin_app):
    pin = make_pin()
    pin.item_id = "synthetic-key"
    copies = []
    pin.copy_requested.connect(copies.append)
    menu = pin._context_menu()
    next(action for action in menu.actions() if action.text().startswith("复制图片")).trigger()
    assert copies == [pin]
    pin.set_scale(0.5)
    pin.set_opacity(0.7)
    pin.move(140, 160)
    state = pin.state()
    assert state["id"] == "synthetic-key" and state["scale"] == 0.5 and state["opacity"] == 0.7
    changes = []
    pin.click_through_changed.connect(changes.append)
    pin.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pin.show()
    pin_app.processEvents()
    geometry = pin.geometry()
    pin.set_click_through(True)
    pin_app.processEvents()
    assert pin.click_through and pin.isVisible() and changes == [pin]
    assert pin.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert pin.geometry().size() == geometry.size()
    pin.set_click_through(False)
    assert not pin.windowFlags() & Qt.WindowType.WindowTransparentForInput
    restored = make_pin()
    restored.restore_state(state)
    assert restored.scale_factor == 0.5 and restored.opacity == pytest.approx(0.7)
    menu.deleteLater()
