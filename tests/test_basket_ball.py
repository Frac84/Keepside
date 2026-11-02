"""Basket routes and badges use synthetic Qt events, never desktop input."""
from types import SimpleNamespace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QMimeData, QUrl, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QPushButton

from snapvault.floating_ball import FloatingBall


class Basket(QObject):
    changed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.count = 2


class Panel(QObject):
    def __init__(self):
        super().__init__()
        self.host = SimpleNamespace(file_basket=Basket(self))
        self.files_locked = False
        self.shelf_dark = True
        self._interaction_depth = 0
        self.lock_button = QPushButton()
        self.lock_button.setCheckable(True)
        self.added, self.opened = [], []
        self.recent_opens = 0

    @property
    def basket_count(self):
        return 0 if self.files_locked else self.host.file_basket.count

    @staticmethod
    def drop_paths(mime):
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def add_basket_paths(self, paths):
        self.added.extend(paths)
        self.host.file_basket.count += len(paths)
        self.host.file_basket.changed.emit()

    def open_basket(self, from_ball=False):
        self.opened.append(from_ball)

    def open_from_ball(self):
        self.recent_opens += 1

    def save_ball_position(self):
        pass


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def fixture(app):
    panel = Panel()
    ball = FloatingBall(panel)
    ball.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    yield ball, panel
    ball.hide()
    ball.deleteLater()
    panel.lock_button.deleteLater()
    panel.deleteLater()
    app.processEvents()


def payload(tmp_path):
    source = tmp_path / 'synthetic-file.txt'
    source.write_text('original bytes', encoding='utf-8')
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    return source, mime


def test_badge_tracks_model_without_file_metadata_reads(fixture):
    ball, panel = fixture
    assert ball.acceptDrops() and ball.basket_count == 2
    panel.host.file_basket.count = 123
    panel.host.file_basket.changed.emit()
    assert ball.basket_count == 123
    assert '123' in ball.toolTip()
    panel.host.file_basket.count = 0
    panel.host.file_basket.changed.emit()
    assert ball.basket_count == 0


def test_manual_lock_hides_badge_and_count_tooltip(fixture):
    ball, panel = fixture
    panel.files_locked = True
    panel.lock_button.setChecked(True)
    assert ball.basket_count == 0
    assert '暂存 2' not in ball.toolTip() and '已锁定' in ball.toolTip()
    panel.host.file_basket.count = 7
    panel.host.file_basket.changed.emit()
    assert ball.basket_count == 0
    panel.files_locked = False
    panel.lock_button.setChecked(False)
    assert ball.basket_count == 7


def test_drop_routes_to_basket_copy_only_and_keeps_original(fixture, tmp_path):
    ball, panel = fixture
    source, mime = payload(tmp_path)
    event = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                       mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    ball.dropEvent(event)
    assert event.isAccepted() and event.dropAction() == Qt.DropAction.CopyAction
    assert [Path(path) for path in panel.added] == [source] and panel.opened == [True]
    assert not panel.recent_opens and ball.basket_count == 3
    assert source.read_text(encoding='utf-8') == 'original bytes'


@pytest.mark.parametrize('locked,actions', [(True, Qt.DropAction.CopyAction), (False, Qt.DropAction.MoveAction)])
def test_locked_or_move_only_drop_is_rejected(fixture, tmp_path, locked, actions):
    ball, panel = fixture
    panel.files_locked = locked
    source, mime = payload(tmp_path)
    enter = QDragEnterEvent(QPoint(20, 20), actions, mime, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
    ball.dragEnterEvent(enter)
    assert not enter.isAccepted()
    drop = QDropEvent(QPointF(20, 20), actions, mime, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    ball.dropEvent(drop)
    assert not drop.isAccepted() and not panel.added and not panel.opened
    assert source.exists()


def test_drag_hover_uses_copy_cursor_and_remains_revealed(fixture, tmp_path):
    ball, _panel = fixture
    _source, mime = payload(tmp_path)
    enter = QDragEnterEvent(QPoint(20, 20), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                            mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    ball.dragEnterEvent(enter)
    assert enter.isAccepted() and enter.dropAction() == Qt.DropAction.CopyAction
    assert ball._drop_hover
    assert not ball._hide_timer.isActive()
