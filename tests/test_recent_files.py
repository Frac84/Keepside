"""Recent files use temporary directories; never enumerate real Downloads."""
import copy
import builtins
import io
import json
import os
from pathlib import Path
import time

from PySide6.QtWidgets import QApplication
import pytest

from snapvault import recent_files


class MemoryVault:
    def __init__(self):
        self.settings = {}

    def setting(self, key, default=None):
        return copy.deepcopy(self.settings.get(key, default))

    def set_setting(self, key, value):
        self.settings[key] = copy.deepcopy(value)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def service(app, monkeypatch):
    monkeypatch.setattr(recent_files, "STABILITY_SECONDS", 0.04)
    instances = []

    def create(vault=None, directories=(), watch=False, **options):
        options.setdefault("system_recent", False)
        value = recent_files.RecentFiles(vault or MemoryVault(),
                                        directories=None if directories is None else list(directories),
                                        watch=watch, **options)
        instances.append(value)
        return value

    yield create
    for value in instances:
        value.stop()
    app.processEvents()


def wait_for(app, condition, timeout=4):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("Recent-files background operation did not finish")


def test_only_selected_directory_top_level_and_completed_files(app, service, tmp_path):
    chosen, outside = tmp_path / "chosen", tmp_path / "outside"
    chosen.mkdir()
    outside.mkdir()
    (chosen / "photo.png").write_bytes(b"test image stand-in")
    (chosen / "report.pdf").write_bytes(b"test pdf stand-in")
    for name in ("video.mp4.crdownload", "image.part", "archive.partial", "draft.tmp", "~$report.docx", "shortcut.lnk"):
        (chosen / name).write_bytes(b"not a completed regular file")
    nested = chosen / "nested"
    nested.mkdir()
    (nested / "private.txt").write_text("not recursively observed")
    (outside / "unselected.txt").write_text("not observed")
    model = service(directories=[chosen])
    wait_for(app, lambda: len(model.entries()) == 2)
    assert {row["name"] for row in model.entries()} == {"photo.png", "report.pdf"}
    assert all(Path(row["path"]).parent == chosen for row in model.entries())


def test_initial_order_uses_modification_time_and_watch_detects_new_file(app, service, tmp_path):
    old, newer = tmp_path / "old.txt", tmp_path / "newer.txt"
    old.write_text("old")
    newer.write_text("newer")
    timestamp = time.time() - 100
    os.utime(old, (timestamp - 50, timestamp - 50))
    os.utime(newer, (timestamp, timestamp))
    model = service(directories=[tmp_path], watch=True)
    wait_for(app, lambda: len(model.entries()) == 2)
    assert [row["name"] for row in model.entries()] == ["newer.txt", "old.txt"]
    assert model.entries()[0]["seen_at"] == pytest.approx(timestamp, abs=0.01)
    (tmp_path / "just-downloaded.txt").write_text("new file")
    wait_for(app, lambda: len(model.entries()) == 3)
    assert model.entries()[0]["name"] == "just-downloaded.txt"


def test_unreadable_file_waits_until_explicit_refresh(app, service, tmp_path, monkeypatch):
    file = tmp_path / "not-ready.pdf"
    file.write_bytes(b"still held by downloading application")
    readable = False
    monkeypatch.setattr(recent_files, "_readable", lambda _path: readable)
    model = service(directories=[tmp_path])
    wait_for(app, lambda: model._retry_rounds == 1)
    assert model.entries() == []
    readable = True
    model.refresh()
    wait_for(app, lambda: len(model.entries()) == 1)


def test_dismissal_survives_refresh_and_restart_but_allows_changed_file(app, service, tmp_path):
    file = tmp_path / "report.txt"
    file.write_text("original")
    vault = MemoryVault()
    model = service(vault, [tmp_path])
    wait_for(app, lambda: len(model.entries()) == 1)
    key = model.entries()[0]["key"]
    assert model.remove(key)
    assert file.read_text() == "original"
    model.refresh()
    wait_for(app, lambda: not model._seed_pass)
    time.sleep(0.08)
    app.processEvents()
    assert model.entries() == []
    model.stop()
    replacement = service(vault, [tmp_path])
    wait_for(app, lambda: not replacement._seed_pass)
    assert replacement.entries() == []
    file.write_text("a substantially updated document")
    replacement.refresh()
    wait_for(app, lambda: len(replacement.entries()) == 1)
    assert replacement.entries()[0]["key"] == key
    assert file.exists()


def test_pin_search_and_manual_file_persist_with_no_watched_folders(app, service, tmp_path):
    first, second = tmp_path / "First Report.pdf", tmp_path / "second.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    vault = MemoryVault()
    model = service(vault)
    assert model.add_paths([first]) == 1
    key = model.entries()[0]["key"]
    assert model.set_pinned(key, True)
    assert model.add_paths([second]) == 1
    assert model.entries()[0]["key"] == key
    assert len(model.entries("FIRST report")) == 1
    snapshot = model.entries()[0]
    snapshot["name"] = "caller changes cannot modify the model"
    assert model.entries()[0]["name"] == first.name
    model.stop()
    replacement = service(vault)
    wait_for(app, lambda: not replacement._seed_pass)
    assert replacement.entries()[0]["key"] == key
    assert replacement.entries()[0]["pinned"] is True
    assert all(row["source"] == "手动添加" for row in replacement.entries())


def test_disappearing_and_returning_file_updates_cached_availability(app, service, tmp_path):
    file = tmp_path / "temporary.txt"
    file.write_text("original")
    model = service()
    model.add_paths([file])
    key = model.entries()[0]["key"]
    model.set_pinned(key, True)
    file.unlink()
    model.refresh()
    wait_for(app, lambda: not model.entries()[0]["exists"])
    assert model.entries()[0]["pinned"]
    file.write_text("back again")
    model.refresh()
    wait_for(app, lambda: model.entries()[0]["exists"])
    assert model.entries()[0]["key"] == key


def test_folder_configuration_persists_and_disabling_does_not_fall_back(app, service, tmp_path, monkeypatch):
    folder = tmp_path / "watch"
    folder.mkdir()
    (folder / "document.txt").write_text("doc")
    # A trap would be discovered if an explicit [] accidentally used Downloads.
    monkeypatch.setattr(recent_files.QStandardPaths, "writableLocation", lambda _kind: str(folder))
    vault = MemoryVault()
    disabled = service(vault, [])
    wait_for(app, lambda: not disabled._seed_pass)
    assert disabled.folders() == []
    assert disabled.entries() == []
    disabled.set_folders([folder, folder, ""])
    wait_for(app, lambda: len(disabled.entries()) == 1)
    assert disabled.folders() == [str(folder.resolve())]
    assert disabled.entries()[0]["source"] == "下载"
    disabled.stop()
    restored = recent_files.RecentFiles(vault, watch=False)
    try:
        wait_for(app, lambda: len(restored.entries()) == 1)
        assert restored.folders() == [str(folder.resolve())]
        restored.set_folders([])
        assert restored.entries() == []
        restored.refresh()
        wait_for(app, lambda: not restored._seed_pass)
        assert restored.entries() == []
    finally:
        restored.stop()


def test_manual_add_rejects_directories_and_incomplete_files_without_touching_them(app, service, tmp_path):
    incomplete = tmp_path / "unfinished.part"
    incomplete.write_text("keep me")
    model = service()
    assert model.add_paths([tmp_path, incomplete, tmp_path / "missing.txt"]) == 0
    assert incomplete.read_text() == "keep me"
    assert model.entries() == []
    model.stop()
    assert not model._worker.is_alive()
    model.stop()  # Closing through multiple app teardown paths is harmless.


def test_more_candidates_than_shelf_capacity_do_not_rotate_old_files_to_top(app, service, tmp_path, monkeypatch):
    monkeypatch.setattr(recent_files, "MAX_RECORDS", 3)
    timestamp = time.time() - 1000
    for index in range(7):
        file = tmp_path / f"document-{index}.txt"
        file.write_text(str(index))
        os.utime(file, (timestamp + index, timestamp + index))
    model = service(directories=[tmp_path])
    wait_for(app, lambda: len(model.entries()) == 3)
    original = [(row["name"], row["seen_at"]) for row in model.entries()]
    assert [name for name, _ in original] == ["document-6.txt", "document-5.txt", "document-4.txt"]
    for _ in range(3):
        model.refresh()
        generation = model._generation
        wait_for(app, lambda: model._generation == generation and len(model._observed) == 7)
        time.sleep(0.08)
        app.processEvents()
        assert [(row["name"], row["seen_at"]) for row in model.entries()] == original
    newest = tmp_path / "genuinely-new.txt"
    newest.write_text("download keeps a server-provided old modification time")
    os.utime(newest, (timestamp - 100, timestamp - 100))
    model.refresh()
    wait_for(app, lambda: model.entries()[0]["name"] == newest.name)
    assert model.entries()[0]["seen_at"] > timestamp + 100


def test_pending_download_keeps_rechecking_after_four_attempts(app, service, tmp_path, monkeypatch):
    file = tmp_path / "long-download.bin"
    file.write_bytes(b"data")
    attempts = []
    monkeypatch.setattr(recent_files, "RETRY_START_MS", 10)
    monkeypatch.setattr(recent_files, "RETRY_MAX_MS", 40)

    def becomes_readable(_path):
        attempts.append(_path)
        return len(attempts) >= 7

    monkeypatch.setattr(recent_files, "_readable", becomes_readable)
    model = service(directories=[tmp_path])
    wait_for(app, lambda: len(model.entries()) == 1)
    assert len(attempts) >= 7
    assert not model._retry.isActive()


def test_system_recent_uses_shortcut_timestamp_and_survives_restart(app, service, tmp_path):
    recent, documents = tmp_path / "recent", tmp_path / "documents"
    recent.mkdir()
    documents.mkdir()
    document = documents / "old-document.txt"
    document.write_text("local file")
    shortcut = recent / "old-document.lnk"
    shortcut.write_bytes(b"synthetic shortcut resolved by injected callable")
    used_at = time.time() - 600
    os.utime(shortcut, (used_at, used_at))
    vault = MemoryVault()
    options = dict(system_recent=True, recent_directory=recent, resolver=lambda _path: str(document))
    model = service(vault, **options)
    wait_for(app, lambda: len(model.entries()) == 1)
    entry = model.entries()[0]
    assert entry["source"] == "最近使用"
    assert entry["origin"] == "system_recent"
    assert entry["seen_at"] == pytest.approx(used_at, abs=0.01)
    model.stop()
    replacement = service(vault, **options)
    wait_for(app, lambda: not replacement._seed_pass)
    assert replacement.entries()[0]["seen_at"] == entry["seen_at"]


def test_system_recent_deduplicates_download_and_writes_only_when_changed(app, service, tmp_path):
    recent, downloads = tmp_path / "recent", tmp_path / "downloads"
    recent.mkdir()
    downloads.mkdir()
    file = downloads / "report.pdf"
    file.write_bytes(b"data")
    shortcut = recent / "report.lnk"
    shortcut.write_bytes(b"link")
    os.utime(file, (time.time() - 500, time.time() - 500))
    vault = MemoryVault()
    writes = []
    original_save = vault.set_setting
    vault.set_setting = lambda key, value: (writes.append(key), original_save(key, value))[-1]
    model = service(vault, [downloads], system_recent=True, recent_directory=recent, resolver=lambda _path: str(file))
    wait_for(app, lambda: len(model.entries()) == 1)
    assert model.entries()[0]["origin"] == "system_recent"
    count = len(writes)
    model.refresh()
    time.sleep(0.09)
    app.processEvents()
    assert len(writes) == count
    assert model.scan_metrics()["links_parsed"] == 0
    model.set_system_recent_enabled(False)
    wait_for(app, lambda: model.entries()[0]["origin"] == "watch")
    assert not model.system_recent_enabled()
    assert file.exists() and shortcut.exists()


def test_system_recent_removal_reappears_only_after_later_use_and_clearing_source_drops_it(app, service, tmp_path):
    recent = tmp_path / "recent"
    recent.mkdir()
    file = tmp_path / "document.txt"
    file.write_text("not in a watched folder")
    shortcut = recent / "document.lnk"
    shortcut.write_bytes(b"link")
    model = service(system_recent=True, recent_directory=recent, resolver=lambda _path: str(file))
    wait_for(app, lambda: len(model.entries()) == 1)
    key = model.entries()[0]["key"]
    assert model.remove(key)
    model.refresh()
    time.sleep(0.09)
    app.processEvents()
    assert model.entries() == []
    stamp = shortcut.stat().st_mtime + 30
    os.utime(shortcut, (stamp, stamp))
    model.refresh()
    wait_for(app, lambda: len(model.entries()) == 1)
    assert model.entries()[0]["key"] == key
    shortcut.unlink()
    model.refresh()
    wait_for(app, lambda: model.entries() == [])
    assert file.read_text() == "not in a watched folder"


def test_isolated_default_never_discovers_real_recent_directory(app, tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("Isolated instance touched the real Known Folder Recent")
    monkeypatch.setattr(recent_files.windows_recent, "recent_directory", forbidden)
    model = recent_files.RecentFiles(MemoryVault(), directories=[], watch=False)
    try:
        wait_for(app, lambda: not model._seed_pass)
        assert not model.system_recent_enabled()
        assert model.entries() == []
    finally:
        model.stop()


def test_recent_directory_notifications_discover_new_system_entry(app, service, tmp_path):
    recent = tmp_path / "recent"
    recent.mkdir()
    file = tmp_path / "newly-opened.txt"
    file.write_text("target")
    model = service(watch=True, system_recent=True, recent_directory=recent, resolver=lambda _path: str(file))
    wait_for(app, lambda: not model._seed_pass)
    assert model.entries() == []
    (recent / "opened.lnk").write_bytes(b"synthetic")
    wait_for(app, lambda: len(model.entries()) == 1)
    assert model.entries()[0]["path"] == str(file)
    assert model.entries()[0]["source"] == "最近使用"


def test_default_records_opened_files_without_scanning_downloads(app, service, tmp_path, monkeypatch):
    recent, downloads = tmp_path / "recent", tmp_path / "downloads"
    recent.mkdir()
    downloads.mkdir()
    file = downloads / "downloaded-but-not-opened.pdf"
    file.write_bytes(b"newly downloaded")
    monkeypatch.setattr(recent_files.QStandardPaths, "writableLocation", lambda _kind: str(downloads))
    model = service(directories=None, watch=True, system_recent=True,
                    recent_directory=recent, resolver=lambda _path: str(file))
    wait_for(app, lambda: not model._seed_pass)
    assert model.folders() == []
    assert model.entries() == []
    assert str(downloads) not in model._watcher.directories()
    (downloads / "another-new-file.txt").write_text("not opened")
    model.refresh()
    wait_for(app, lambda: model.scan_metrics().get("links_considered") == 0)
    assert model.entries() == []
    (recent / "opened.lnk").write_bytes(b"history now records the file being opened")
    wait_for(app, lambda: len(model.entries()) == 1)
    assert model.entries()[0]["path"] == str(file)
    assert model.entries()[0]["origin"] == "system_recent"


def test_old_implicit_download_feed_is_pruned_without_losing_manual_or_pins(app, service, tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    automatic, pinned, manual = [downloads / name for name in ("automatic.txt", "pinned.txt", "manual.txt")]
    for file in (automatic, pinned, manual):
        file.write_text(file.name)
    monkeypatch.setattr(recent_files.QStandardPaths, "writableLocation", lambda _kind: str(downloads))
    vault = MemoryVault()
    # The old automatic default never persisted the folder setting. Construct
    # that old state through an injected feed, without an explicit set_folders.
    legacy = service(vault, directories=[downloads])
    wait_for(app, lambda: len(legacy.entries()) == 3)
    legacy.set_pinned(recent_files._key(str(pinned)), True)
    legacy.add_paths([manual])
    legacy.stop()
    assert recent_files.FOLDERS_KEY not in vault.settings
    migrated = service(vault, directories=None)
    wait_for(app, lambda: not migrated._seed_pass)
    assert migrated.folders() == []
    assert {row["name"] for row in migrated.entries()} == {pinned.name, manual.name}
    assert {row["name"] for row in vault.settings[recent_files.STATE_KEY]["records"]} == {pinned.name, manual.name}
    assert all(file.read_text() == file.name for file in (automatic, pinned, manual))


def test_multigigabyte_open_video_is_only_a_metadata_reference(app, service, tmp_path, monkeypatch):
    recent = tmp_path / "recent"
    recent.mkdir()
    video = tmp_path / "five-gigabyte-video.mp4"
    size = 5 * 1024 ** 3
    with video.open("wb") as stream:
        if os.name == "nt":
            # Mark the synthetic file sparse before extending it: this test
            # never allocates or writes 5 GB of zero-filled video contents.
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            control = kernel.DeviceIoControl
            control.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            control.restype = wintypes.BOOL
            returned = wintypes.DWORD()
            assert control(msvcrt.get_osfhandle(stream.fileno()), 0x900C4, None, 0,
                           None, 0, ctypes.byref(returned), None), ctypes.get_last_error()
        stream.truncate(size)
    (recent / "video.lnk").write_bytes(b"synthetic recent reference")
    original_builtin, original_io = builtins.open, io.open

    def guarded(original):
        def open_other_files(path, *args, **kwargs):
            if not isinstance(path, int) and os.path.normcase(os.fspath(path)) == os.path.normcase(str(video)):
                raise AssertionError("History indexing opened the video target")
            return original(path, *args, **kwargs)
        return open_other_files

    monkeypatch.setattr(builtins, "open", guarded(original_builtin))
    monkeypatch.setattr(io, "open", guarded(original_io))
    monkeypatch.setattr(recent_files, "_readable", lambda _path: pytest.fail("Recent targets need metadata only"))
    monkeypatch.setattr(recent_files, "STABILITY_SECONDS", 5)
    vault = MemoryVault()
    model = service(vault, directories=None, system_recent=True, recent_directory=recent,
                    resolver=lambda _path: str(video))
    wait_for(app, lambda: len(model.entries()) == 1, timeout=2)
    entry = model.entries()[0]
    assert entry["path"] == str(video)
    assert entry["size"] == size
    assert entry["origin"] == "system_recent"
    assert len(json.dumps(vault.settings[recent_files.STATE_KEY])) < 2048
    assert model.remove(entry["key"])
    assert video.stat().st_size == size


def test_recent_open_time_orders_files_independently_of_content_modification(app, service, tmp_path):
    recent = tmp_path / "recent"
    recent.mkdir()
    old_open, new_open = tmp_path / "edited-today.txt", tmp_path / "opened-today.mp4"
    old_open.write_text("new content")
    new_open.write_bytes(b"old video")
    stamp = time.time() - 600
    os.utime(new_open, (stamp - 5000, stamp - 5000))
    old_link, new_link = recent / "old.lnk", recent / "new.lnk"
    old_link.write_bytes(b"old open")
    new_link.write_bytes(b"new open")
    os.utime(old_link, (stamp, stamp))
    os.utime(new_link, (stamp + 100, stamp + 100))
    model = service(directories=None, system_recent=True, recent_directory=recent,
                    resolver=lambda path: str(old_open if Path(path).name == old_link.name else new_open))
    wait_for(app, lambda: len(model.entries()) == 2)
    assert [row["name"] for row in model.entries()] == [new_open.name, old_open.name]
    assert [row["seen_at"] for row in model.entries()] == pytest.approx([stamp + 100, stamp], abs=0.01)
    # Opening the first file again changes its history timestamp, regardless
    # of either original file's modification timestamp or file type/size.
    os.utime(old_link, (stamp + 200, stamp + 200))
    model.refresh()
    wait_for(app, lambda: model.entries()[0]["name"] == old_open.name)
    assert model.entries()[0]["seen_at"] == pytest.approx(stamp + 200, abs=0.01)
