"""Read-only Recent Items parsing, using synthetic links and local targets."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading
import time

import pytest

from snapvault import windows_recent as recent


def test_recent_targets_are_local_files_deduplicated_and_cached(tmp_path):
    links = tmp_path / "recent"
    links.mkdir()
    file = tmp_path / "report.txt"
    file.write_text("local target")
    folder = tmp_path / "folder"
    folder.mkdir()
    targets = {"old.lnk": str(file), "new.lnk": str(file), "folder.lnk": str(folder),
               "missing.lnk": str(tmp_path / "missing.txt"), "remote.lnk": r"\\server\share\secret.txt",
               "url.lnk": "https://example.com/", "relative.lnk": "relative.txt"}
    stamp = time.time() - 100
    for index, name in enumerate(targets):
        link = links / name
        link.write_bytes(b"synthetic")
        os.utime(link, (stamp + index, stamp + index))
    calls = []
    model = recent.RecentItems(lambda path: (calls.append(path), targets[Path(path).name])[-1])
    rows = model.scan(str(links))
    assert rows == [dict(path=str(file), used_at=pytest.approx(stamp + 1, abs=0.01),
                         recent_mtime_ns=(links / "new.lnk").stat().st_mtime_ns)]
    assert len(calls) == len(targets)
    model.scan(str(links))
    assert model.metrics["links_parsed"] == 0
    assert len(calls) == len(targets)
    model.close()


def test_scan_limits_shortcut_parsing_and_skips_jumplist_subdirectories(tmp_path):
    links = tmp_path / "recent"
    links.mkdir()
    target = tmp_path / "file.txt"
    target.write_text("local")
    nested = links / "AutomaticDestinations"
    nested.mkdir()
    (nested / "history.automaticDestinations-ms").write_bytes(b"must never be parsed")
    for index in range(320):
        (links / f"file-{index}.lnk").write_bytes(b"synthetic")
    calls = []
    model = recent.RecentItems(lambda path: (calls.append(path), str(target))[-1])
    assert len(model.scan(str(links))) == 1
    assert len(calls) == recent.MAX_LINKS == 200
    assert model.metrics["links_considered"] == 200
    model.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows COM Shell Link")
def test_native_shell_link_read_returns_target_without_executing_it(tmp_path):
    target = tmp_path / "never-executed.cmd"
    target.write_text("@echo off\necho unexpected>should-not-exist.txt\n")
    shortcut = tmp_path / "synthetic.lnk"
    # Synthetic shortcut creation uses SetPath/Save in the test only. Production
    # code exclusively exposes Load/GetPath and never calls Resolve or Execute.
    with recent.ShellLinkResolver() as resolver:
        hr = recent._method(resolver._shell, 20, ctypes.c_long, wintypes.LPCWSTR)(resolver._shell, str(target))
        assert hr >= 0
        hr = recent._method(resolver._persist, 6, ctypes.c_long, wintypes.LPCWSTR, wintypes.BOOL)(
            resolver._persist, str(shortcut), True)
        assert hr >= 0
        assert os.path.normcase(resolver(shortcut)) == os.path.normcase(str(target))
    assert not (tmp_path / "should-not-exist.txt").exists()
    assert not resolver._persist.value and not resolver._shell.value and not resolver._initialized


def test_native_resolver_lifetime_stays_on_scanning_thread(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_text("local")
    (tmp_path / "file.lnk").write_bytes(b"synthetic")
    events = []

    class FakeNative:
        def __enter__(self):
            events.append(("enter", threading.get_ident()))
            return self

        def __call__(self, _shortcut):
            events.append(("read", threading.get_ident()))
            return str(target)

        def close(self):
            events.append(("close", threading.get_ident()))

    monkeypatch.setattr(recent, "ShellLinkResolver", FakeNative)

    def scan():
        source = recent.RecentItems()
        source.scan(str(tmp_path))
        source.scan(str(tmp_path))
        source.close()

    worker = threading.Thread(target=scan)
    worker.start()
    worker.join(2)
    assert [event for event, _thread in events] == ["enter", "read", "close"]
    assert len({thread for _event, thread in events}) == 1
    assert events[0][1] != threading.get_ident()
