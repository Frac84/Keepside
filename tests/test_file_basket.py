"""File basket behavior uses synthetic local paths and never a real vault."""
import builtins
import copy
import os
from pathlib import Path
import shutil

from PySide6.QtWidgets import QApplication
import pytest

from snapvault import file_basket
from snapvault.file_basket import BasketAddResult, FileBasket, SETTING_KEY


class MemoryVault:
    def __init__(self):
        self.settings = {}
        self.writes = 0
        self.fail = False

    def setting(self, key, default=None):
        return copy.deepcopy(self.settings.get(key, default))

    def set_setting(self, key, value):
        if self.fail:
            raise OSError("synthetic persistence failure")
        self.settings[key] = copy.deepcopy(value)
        self.writes += 1


@pytest.fixture(scope="module", autouse=True)
def application():
    return QApplication.instance() or QApplication([])


def make_file(root, name, content=b"synthetic content"):
    path = root / name
    path.write_bytes(content)
    return path


def test_paths_persist_in_order_and_emit_after_success(tmp_path):
    vault = MemoryVault()
    basket = FileBasket(vault)
    first, second = make_file(tmp_path, "first.txt"), make_file(tmp_path, "second.bin")
    notifications = []
    basket.changed.connect(lambda: notifications.append((basket.count, copy.deepcopy(vault.settings))))
    assert basket.add_paths([first, second]) == BasketAddResult(2, 0, 0, 0)
    assert [Path(row["path"]) for row in basket.entries()] == [first, second]
    assert notifications[0][0] == 2
    assert notifications[0][1][SETTING_KEY] == {"version": 1, "paths": [str(first), str(second)]}
    restarted = FileBasket(vault)
    assert restarted.entries() == basket.entries()
    assert vault.writes == 1


def test_case_and_normalized_path_duplicates_do_not_write_again(tmp_path):
    path = make_file(tmp_path, "CaseFile.txt")
    vault = MemoryVault()
    basket = FileBasket(vault)
    assert basket.add_paths([path, str(path), os.path.join(str(tmp_path), ".", path.name)]).duplicates == 2
    assert basket.add_paths([str(path).swapcase()]).duplicates == 1
    assert basket.count == 1 and vault.writes == 1


def test_capacity_keeps_existing_order_and_reports_overflow(tmp_path):
    files = [make_file(tmp_path, str(index) + ".txt") for index in range(4)]
    basket = FileBasket(MemoryVault(), limit=2)
    assert basket.add_paths(files) == BasketAddResult(2, 0, 0, 2)
    assert basket.existing_paths() == files[:2]
    assert basket.add_paths([files[0], files[2]]) == BasketAddResult(0, 1, 0, 1)
    assert basket.add_paths([files[2], files[2]]) == BasketAddResult(0, 1, 0, 1)


@pytest.mark.parametrize("bad", [r"\\server\share\document.txt", "//server/share/a", r"\\?\C:\temp\a.txt",
                                  r"\\.\PhysicalDrive0", "file:///C:/temp/a.txt", "https://example.com/a",
                                  "relative.txt", "", "a\0b", None, 12, b"/tmp/a"])
def test_remote_device_relative_and_invalid_paths_rejected_before_stat(bad, monkeypatch):
    basket = FileBasket(MemoryVault())
    monkeypatch.setattr(file_basket, "_metadata", lambda _: (_ for _ in ()).throw(AssertionError("unsafe path stat")))
    assert basket.add_paths([bad]) == BasketAddResult(skipped=1)
    assert basket.count == 0


def test_folder_and_missing_file_are_skipped(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    basket = FileBasket(MemoryVault())
    assert basket.add_paths([folder, tmp_path / "missing.txt"]) == BasketAddResult(skipped=2)


def test_mapped_remote_drive_is_rejected_before_filesystem_stat(tmp_path, monkeypatch):
    path = make_file(tmp_path, "mapped-drive-stand-in.txt")
    basket = FileBasket(MemoryVault())
    monkeypatch.setattr(file_basket, "local_path", lambda _path: "")
    monkeypatch.setattr(file_basket.os, "stat", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no remote stat")))
    assert basket.add_paths([path]) == BasketAddResult(skipped=1)


def test_reparse_ancestor_is_rejected_without_touching_target(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat
    parent = tmp_path / "junction-stand-in"
    parent.mkdir()
    path = make_file(parent, "target.txt")
    basket = FileBasket(MemoryVault())
    real_stat = os.stat

    def metadata(value, **kwargs):
        if Path(value) == parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        if Path(value) == path:
            raise AssertionError("must not follow the junction")
        return real_stat(value, **kwargs)

    monkeypatch.setattr(file_basket.os, "stat", metadata)
    assert basket.add_paths([path]) == BasketAddResult(skipped=1)


def test_removed_file_reference_survives_reload_and_is_not_exported(tmp_path):
    path = make_file(tmp_path, "temporary.txt")
    vault = MemoryVault()
    basket = FileBasket(vault)
    basket.add_paths([path])
    original_key = basket.entries()[0]["key"]
    path.unlink()
    restarted = FileBasket(vault)
    assert restarted.count == 1
    row = restarted.entries()[0]
    assert row["key"] == original_key and row["exists"] is False
    assert restarted.existing_paths() == []
    assert restarted.count == 1 and vault.writes == 1


def test_selective_paths_search_and_remove_do_not_touch_originals(tmp_path):
    first, second = make_file(tmp_path, "notes.txt"), make_file(tmp_path, "photo.png", b"not decoded")
    originals = {path: path.read_bytes() for path in (first, second)}
    basket = FileBasket(MemoryVault())
    basket.add_paths([first, second])
    first_key = basket.entries("NOTES")[0]["key"]
    assert basket.existing_paths([first_key]) == [first]
    assert basket.remove([first_key]) == 1 and basket.count == 1
    assert basket.remove([first_key]) == 0
    assert basket.clear() == 1 and basket.clear() == 0
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize("operation", ["add", "remove", "clear"])
def test_failed_persistence_preserves_memory_and_sends_no_signal(tmp_path, operation):
    first, second = make_file(tmp_path, "first.txt"), make_file(tmp_path, "second.txt")
    vault = MemoryVault()
    basket = FileBasket(vault)
    basket.add_paths([first])
    before, stored = basket.entries(), copy.deepcopy(vault.settings)
    signals = []
    basket.changed.connect(lambda: signals.append(True))
    vault.fail = True
    with pytest.raises(OSError, match="synthetic"):
        if operation == "add":
            basket.add_paths([second])
        elif operation == "remove":
            basket.remove([before[0]["key"]])
        else:
            basket.clear()
    assert basket.entries() == before and vault.settings == stored and signals == []


def test_large_video_is_only_statted_not_read_or_copied(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-eight-gib.mp4"
    # Mock metadata for a synthetic path rather than allocating an 8 GiB file.
    path.write_bytes(b"small sentinel")
    original_metadata = file_basket._metadata

    def metadata(value):
        info = original_metadata(value)
        if info is None:
            return None
        from types import SimpleNamespace
        return SimpleNamespace(st_size=8 * 1024 ** 3, st_mtime_ns=info.st_mtime_ns)

    monkeypatch.setattr(file_basket, "_metadata", metadata)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("file body must not be opened or copied")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(shutil, "copyfile", forbidden)
    monkeypatch.setattr(shutil, "copyfileobj", forbidden)
    vault = MemoryVault()
    basket = FileBasket(vault)
    assert basket.add_paths([path]).added == 1
    assert basket.entries()[0]["size"] == 8 * 1024 ** 3
    assert basket.existing_paths() == [path]
    assert vault.settings[SETTING_KEY] == {"version": 1, "paths": [str(path)]}


def test_load_sanitizes_bad_state_without_rewriting_or_stating(tmp_path, monkeypatch):
    path = str(tmp_path / "missing.txt")
    vault = MemoryVault()
    vault.settings[SETTING_KEY] = {"version": 1, "paths": [path, path, "relative", r"\\server\share\a", None]}
    monkeypatch.setattr(file_basket, "_metadata", lambda _: (_ for _ in ()).throw(AssertionError("no stat during load")))
    basket = FileBasket(vault)
    assert basket.count == 1 and vault.writes == 0


@pytest.mark.parametrize("state", [None, [], "wrong", {"version": 99, "paths": []}, {"version": 1, "paths": "wrong"}])
def test_malformed_state_is_empty_and_not_overwritten(state):
    vault = MemoryVault()
    vault.settings[SETTING_KEY] = state
    basket = FileBasket(vault)
    assert basket.count == 0 and vault.writes == 0
