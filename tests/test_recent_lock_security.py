"""Manual recent-file lock boundaries using only isolated files and fake Hello.

No system authentication dialog, user clipboard, global hotkey, external file,
or running application is touched by these tests.
"""
import threading

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtTest import QTest
import pytest

from snapvault import recent_panel
from test_recent_panel import app, flush, shelf, wait_for


@pytest.fixture
def hello(monkeypatch):
    calls = []

    def availability():
        calls.append("availability")
        return recent_panel.windows_hello.Availability(True, "available", "")

    def verify(*_args, **_kwargs):
        calls.append("verify")
        return recent_panel.windows_hello.Verification(False, "canceled", "已取消验证")

    monkeypatch.setattr(recent_panel.windows_hello, "availability", availability)
    monkeypatch.setattr(recent_panel.windows_hello, "verify", verify)
    return calls


def private_record(host, panel, tmp_path):
    file = tmp_path / "Synthetic-Private-Record.txt"
    file.write_text("test-only", encoding="utf-8")
    assert host.recent_files.add_paths([file]) == 1
    panel.tabs.setCurrentIndex(1)
    panel.refresh()
    assert panel.file_results.count() == 1
    return file


def forbidden(label):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"Locked recent files reached {label}")
    return fail


def test_default_is_unlocked_and_opening_never_requests_authentication(shelf, hello):
    host, panel, _clipboard, _desktop = shelf()
    assert not panel.files_locked
    assert host.vault.setting("recent_files_locked_v1", False) is False
    panel.open(files=True)
    panel.collapse()
    panel.open_from_ball()
    assert not panel.files_locked
    assert "verify" not in hello


def test_manual_lock_clears_existing_sensitive_widgets_even_during_interaction(
        shelf, hello, tmp_path):
    host, panel, _clipboard, _desktop = shelf()
    file = private_record(host, panel, tmp_path)
    panel.search.setText(file.stem)
    panel.refresh()
    assert panel.file_results.count() == 1 and panel._icons
    panel.summary.setToolTip(str(file))
    panel.status.setText(str(file))
    panel._interaction_depth = 1
    try:
        assert panel.lock_files()
        assert panel.files_locked
        assert host.vault.setting("recent_files_locked_v1") is True
        assert panel.file_results.count() == 0
        assert panel.selected_entries() == []
        assert panel.file_entries() == []
        assert not panel._icons
        assert panel.search.text() == ""
        assert file.name not in panel.status.text()
        assert str(file) not in panel.summary.toolTip()
        assert "verify" not in hello
    finally:
        panel._interaction_depth = 0


def test_persisted_lock_survives_panel_recreation_without_prompt(shelf, hello, tmp_path):
    host, panel, _clipboard, _desktop = shelf()
    file = private_record(host, panel, tmp_path)
    assert panel.lock_files()
    other_host, other, _clipboard, _desktop = shelf(vault=host.vault)
    other.open(files=True)
    assert other.files_locked
    assert other.file_results.count() == 0
    assert other.file_entries() == []
    assert other_host.vault.setting("recent_files_locked_v1") is True
    assert file.exists()
    assert "verify" not in hello


def test_locked_refresh_does_not_query_paths_or_decode_icons(shelf, hello, monkeypatch, app):
    host, panel, _clipboard, _desktop = shelf()
    panel.tabs.setCurrentIndex(1)
    assert panel.lock_files()
    monkeypatch.setattr(host.recent_files, "entries", forbidden("backend entries"))
    monkeypatch.setattr(panel, "file_icon", forbidden("icon decoder"))
    panel.open(files=True)
    panel.refresh()
    host.recent_files.changed.emit()
    panel.timer.timeout.emit()
    flush(app)
    assert panel.file_entries() == []
    assert panel.file_results.count() == 0


def test_locked_direct_actions_cannot_open_export_modify_or_drag(
        shelf, hello, tmp_path, monkeypatch):
    host, panel, clipboard, desktop = shelf()
    file = private_record(host, panel, tmp_path)
    assert panel.lock_files()
    monkeypatch.setattr(recent_panel.QDesktopServices, "openUrl", forbidden("file open"))
    monkeypatch.setattr(recent_panel, "reveal_file", forbidden("folder reveal"))
    monkeypatch.setattr(recent_panel, "QDrag", forbidden("drag construction"))
    monkeypatch.setattr(host.recent_files, "set_pinned", forbidden("pin mutation"))
    monkeypatch.setattr(host.recent_files, "remove", forbidden("record removal"))
    monkeypatch.setattr(host.recent_files, "add_paths", forbidden("file import"))
    monkeypatch.setattr(recent_panel.QFileDialog, "getOpenFileNames", forbidden("file picker"))
    monkeypatch.setattr(recent_panel.QFileDialog, "getExistingDirectory", forbidden("folder picker"))
    panel.open_current()
    panel.accept_current(paste=False)
    panel.accept_current(paste=True)
    panel.start_file_drag()
    panel.reveal_current()
    panel.toggle_pin()
    panel.remove_entries()
    panel.add_paths([file])
    panel.choose_files()
    panel.choose_folder()
    panel.file_menu(QPoint(0, 0))
    assert clipboard.payloads == []
    assert desktop == []
    assert host.owned_copies == 0
    assert file.read_text(encoding="utf-8") == "test-only"


def test_locked_source_menu_does_not_expose_watched_folder_even_on_content_tab(
        shelf, hello, tmp_path, monkeypatch):
    host, panel, _clipboard, _desktop = shelf()
    watched = tmp_path / "Synthetic-Private-Watched-Folder"
    watched.mkdir()
    host.recent_files.set_folders([watched])
    assert panel.lock_files()
    panel.tabs.setCurrentIndex(0)
    menus = []

    def inspect_menu(menu, _point):
        def names(item):
            values = []
            for action in item.actions():
                values.append(action.text())
                if action.menu() is not None:
                    values.extend(names(action.menu()))
            return values
        menus.extend(names(menu))
        menu.deleteLater()

    monkeypatch.setattr(panel, "run_menu", inspect_menu)
    panel.source_menu()
    assert str(watched) not in "\n".join(menus)
    assert watched.name not in "\n".join(menus)
    assert "verify" not in hello


def test_recent_lock_preserves_content_tab_use(shelf, hello):
    host, panel, _clipboard, _desktop = shelf()
    key, _ = host.vault.add_text("Synthetic content-tab note")
    assert panel.lock_files()
    panel.open(files=False)
    assert panel.files_locked
    assert panel.results.count() == 1
    panel.accept_current(paste=False)
    assert host.copied_rows[-1]["id"] == key
    assert "verify" not in hello


class Drop:
    def __init__(self, file):
        self.mime = QMimeData()
        self.mime.setUrls([QUrl.fromLocalFile(str(file))])
        self.accepted = False
        self.ignored = False

    def mimeData(self):
        return self.mime

    def source(self):
        return None

    def acceptProposedAction(self):
        self.accepted = True

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True

    def setDropAction(self, _action):
        pass


@pytest.mark.parametrize("target", ["panel", "file_list", "ball"])
def test_locked_drop_cannot_import_or_reveal_records(shelf, hello, tmp_path, monkeypatch, target):
    host, panel, _clipboard, _desktop = shelf()
    file = tmp_path / "Synthetic-Dropped-File.txt"
    file.write_text("test-only", encoding="utf-8")
    assert panel.lock_files()
    monkeypatch.setattr(host.recent_files, "add_paths", forbidden("drop import"))
    widget = {"panel": panel, "file_list": panel.file_results, "ball": panel.ensure_ball()}[target]
    entered, dropped = Drop(file), Drop(file)
    widget.dragEnterEvent(entered)
    widget.dropEvent(dropped)
    assert not entered.accepted
    assert not dropped.accepted
    assert panel.files_locked
    assert panel.file_results.count() == 0


def test_unavailable_hello_does_not_create_an_unrecoverable_new_lock(shelf, hello, monkeypatch):
    host, panel, _clipboard, _desktop = shelf()
    monkeypatch.setattr(recent_panel.windows_hello, "availability", lambda:
        recent_panel.windows_hello.Availability(False, "not_configured", "请先设置 Windows Hello"))
    assert panel.lock_files() is False
    assert not panel.files_locked
    assert host.vault.setting("recent_files_locked_v1", False) is False
    assert "verify" not in hello


@pytest.mark.parametrize("success,status", [
    (False, "cancelled"), (False, "error"), (True, "error"), (False, "verified"),
])
def test_non_verified_result_never_unlocks(shelf, hello, monkeypatch, app, success, status):
    host, panel, _clipboard, _desktop = shelf()
    assert panel.lock_files()
    calls = []

    def verification(*_args):
        calls.append("verification")
        return recent_panel.windows_hello.Verification(success, status, "Synthetic outcome")

    monkeypatch.setattr(recent_panel.windows_hello, "verify", verification)
    panel.unlock_files()
    wait_for(app, lambda: calls and panel._auth_worker is None)
    assert calls == ["verification"]
    assert panel.files_locked
    assert host.vault.setting("recent_files_locked_v1") is True
    assert panel.file_results.count() == 0


def test_only_verified_explicit_unlock_restores_recent_records(shelf, hello, monkeypatch, app, tmp_path):
    host, panel, _clipboard, _desktop = shelf()
    file = private_record(host, panel, tmp_path)
    assert panel.lock_files()
    calls = []

    def verification(hwnd, message, cancel_event):
        assert hwnd and isinstance(message, str) and not cancel_event.is_set()
        calls.append(threading.current_thread().name)
        return recent_panel.windows_hello.Verification(True, "verified", "Synthetic success")

    monkeypatch.setattr(recent_panel.windows_hello, "verify", verification)
    panel.open(files=True)
    assert calls == []
    panel.unlock_files()
    wait_for(app, lambda: calls and panel._auth_worker is None)
    assert len(calls) == 1
    assert calls[0] != threading.current_thread().name
    assert not panel.files_locked
    assert host.vault.setting("recent_files_locked_v1") is False
    assert panel.file_results.count() == 1
    assert panel.file_results.item(0).data(Qt.ItemDataRole.UserRole)["path"] == str(file)


@pytest.mark.parametrize("cancel_action", ["cancel_unlock", "lock_files", "collapse"])
def test_late_verification_after_cancel_relock_or_close_stays_locked(
        shelf, hello, monkeypatch, app, cancel_action):
    host, panel, _clipboard, _desktop = shelf()
    assert panel.lock_files()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def verification(*_args):
        calls.append("verification")
        entered.set()
        release.wait(timeout=3)
        # A backend may finish with Verified just as cancellation is requested.
        return recent_panel.windows_hello.Verification(True, "verified", "Late synthetic success")

    monkeypatch.setattr(recent_panel.windows_hello, "verify", verification)
    panel.open(files=True)
    panel.unlock_files()
    try:
        wait_for(app, entered.is_set)
        generation = panel._auth_generation
        panel.unlock_files()  # Repeated clicks must not start parallel verifications.
        assert calls == ["verification"]
        getattr(panel, cancel_action)()
        assert panel._auth_generation > generation
    finally:
        release.set()
        wait_for(app, lambda: panel._auth_worker is None)
    assert panel.files_locked
    assert host.vault.setting("recent_files_locked_v1") is True
    assert panel.file_results.count() == 0


def test_hello_focus_loss_does_not_collapse_pending_verification(shelf, hello, monkeypatch, app):
    _host, panel, _clipboard, _desktop = shelf()
    assert panel.lock_files()
    entered, release = threading.Event(), threading.Event()

    def verification(*_args):
        entered.set()
        release.wait(timeout=3)
        return recent_panel.windows_hello.Verification(False, "cancelled", "Synthetic cancellation")

    monkeypatch.setattr(recent_panel.windows_hello, "verify", verification)
    panel.keep_open.setChecked(False)
    panel.open(files=True)
    panel.unlock_files()
    try:
        wait_for(app, entered.is_set)
        monkeypatch.setattr(panel, "isActiveWindow", lambda: False)
        panel._hide_if_inactive()
        assert panel.isVisible()
    finally:
        release.set()
        wait_for(app, lambda: panel._auth_worker is None)
    assert panel.files_locked


def test_lock_cancels_pending_file_autopaste_without_clearing_clipboard(
        shelf, hello, tmp_path, app):
    host, panel, clipboard, desktop = shelf()
    private_record(host, panel, tmp_path)
    panel.target = 4242
    panel.accept_current(paste=True)
    assert len(clipboard.payloads) == 1
    assert desktop == [("focus", 4242)]
    assert panel.lock_files()
    QTest.qWait(130)
    assert desktop == [("focus", 4242)]
    assert len(clipboard.payloads) == 1
    assert panel.files_locked


def test_cannot_claim_lock_if_persistence_fails(shelf, hello, tmp_path, monkeypatch):
    host, panel, _clipboard, _desktop = shelf()
    private_record(host, panel, tmp_path)
    original_setting = host.vault.set_setting

    def fail_lock_setting(key, value):
        if key == "recent_files_locked_v1":
            raise OSError("Synthetic read-only storage")
        return original_setting(key, value)

    monkeypatch.setattr(host.vault, "set_setting", fail_lock_setting)
    assert panel.lock_files() is False
    assert not panel.files_locked
    assert host.vault.setting("recent_files_locked_v1", False) is False
    assert panel.file_results.count() == 1


def test_verified_unlock_keeps_lock_if_persistence_fails(shelf, hello, monkeypatch, app):
    host, panel, _clipboard, _desktop = shelf()
    assert panel.lock_files()
    original_setting = host.vault.set_setting
    calls = []

    def fail_unlock_setting(key, value):
        if key == "recent_files_locked_v1" and value is False:
            raise OSError("Synthetic read-only storage")
        return original_setting(key, value)

    def verification(*_args):
        calls.append("verification")
        return recent_panel.windows_hello.Verification(True, "verified", "Synthetic success")

    monkeypatch.setattr(host.vault, "set_setting", fail_unlock_setting)
    monkeypatch.setattr(recent_panel.windows_hello, "verify", verification)
    panel.unlock_files()
    wait_for(app, lambda: calls and panel._auth_worker is None)
    assert panel.files_locked
    assert host.vault.setting("recent_files_locked_v1") is True


@pytest.mark.parametrize("picker", ["file", "folder"])
def test_dialog_return_cannot_add_paths_if_locked_while_dialog_was_open(
        shelf, hello, monkeypatch, tmp_path, picker):
    host, panel, _clipboard, _desktop = shelf()
    file = tmp_path / "Synthetic-Late-Dialog-File.txt"
    file.write_text("test-only", encoding="utf-8")
    watched = tmp_path / "Synthetic-Late-Dialog-Folder"
    watched.mkdir()
    monkeypatch.setattr(host.recent_files, "add_paths", forbidden("late picker import"))
    monkeypatch.setattr(host.recent_files, "set_folders", forbidden("late picker folder change"))

    def delayed_file(*_args):
        assert panel.lock_files()
        return [str(file)], "All files"

    def delayed_folder(*_args):
        assert panel.lock_files()
        return str(watched)

    monkeypatch.setattr(recent_panel.QFileDialog, "getOpenFileNames", delayed_file)
    monkeypatch.setattr(recent_panel.QFileDialog, "getExistingDirectory", delayed_folder)
    (panel.choose_files if picker == "file" else panel.choose_folder)()
    assert panel.files_locked
    assert panel.file_results.count() == 0


def test_old_source_menu_action_cannot_modify_sources_after_lock(
        shelf, hello, tmp_path, monkeypatch):
    host, panel, _clipboard, _desktop = shelf()
    watched = tmp_path / "Synthetic-Existing-Watched-Folder"
    watched.mkdir()
    host.recent_files.set_folders([watched])
    original_folders = host.recent_files.folders()
    observed = []

    def old_menu_trigger(menu, _point):
        folders = [action for action in menu.actions() if action.menu() is not None
                   and action.text() == str(watched)]
        assert len(folders) == 1
        old_action = next(action for action in folders[0].menu().actions()
                          if action.text() == "停止监听")
        assert panel.lock_files()
        old_action.trigger()
        observed.append("triggered")
        menu.deleteLater()

    monkeypatch.setattr(panel, "run_menu", old_menu_trigger)
    panel.source_menu()
    assert observed == ["triggered"]
    assert panel.files_locked
    assert host.recent_files.folders() == original_folders


def test_worker_completion_updates_widgets_on_gui_thread(shelf, hello, monkeypatch, app):
    _host, panel, _clipboard, _desktop = shelf()
    assert panel.lock_files()
    finished_threads = []
    original_finished = panel._auth_finished

    def record_finished(worker):
        finished_threads.append(threading.current_thread().name)
        original_finished(worker)

    monkeypatch.setattr(panel, "_auth_finished", record_finished)
    panel.unlock_files()
    wait_for(app, lambda: panel._auth_worker is None and finished_threads)
    assert finished_threads == [threading.current_thread().name]
