"""Exercise the actual shelf with isolated files and mocked desktop services."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
import pytest
from shiboken6 import isValid

from snapvault import floating_ball, hotkey, quickpaste, recent_panel
from snapvault.recent_files import RecentFiles
from snapvault.storage import Vault


class RecordingClipboard:
    def __init__(self):
        self.payloads = []

    def setMimeData(self, mime):
        self.payloads.append(mime)


class FakeHost(QWidget):
    def __init__(self, vault):
        super().__init__()
        self.vault = vault
        self.quick_autopaste = True
        self.copied_rows = []
        self.pinned_rows = []
        self.owned_copies = 0
        self.recent_files = RecentFiles(vault, self, directories=[], watch=False)

    def _copy_owned(self, operation):
        self.owned_copies += 1
        operation()

    def copy_row(self, row):
        self.copied_rows.append(row)
        return True

    def pin_row(self, row):
        self.pinned_rows.append(row)


def flush(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wait_for(app, condition, timeout=2):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("Expected shelf operation did not complete")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shelf(app, tmp_path, monkeypatch):
    clipboard = RecordingClipboard()
    desktop = []

    class IsolatedApplication:
        @staticmethod
        def clipboard():
            return clipboard

    monkeypatch.setattr(recent_panel, "QApplication", IsolatedApplication)
    monkeypatch.setattr(recent_panel, "apply_glass", lambda *_args: dict(
        enabled=False, method="fallback", reason="isolated test"))
    monkeypatch.setattr(recent_panel, "clamp_position", lambda point, _size: QPoint(point))
    monkeypatch.setattr(floating_ball, "clamp_position", lambda point, _size: QPoint(point))
    # quickpaste and recent_panel share this module. No real global key or
    # foreground-window inspection can escape these test replacements.
    assert quickpaste.hotkey is hotkey and recent_panel.hotkey is hotkey
    monkeypatch.setattr(hotkey, "foreground_window", lambda: 4242)
    monkeypatch.setattr(hotkey, "focus_window", lambda value: desktop.append(("focus", value)) or True)
    monkeypatch.setattr(hotkey, "send_paste", lambda: desktop.append("paste"))
    monkeypatch.setattr(recent_panel, "reveal_file", lambda _path: (_ for _ in ()).throw(AssertionError("unexpected folder UI")))
    monkeypatch.setattr(recent_panel.QDesktopServices, "openUrl", lambda _url: (_ for _ in ()).throw(AssertionError("unexpected file launch")))
    panels = []

    def make(vault=None, default_source=False):
        host = FakeHost(vault or Vault(tmp_path / f"vault-{len(panels)}"))
        # Retain the content-tab starting premise of the original regressions;
        # production still defaults to the newly introduced recent-file tab.
        host.vault.set_setting("quick_panel_files_tab", False)
        host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        panel = recent_panel.ShelfPanel(host)
        # These legacy regression cases explicitly inspect the merged view.
        # Production starts with Windows recently opened files only.
        if not default_source:
            panel.source_filter.setCurrentIndex(panel.source_filter.findData("all"))
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        panel.keep_open.setChecked(True)
        panels.append((host, panel))
        return host, panel, clipboard, desktop

    yield make
    for host, panel in panels:
        if isValid(panel):
            panel.shutdown()
            panel.deleteLater()
        if isValid(host):
            host.recent_files.stop()
            host.deleteLater()
    flush(app)


def listed(panel):
    return [panel.file_results.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(panel.file_results.count())]


def select(panel, *keys):
    panel.file_results.clearSelection()
    for index in range(panel.file_results.count()):
        item = panel.file_results.item(index)
        if item.data(Qt.ItemDataRole.UserRole)["key"] in keys:
            item.setSelected(True)


def shot(vault, text, when, color="red"):
    key, _ = vault.add(Image.new("RGB", (30, 20), color), when)
    vault.finish_ocr(key, text)
    return key


def test_recent_files_merge_with_screenshots_and_pins_take_priority(shelf, tmp_path):
    host, panel, _clipboard, _desktop = shelf()
    now = datetime.now(timezone.utc)
    old = shot(host.vault, "旧截图", now - timedelta(days=1))
    newest = shot(host.vault, "新截图", now + timedelta(hours=1), "blue")
    external = tmp_path / "刚下载的资料.pdf"
    external.write_bytes(b"synthetic pdf")
    assert host.recent_files.add_paths([external]) == 1
    panel.tabs.setCurrentIndex(1)
    assert [row["name"] for row in listed(panel)] == ["新截图.png", external.name, "旧截图.png"]
    select(panel, "vault:" + old)
    panel.toggle_pin()
    assert listed(panel)[0]["key"] == "vault:" + old
    external_key = next(row["key"] for row in listed(panel) if row["path"] == str(external))
    select(panel, external_key)
    panel.toggle_pin()
    entries = listed(panel)
    assert entries[0]["key"] == external_key and entries[0]["pinned"]
    assert entries[1]["key"] == "vault:" + old and entries[1]["pinned"]
    assert entries[2]["key"] == "vault:" + newest


def test_remove_only_hides_external_record_and_keeps_library_screenshot(shelf, tmp_path):
    host, panel, _clipboard, _desktop = shelf()
    key = shot(host.vault, "要移出的截图", datetime.now(timezone.utc))
    original = host.vault.images / host.vault.get(key)["filename"]
    external = tmp_path / "保留原文件.txt"
    external.write_text("keep this file", encoding="utf-8")
    host.recent_files.add_paths([external])
    panel.tabs.setCurrentIndex(1)
    select(panel, *(row["key"] for row in listed(panel)))
    panel.remove_entries()
    assert listed(panel) == []
    assert original.is_file() and host.vault.get(key) is not None
    assert external.read_text(encoding="utf-8") == "keep this file"
    panel.tabs.setCurrentIndex(0)
    assert panel.results.count() == 1 and panel.current_id() == key


def test_filename_search_filters_external_files_and_screenshots(shelf, tmp_path):
    host, panel, _clipboard, _desktop = shelf()
    shot(host.vault, "学校预算", datetime.now(timezone.utc))
    first, second = tmp_path / "学校 report.pdf", tmp_path / "unrelated.zip"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    host.recent_files.add_paths([first, second])
    panel.tabs.setCurrentIndex(1)
    panel.search.setText("学校")
    panel.refresh()
    assert {row["name"] for row in listed(panel)} == {first.name, "学校预算.png"}
    panel.search.setText("REPORT")
    panel.refresh()
    assert [row["name"] for row in listed(panel)] == [first.name]


def test_content_tab_keeps_relevance_search_copy_and_paste(shelf, app):
    host, panel, clipboard, desktop = shelf()
    now = datetime.now(timezone.utc)
    host.vault.add_text("会议纪要\n下午讨论预算", now)
    best, _ = host.vault.add_text("预算表\n第二行", now - timedelta(minutes=5))
    host.vault.add_text("别的内容", now + timedelta(minutes=1))
    panel.open(files=False)
    assert panel.target == 4242
    panel.search.setText("预算")
    panel.refresh()
    assert panel.results.count() == 2 and panel.current_id() == best
    panel.accept_current(paste=False)
    assert host.copied_rows[-1]["id"] == best and desktop == []
    assert clipboard.payloads == []
    panel.open(files=False)
    panel.search.setText("预算")
    panel.refresh()
    panel.accept_current()
    wait_for(app, lambda: desktop == [("focus", 4242), "paste"])
    assert host.copied_rows[-1]["id"] == best


def test_copy_external_file_uses_owned_mime_without_real_clipboard(shelf, tmp_path):
    host, panel, clipboard, desktop = shelf()
    file = tmp_path / "中文 文件.pdf"
    file.write_bytes(b"synthetic")
    host.recent_files.add_paths([file])
    panel.tabs.setCurrentIndex(1)
    panel.accept_current(paste=False)
    assert host.owned_copies == 1 and len(clipboard.payloads) == 1
    assert [Path(url.toLocalFile()) for url in clipboard.payloads[0].urls()] == [file]
    assert desktop == [] and file.read_bytes() == b"synthetic"


def test_multi_file_drag_uses_copy_action_and_survives_focus_loss(shelf, tmp_path, monkeypatch):
    host, panel, clipboard, desktop = shelf()
    files = [tmp_path / "first.pdf", tmp_path / "第二 个.txt"]
    for file in files:
        file.write_bytes(b"synthetic")
    host.recent_files.add_paths(files)
    panel.tabs.setCurrentIndex(1)
    panel.show()
    panel.keep_open.setChecked(False)
    select(panel, *(row["key"] for row in listed(panel)))
    monkeypatch.setattr(panel, "isActiveWindow", lambda: False)
    calls = []

    class Drag:
        def __init__(self, parent):
            assert parent is panel.file_results

        def setMimeData(self, mime):
            self.mime = mime

        def setPixmap(self, _pixmap):
            pass

        def exec(self, supported, default):
            calls.append((supported, default))
            assert panel._dragging
            panel._hide_if_inactive()
            assert panel.isVisible(), "losing focus during drag must not close the shelf"
            assert {Path(url.toLocalFile()) for url in self.mime.urls()} == set(files)
            assert all(url.isLocalFile() for url in self.mime.urls())
            return Qt.DropAction.CopyAction

        def deleteLater(self):
            calls.append("deleted")

    monkeypatch.setattr(recent_panel, "QDrag", Drag)
    panel.start_file_drag()
    assert calls == [(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction), "deleted"]
    assert not panel.isVisible() and not panel._dragging
    assert clipboard.payloads == [] and desktop == []
    assert all(file.read_bytes() == b"synthetic" for file in files)


def test_keep_open_preserves_panel_after_successful_drag_and_focus_loss(shelf, tmp_path, monkeypatch):
    host, panel, _clipboard, _desktop = shelf()
    file = tmp_path / "keep.pdf"
    file.write_bytes(b"keep")
    host.recent_files.add_paths([file])
    panel.tabs.setCurrentIndex(1)
    panel.show()
    monkeypatch.setattr(panel, "isActiveWindow", lambda: False)

    class Drag:
        def __init__(self, _parent):
            pass

        def setMimeData(self, _mime):
            pass

        def setPixmap(self, _pixmap):
            pass

        def exec(self, *_args):
            return Qt.DropAction.CopyAction

        def deleteLater(self):
            pass

    monkeypatch.setattr(recent_panel, "QDrag", Drag)
    panel.start_file_drag()
    panel._hide_if_inactive()
    assert panel.isVisible()
    panel.keep_open.setChecked(False)
    panel._hide_if_inactive()
    assert not panel.isVisible()


def test_missing_source_never_copies_or_pastes(shelf, tmp_path, monkeypatch):
    host, panel, clipboard, desktop = shelf()
    file = tmp_path / "deleted.pdf"
    file.write_bytes(b"deleted externally")
    host.recent_files.add_paths([file])
    panel.tabs.setCurrentIndex(1)
    panel.target = 4242
    # Keep the cached visible entry, then remove the file outside the service.
    file.unlink()
    monkeypatch.setattr(recent_panel, "QDrag", lambda *_: (_ for _ in ()).throw(AssertionError("drag must not start")))
    panel.accept_current()
    assert "无法复制" in panel.status.text()
    panel.start_file_drag()
    assert "无法拖出" in panel.status.text()
    assert clipboard.payloads == [] and desktop == [] and host.owned_copies == 0


def test_screenshot_copy_prepares_real_stable_snapshot_from_latest_row(shelf):
    host, panel, clipboard, desktop = shelf()
    key = shot(host.vault, "最初标题", datetime.now(timezone.utc))
    panel.tabs.setCurrentIndex(1)
    select(panel, "vault:" + key)
    old = host.vault.images / host.vault.get(key)["filename"]
    host.vault.finish_ocr(key, "后台更新的标题")
    assert not old.exists()
    original = host.vault.images / host.vault.get(key)["filename"]
    content = original.read_bytes()
    panel.accept_current(paste=False)
    exported = Path(clipboard.payloads[-1].urls()[0].toLocalFile())
    assert "后台更新的标题" in exported.name
    assert exported.parent.parent == host.vault.root / "drag-cache"
    host.vault.delete(key)
    assert exported.read_bytes() == content and desktop == []


def test_drop_paths_only_accepts_local_file_urls(shelf, tmp_path):
    _host, panel, _clipboard, _desktop = shelf()
    file = tmp_path / "local.txt"
    file.write_text("local", encoding="utf-8")
    mime = QMimeData()
    mime.setUrls([QUrl("https://example.com/file.txt"), QUrl.fromLocalFile(str(file))])
    assert [Path(path) for path in panel.drop_paths(mime)] == [file]


def test_pinned_older_screenshot_remains_searchable_by_ocr_content(shelf):
    host, panel, _clipboard, _desktop = shelf()
    now = datetime.now(timezone.utc)
    old = shot(host.vault, "独立标题\nneedle 搜索命中的正文", now - timedelta(days=1))
    # The normal query only yields 80 recent matches; a pinned old match must
    # still be available when the search term occurs in OCR rather than title.
    for index in range(81):
        host.vault.add(Image.new("RGB", (30 + index, 20), "blue"), now + timedelta(seconds=index))
        key = host.vault.query(limit=1)[0]["id"]
        host.vault.finish_ocr(key, "新截图 needle")
    panel._pinned_shots.add(old)
    panel.tabs.setCurrentIndex(1)
    panel.search.setText("needle")
    panel.refresh()
    assert listed(panel)[0]["key"] == "vault:" + old


def test_title_drag_saves_position_and_next_open_restores_it(shelf):
    host, panel, _clipboard, _desktop = shelf()
    panel.move(150, 170)
    panel.begin_window_drag(QPoint(250, 270))
    panel.end_window_drag()
    assert host.vault.setting("shelf_panel_position") is None
    panel.begin_window_drag(QPoint(250, 270))
    panel.move_window_drag(QPoint(340, 320))
    panel.end_window_drag()
    assert panel.pos() == QPoint(240, 220)
    assert host.vault.setting("shelf_panel_position") == [240, 220]
    panel.move(20, 20)
    panel.open()
    assert panel.pos() == QPoint(240, 220)


def test_collapse_shows_ball_and_click_reopens_recent_files(shelf):
    host, panel, _clipboard, _desktop = shelf()
    panel.open(files=False)
    panel.collapse()
    ball = panel._orb
    assert not panel.isVisible() and ball.isVisible()
    assert ball.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    assert panel.results.count() == panel.file_results.count() == 0
    assert host.vault.setting("shelf_ball_position") == [ball.x(), ball.y()]
    ball.click()
    assert panel.isVisible() and panel.files_mode and not ball.isVisible()


def mouse_event(kind, local, global_point, button, buttons):
    return QMouseEvent(kind, QPointF(local), QPointF(global_point), button, buttons,
                       Qt.KeyboardModifier.NoModifier)


def test_ball_drag_persists_position_without_opening_panel(shelf):
    host, panel, _clipboard, _desktop = shelf()
    panel.show_ball()
    ball = panel._orb
    ball.move(120, 160)
    press, release = QPoint(150, 190), QPoint(230, 240)
    ball.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, QPoint(30, 30), press,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
    ball.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, QPoint(30, 30), release,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
    ball.mouseReleaseEvent(mouse_event(QEvent.Type.MouseButtonRelease, QPoint(30, 30), release,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
    assert ball.pos() == QPoint(200, 210)
    assert host.vault.setting("shelf_ball_position") == [200, 210]
    assert ball.isVisible() and not panel.isVisible()
    _host, replacement, _clipboard, _desktop = shelf(host.vault)
    replacement.show_ball()
    assert replacement._orb.pos() == QPoint(200, 210)


def test_escape_from_search_collapses_to_ball(shelf):
    _host, panel, _clipboard, _desktop = shelf()
    panel.open(files=True)
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    assert panel.eventFilter(panel.search, event)
    assert not panel.isVisible() and panel._orb.isVisible()


@pytest.mark.parametrize("ball_first", [False, True])
def test_capture_temporarily_hides_shelf_surfaces_then_restores(shelf, ball_first):
    _host, panel, _clipboard, _desktop = shelf()
    panel.open(files=True)
    if ball_first:
        panel.collapse()
    before = (panel.isVisible(), panel._orb is not None and panel._orb.isVisible())
    state = panel.suspend_for_capture()
    assert state == before
    assert not panel.isVisible() and (panel._orb is None or not panel._orb.isVisible())
    panel.show_ball()
    assert panel._orb is None or not panel._orb.isVisible()
    panel.resume_after_capture(state)
    assert (panel.isVisible(), panel._orb is not None and panel._orb.isVisible()) == before


def test_disabling_ball_prevents_collapse_from_showing_it(shelf):
    host, panel, _clipboard, _desktop = shelf()
    panel.open(files=True)
    panel.hide_ball(permanent=True)
    panel.collapse()
    assert host.vault.setting("shelf_ball_enabled") is False
    assert not panel.isVisible() and not panel._orb.isVisible()
    panel.show_ball(enable=True)
    assert panel._orb.isVisible() and host.vault.setting("shelf_ball_enabled") is True


def test_shutdown_hides_and_deletes_ball_without_reappearing(shelf, app):
    _host, panel, _clipboard, _desktop = shelf()
    panel.open(files=True)
    panel.collapse()
    ball = panel._orb
    destroyed = []
    ball.destroyed.connect(lambda *_args: destroyed.append(True))
    panel.shutdown()
    assert not panel.isVisible() and not ball.isVisible()
    panel.show_ball()
    assert panel._orb is None
    flush(app)
    assert destroyed == [True] and not isValid(ball)


def test_destroying_panel_deletes_its_ownerless_ball(shelf, app):
    _host, panel, _clipboard, _desktop = shelf()
    panel.show_ball()
    ball = panel._orb
    panel.deleteLater()
    flush(app)
    flush(app)
    assert not isValid(panel) and not isValid(ball)


def test_system_recent_filter_uses_origin_not_display_source(shelf, tmp_path, monkeypatch):
    host, panel, _clipboard, _desktop = shelf()
    screenshot = shot(host.vault, "最近截图", datetime.now(timezone.utc))
    files = [tmp_path / "system.txt", tmp_path / "manual.txt"]
    for file in files:
        file.write_text("synthetic", encoding="utf-8")
    entries = [dict(key=str(index), path=str(file), name=file.name, source="最近使用",
                    origin=origin, seen_at=time.time() + index, pinned=False, exists=True)
               for index, (file, origin) in enumerate(zip(files, ("system_recent", "manual")))]
    monkeypatch.setattr(host.recent_files, "entries", lambda *_args, **_kwargs: [dict(row) for row in entries])
    panel.tabs.setCurrentIndex(1)
    panel.source_filter.setCurrentIndex(panel.source_filter.findData("system"))
    panel.refresh()
    assert [row["key"] for row in listed(panel)] == ["0"]
    panel.source_filter.setCurrentIndex(panel.source_filter.findData("external"))
    assert [row["key"] for row in listed(panel)] == ["1"]
    panel.source_filter.setCurrentIndex(panel.source_filter.findData("screenshots"))
    assert [row["key"] for row in listed(panel)] == ["vault:" + screenshot]


def test_shelf_appearance_is_independent_and_persisted(shelf):
    host, panel, _clipboard, _desktop = shelf()
    host.vault.set_setting("theme", "paper")
    panel.set_appearance(False)
    assert not panel.shelf_dark and host.vault.setting("shelf_appearance") == "light"
    panel.set_appearance(True)
    assert panel.shelf_dark and host.vault.setting("shelf_appearance") == "dark"
    assert host.vault.setting("theme") == "paper"


def test_default_view_only_lists_recently_opened_files_without_querying_library(shelf, tmp_path, monkeypatch):
    host, panel, _clipboard, _desktop = shelf(default_source=True)
    assert panel.source_filter.currentData() == "system"
    path = tmp_path / "刚打开的视频.mp4"
    path.write_bytes(b"synthetic video")
    manual = tmp_path / "手动添加.txt"
    manual.write_text("synthetic", encoding="utf-8")
    entries = [dict(key=str(index), path=str(file), name=file.name, source="最近使用",
                    origin=origin, seen_at=time.time() + index, pinned=False, exists=True)
               for index, (file, origin) in enumerate(zip((path, manual), ("system_recent", "manual")))]
    monkeypatch.setattr(host.recent_files, "entries", lambda *_args, **_kwargs: [dict(row) for row in entries])
    monkeypatch.setattr(host.vault, "query", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recent view must not query screenshots")))
    monkeypatch.setattr(host.vault, "rows_by_ids", lambda *_args: (_ for _ in ()).throw(AssertionError("recent view must not query screenshots")))
    panel.tabs.setCurrentIndex(1)
    assert [row["path"] for row in listed(panel)] == [str(path)]


@pytest.mark.parametrize("activation", ["double_click", "search_enter", "list_enter", "button"])
def test_open_video_uses_original_url_once_without_copying_or_pasting(shelf, tmp_path, monkeypatch, activation):
    host, panel, clipboard, desktop = shelf()
    path = tmp_path / "中文 #100% & 视频.mp4"
    path.write_bytes(b"synthetic video")
    host.recent_files.add_paths([path])
    panel.tabs.setCurrentIndex(1)
    calls = []
    monkeypatch.setattr(recent_panel.QDesktopServices, "openUrl", lambda url: calls.append(url) or True)
    if activation == "double_click":
        panel.file_results.itemActivated.emit(panel.file_results.currentItem())
    elif activation in ("search_enter", "list_enter"):
        target = panel.search if activation == "search_enter" else panel.file_results
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        assert panel.eventFilter(target, event)
    else:
        panel.open_button.click()
    assert len(calls) == 1 and calls[0].isLocalFile()
    assert Path(calls[0].toLocalFile()) == path
    assert clipboard.payloads == [] and desktop == [] and host.owned_copies == 0
    assert not (host.vault.root / "drag-cache").exists()


def test_recent_file_shortcuts_locate_and_copy_explicitly(shelf, tmp_path, monkeypatch):
    host, panel, clipboard, desktop = shelf()
    path = tmp_path / "文件.txt"
    path.write_text("synthetic", encoding="utf-8")
    host.recent_files.add_paths([path])
    panel.tabs.setCurrentIndex(1)
    located = []
    monkeypatch.setattr(recent_panel, "reveal_file", lambda value: located.append(Path(value)) or True)
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.AltModifier)
    assert panel.eventFilter(panel.search, event)
    assert located == [path] and clipboard.payloads == []
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert panel.eventFilter(panel.file_results, event)
    assert Path(clipboard.payloads[0].urls()[0].toLocalFile()) == path
    assert desktop == []


def test_external_images_do_not_decode_or_create_thumbnail_cache(shelf, tmp_path, monkeypatch):
    host, panel, _clipboard, _desktop = shelf()
    files = [tmp_path / name for name in ("large.tiff", "large.png", "large.mp4")]
    for path in files:
        path.write_bytes(b"metadata only")
    host.recent_files.add_paths(files)
    monkeypatch.setattr(recent_panel, "QImageReader", lambda *_args: (_ for _ in ()).throw(AssertionError("external body must not be decoded")))
    panel.tabs.setCurrentIndex(1)
    panel.refresh()
    assert len(listed(panel)) == 3
    assert not (host.vault.root / "drag-cache").exists()


def test_open_and_locate_resolve_latest_screenshot_name_without_snapshot(shelf, monkeypatch):
    host, panel, clipboard, desktop = shelf()
    key = shot(host.vault, "初始名称", datetime.now(timezone.utc))
    panel.tabs.setCurrentIndex(1)
    select(panel, "vault:" + key)
    host.vault.finish_ocr(key, "更新后的名称")
    path = host.vault.images / host.vault.get(key)["filename"]
    opened, located = [], []
    monkeypatch.setattr(recent_panel.QDesktopServices, "openUrl", lambda url: opened.append(Path(url.toLocalFile())) or True)
    monkeypatch.setattr(recent_panel, "reveal_file", lambda value: located.append(Path(value)) or True)
    panel.open_current()
    panel.reveal_current()
    assert opened == located == [path]
    assert clipboard.payloads == [] and desktop == []
    assert not (host.vault.root / "drag-cache").exists()


def test_missing_or_multiple_files_never_launch_and_shell_failure_is_visible(shelf, tmp_path, monkeypatch):
    host, panel, _clipboard, _desktop = shelf()
    paths = [tmp_path / name for name in ("one.mp4", "two.txt")]
    for path in paths:
        path.write_bytes(b"synthetic")
    host.recent_files.add_paths(paths)
    panel.tabs.setCurrentIndex(1)
    select(panel, *(row["key"] for row in listed(panel)))
    assert not panel.open_button.isEnabled() and not panel.reveal_button.isEnabled()
    panel.open_current()
    assert "请选择一个文件" in panel.status.text()
    row = next(row for row in listed(panel) if row["path"] == str(paths[0]))
    select(panel, row["key"])
    paths[0].unlink()
    panel.open_current()
    assert "无法打开" in panel.status.text()
    paths[0].write_bytes(b"synthetic")
    monkeypatch.setattr(recent_panel.QDesktopServices, "openUrl", lambda _url: False)
    panel.open_current()
    assert "默认应用" in panel.status.text()
    monkeypatch.setattr(recent_panel, "reveal_file", lambda _value: False)
    panel.reveal_current()
    assert "无法定位" in panel.status.text()


def test_open_collapse_preserves_ball_anchor_and_dock_setting(shelf):
    host, panel, _clipboard, _desktop = shelf()
    panel.show_ball()
    ball = panel._orb
    ball.set_position(QPoint(200, 210), snap=False)
    panel.save_ball_position()
    anchor = ball.persistent_position()
    panel.open_from_ball()
    panel.move(450, 200)
    panel.collapse()
    assert ball.persistent_position() == anchor
    assert host.vault.setting("shelf_ball_position") == [anchor.x(), anchor.y()]
    assert host.vault.setting("shelf_ball_dock_edge") == ball.dock_edge
