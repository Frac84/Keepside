"""Basket UX with temporary files and in-memory clipboard/drag recorders."""
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent, QMouseEvent
from PySide6.QtTest import QTest

from snapvault import recent_panel
from test_recent_panel import app, flush, shelf
from test_recent_lock_security import hello


def files(tmp_path, count=3):
    values = []
    for index in range(count):
        path = tmp_path / f"Synthetic-basket-{index}.txt"
        path.write_text(f"Synthetic original {index}", encoding="utf-8")
        values.append(path)
    return values


def select(panel, *paths):
    chosen = {str(path) for path in paths}
    panel.basket_results.clearSelection()
    for index in range(panel.basket_results.count()):
        item = panel.basket_results.item(index)
        item.setSelected(item.data(Qt.ItemDataRole.UserRole)["path"] in chosen)


def urls(mime):
    return [Path(url.toLocalFile()) for url in mime.urls()]


def test_basket_is_separate_from_recent_files_and_library(shelf, tmp_path):
    host, panel, _clipboard, _desktop = shelf()
    paths = files(tmp_path)
    host.recent_files.add_paths(paths[:1])
    rows_before = host.vault.query(limit=100)
    recent_before = host.recent_files.entries()
    panel.add_basket_paths(paths[1:])
    assert panel.tabs.tabText(2) == "中转篮" and panel.basket_mode
    assert panel.basket_results.count() == panel.basket_count == 2
    assert host.vault.query(limit=100) == rows_before
    assert host.recent_files.entries() == recent_before
    assert {entry["path"] for entry in panel.basket_entries()} == {str(path) for path in paths[1:]}
    assert not any(entry.get("thumbnail") for entry in panel.basket_entries())


def test_basket_tab_state_does_not_turn_legacy_boolean_into_an_index(shelf):
    host, panel, *_ = shelf()
    panel.tabs.setCurrentIndex(1)
    panel.tabs.setCurrentIndex(2)
    assert host.vault.setting("quick_panel_files_tab") is True
    assert host.vault.setting("quick_panel_tab_v2") == 2
    _other_host, other, *_ = shelf(vault=host.vault)
    assert other.basket_mode
    other.open_from_ball()
    assert other.files_mode, "A normal ball click still opens recent files"


def test_copy_selected_and_copy_all_use_standard_file_urls(shelf, tmp_path):
    host, panel, clipboard, _desktop = shelf()
    paths = files(tmp_path)
    original = {path: path.read_bytes() for path in paths}
    panel.add_basket_paths(paths)
    select(panel, paths[0], paths[2])
    panel.copy_basket()
    assert set(urls(clipboard.payloads[-1])) == {paths[0], paths[2]}
    assert host.owned_copies == 1
    panel.search.setText(paths[0].name)
    panel.refresh()
    assert panel.basket_results.count() == 1
    panel.copy_basket(all_items=True)
    assert urls(clipboard.payloads[-1]) == paths
    assert {path: path.read_bytes() for path in paths} == original
    assert host.file_basket.count == 3


def test_drag_all_ignores_filter_skips_missing_and_never_offers_move(shelf, tmp_path, monkeypatch):
    host, panel, *_ = shelf()
    paths = files(tmp_path)
    panel.add_basket_paths(paths)
    paths[1].unlink()
    panel.search.setText(paths[0].name)
    panel.refresh()
    captured = []

    class Drag:
        def __init__(self, source):
            assert source is panel.basket_drag_all_button
        def setMimeData(self, mime):
            captured.append(urls(mime))
        def exec(self, actions, default):
            assert actions == default == Qt.DropAction.CopyAction
            return Qt.DropAction.CopyAction
        def deleteLater(self):
            pass

    monkeypatch.setattr(recent_panel, "QDrag", Drag)
    panel.start_basket_drag(all_items=True)
    assert captured == [[paths[0], paths[2]]]
    assert "跳过 1" in panel.status.text()
    assert host.file_basket.count == 3
    assert paths[0].exists() and paths[2].exists()


def test_remove_and_clear_only_drop_references(shelf, tmp_path):
    host, panel, *_ = shelf()
    paths = files(tmp_path)
    before = {path: path.read_bytes() for path in paths}
    panel.add_basket_paths(paths)
    select(panel, paths[1])
    panel.remove_basket_entries()
    assert host.file_basket.count == 2
    panel.clear_basket()
    assert host.file_basket.count == 0
    assert {path: path.read_bytes() for path in paths} == before
    assert "未删除" in panel.status.text()


def test_default_open_and_reveal_route_to_basket_selected_file(shelf, tmp_path, monkeypatch):
    _host, panel, *_ = shelf()
    path = files(tmp_path, 1)[0]
    panel.add_basket_paths([path])
    opened, revealed = [], []
    monkeypatch.setattr(recent_panel.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    monkeypatch.setattr(recent_panel, "reveal_file", lambda value: revealed.append(value) or True)
    panel.open_current()
    panel.reveal_current()
    assert [Path(value) for value in opened] == [path] and revealed == [path]


def test_locked_basket_hides_metadata_and_blocks_every_direct_action(shelf, hello, tmp_path, monkeypatch):
    host, panel, clipboard, _desktop = shelf()
    path = files(tmp_path, 1)[0]
    panel.add_basket_paths([path])
    panel.search.setText(path.name)
    panel.refresh()
    assert panel.lock_files()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Locked basket reached an external path or operation")

    for name in ("entries", "existing_paths", "add_paths", "remove", "clear"):
        monkeypatch.setattr(host.file_basket, name, forbidden)
    monkeypatch.setattr(recent_panel.QDesktopServices, "openUrl", forbidden)
    monkeypatch.setattr(recent_panel, "reveal_file", forbidden)
    monkeypatch.setattr(recent_panel, "QDrag", forbidden)
    monkeypatch.setattr(recent_panel.QFileDialog, "getOpenFileNames", forbidden)
    panel.refresh()
    panel.open_basket()
    panel.add_basket_paths([path])
    panel.choose_basket_files()
    panel.copy_basket()
    panel.copy_basket(all_items=True)
    panel.start_basket_drag()
    panel.start_basket_drag(all_items=True)
    panel.open_current()
    panel.reveal_current()
    panel.remove_basket_entries()
    panel.clear_basket()
    assert panel.basket_count == 0 and panel.basket_entries() == []
    assert panel.selected_basket_entries() == [] and panel.basket_results.count() == 0
    assert panel.search.text() == "" and path.name not in panel.status.text()
    assert path.name not in panel.basket_summary.toolTip()
    assert not clipboard.payloads and "verify" not in hello


@pytest.mark.parametrize("actions,accepted", [(Qt.DropAction.CopyAction, True), (Qt.DropAction.MoveAction, False)])
def test_list_drop_requires_copy_action(shelf, tmp_path, actions, accepted):
    host, panel, *_ = shelf()
    path = files(tmp_path, 1)[0]
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    event = QDropEvent(QPointF(3, 3), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    panel.basket_results.dropEvent(event)
    assert event.isAccepted() is accepted
    assert host.file_basket.count == int(accepted)
    if accepted:
        assert event.dropAction() == Qt.DropAction.CopyAction


def test_basket_badge_count_is_private_while_manually_locked(shelf, hello, tmp_path, app):
    host, panel, *_ = shelf()
    panel.add_basket_paths(files(tmp_path, 2))
    assert panel.basket_count == 2
    assert panel.lock_files()
    host.file_basket.changed.emit()
    flush(app)
    assert panel.basket_count == 0 and panel.basket_results.count() == 0
    assert host.file_basket.count == 2


def mouse(kind, x, y, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.LeftButton):
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons,
                       Qt.KeyboardModifier.NoModifier)


def test_drag_all_waits_for_mouse_threshold_and_starts_once(shelf, tmp_path, monkeypatch):
    _host, panel, *_ = shelf()
    panel.add_basket_paths(files(tmp_path, 1))
    button = panel.basket_drag_all_button
    calls = []
    monkeypatch.setattr(panel, "start_basket_drag", lambda all_items=False: calls.append(all_items))
    threshold = recent_panel.QGuiApplication.styleHints().startDragDistance()
    button.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 4, 4, Qt.MouseButton.LeftButton))
    assert calls == [], "Mouse-down alone must not enter OLE drag"
    button.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 4 + threshold - 1, 4))
    assert calls == []
    button.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 4 + threshold + 2, 4))
    assert calls == [True] and not button.isDown()
    button.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 4 + threshold + 8, 4))
    assert calls == [True], "One held gesture must never start a second native drag"
    button.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 4, 4, Qt.MouseButton.LeftButton,
                                   Qt.MouseButton.NoButton))
    assert not button.isDown()


def test_drag_all_click_and_keyboard_explain_without_starting_drag(shelf, tmp_path, monkeypatch):
    _host, panel, *_ = shelf()
    panel.add_basket_paths(files(tmp_path, 1))
    button = panel.basket_drag_all_button
    calls = []
    monkeypatch.setattr(panel, "start_basket_drag", lambda **kwargs: calls.append(kwargs))
    button.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 4, 4, Qt.MouseButton.LeftButton))
    button.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 4, 4, Qt.MouseButton.LeftButton,
                                   Qt.MouseButton.NoButton))
    assert calls == [] and "按住" in panel.status.text()
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert calls == [] and "复制全部" in panel.status.text()


def test_locking_during_a_pending_button_gesture_prevents_drag(shelf, hello, tmp_path, monkeypatch):
    _host, panel, *_ = shelf()
    panel.add_basket_paths(files(tmp_path, 1))
    button = panel.basket_drag_all_button
    calls = []
    monkeypatch.setattr(panel, "start_basket_drag", lambda **kwargs: calls.append(kwargs))
    button.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 4, 4, Qt.MouseButton.LeftButton))
    assert panel.lock_files()
    button.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 100, 4))
    button.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 100, 4, Qt.MouseButton.LeftButton,
                                   Qt.MouseButton.NoButton))
    assert not button.isDown() and calls == []


def test_queued_refresh_keeps_drag_widgets_visible_until_native_drag_finishes(shelf, tmp_path):
    _host, panel, *_ = shelf()
    panel.add_basket_paths(files(tmp_path, 1))
    panel.open_basket()
    assert not panel.basket_results.isHidden() and not panel.basket_toolbar.isHidden()
    panel._dragging = True
    try:
        panel.refresh()
        assert not panel.basket_results.isHidden() and not panel.basket_toolbar.isHidden()
    finally:
        panel._dragging = False
