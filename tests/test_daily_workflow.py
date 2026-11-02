"""Direct file-paste/drop routes and reachable pinned-image recovery."""
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QDropEvent, QKeyEvent
from PySide6.QtTest import QTest

from test_recent_panel import app, flush, shelf
from test_recent_lock_security import hello
from test_pinboard import pin_app, image_path, make_pin


def payload(tmp_path):
    path = tmp_path / "synthetic-workflow.txt"
    path.write_text("original bytes", encoding="utf-8")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    return path, mime


@pytest.mark.parametrize("tab", [0, 1, 2])
@pytest.mark.parametrize("surface", ["panel", "recent_list"])
def test_file_drop_opens_basket_without_choosing_a_tab(shelf, tmp_path, tab, surface):
    host, panel, *_ = shelf()
    path, mime = payload(tmp_path)
    panel.tabs.setCurrentIndex(tab)
    widget = panel if surface == "panel" else panel.file_results
    event = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                       mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(event)
    assert event.isAccepted() and event.dropAction() == Qt.DropAction.CopyAction
    assert panel.basket_mode and host.file_basket.count == 1
    assert host.recent_files.entries() == [] and host.vault.query(limit=10) == []
    assert path.read_text(encoding="utf-8") == "original bytes"


@pytest.mark.parametrize("tab", [0, 1, 2])
def test_file_paste_from_any_tab_goes_directly_to_basket(shelf, tmp_path, tab):
    host, panel, clipboard, _ = shelf()
    path, mime = payload(tmp_path)
    clipboard.mimeData = lambda: mime
    panel.tabs.setCurrentIndex(tab)
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert panel.eventFilter(panel.search, event)
    assert panel.basket_mode and host.file_basket.count == 1
    assert Path(panel.basket_entries()[0]["path"]) == path
    assert clipboard.payloads == [] and host.owned_copies == 0


def test_normal_text_paste_keeps_search_behavior_and_does_not_treat_text_as_path(shelf, tmp_path):
    host, panel, clipboard, _ = shelf()
    path, _ = payload(tmp_path)
    mime = QMimeData()
    mime.setText(str(path))
    clipboard.mimeData = lambda: mime
    for tab in (0, 1, 2):
        panel.tabs.setCurrentIndex(tab)
        assert not panel._handle_file_paste()
    panel.paste_basket_files()
    assert host.file_basket.count == 0
    assert "没有文件" in panel.status.text()


def test_locked_file_paste_never_reads_clipboard_or_requests_auth(shelf, hello):
    _host, panel, clipboard, _ = shelf()
    panel.open_basket()
    assert panel.lock_files()
    def forbidden():
        raise AssertionError("Locked paste inspected clipboard")
    clipboard.mimeData = forbidden
    assert panel._handle_file_paste()
    panel.paste_basket_files()
    assert not panel.basket_paste_button.isEnabled()
    assert "verify" not in hello


def test_empty_clipboard_paste_has_a_clear_message(shelf):
    host, panel, clipboard, _ = shelf()
    clipboard.mimeData = lambda: None
    assert not panel._handle_file_paste()
    panel.paste_basket_files()
    assert host.file_basket.count == 0 and "没有文件" in panel.status.text()


def test_ball_click_returns_to_nonempty_basket_without_choosing_tab(shelf, tmp_path):
    host, panel, *_ = shelf()
    path, _ = payload(tmp_path)
    panel.add_basket_paths([path])
    panel.tabs.setCurrentIndex(1)
    panel.collapse()
    panel.open_from_ball()
    assert panel.basket_mode and panel.basket_results.count() == 1
    assert "继续使用中转篮" in panel.ensure_ball().toolTip()


def test_click_through_has_reachable_nontransparent_restore_control(make_pin, pin_app):
    pin = make_pin()
    pin.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pin.show()
    pin.set_click_through(True)
    control = pin._restore_control
    assert control is not None and control.isVisible()
    assert control.parentWidget() is None
    assert not control.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert control.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert "恢复" in control.restore_button.text()
    QTest.mouseClick(control.restore_button, Qt.MouseButton.LeftButton)
    assert not pin.click_through and not control.isVisible()


def test_restore_control_follows_image_stays_on_negative_monitor_and_closes(make_pin, pin_app, monkeypatch):
    pin = make_pin()
    pin.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    area = QRect(-1400, -200, 1400, 900)
    monkeypatch.setattr(pin, "_workarea", lambda: area)
    pin.move(-1100, -190)
    pin.show()
    pin.set_click_through(True)
    control = pin._restore_control
    assert area.contains(control.geometry())
    before = control.pos()
    pin.move(-900, 100)
    pin_app.processEvents()
    assert control.pos() != before and area.contains(control.geometry())
    pin.hide()
    assert not control.isVisible()
    pin.show()
    assert control.isVisible()
    pin.close()
    assert not control.isVisible()


def test_each_transparent_pin_restores_independently(make_pin):
    first, second = make_pin(), make_pin()
    for pin in (first, second):
        pin.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        pin.show()
        pin.set_click_through(True)
    first._restore_control.restore_button.click()
    assert not first.click_through and second.click_through
    assert second._restore_control.isVisible()
