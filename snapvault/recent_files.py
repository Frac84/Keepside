"""A small, local index of files that can be picked up again.

External files are never imported into the screenshot library or deleted here.
Windows Recent provides the default history; optional, explicitly chosen folders
are observed without recursion. Indexing a recently opened file uses metadata
only, even when another application still has that file open.
"""
from __future__ import annotations

import hashlib
import heapq
import math
import os
from pathlib import Path
import stat
import threading
import time
from collections import OrderedDict
import copy

from PySide6.QtCore import QFileSystemWatcher, QObject, QStandardPaths, QTimer, Signal

from . import windows_recent


STATE_KEY = "recent_files_state_v1"
FOLDERS_KEY = "recent_files_folders"
SYSTEM_RECENT_KEY = "recent_files_system_enabled"
MAX_FOLDERS = 6
MAX_RECORDS = 180
MAX_PINNED = 60
MAX_IGNORED = 400
SCAN_LIMIT = 240
STABILITY_SECONDS = 0.55
RETRY_START_MS = 850
RETRY_MAX_MS = 10_000
MAX_OBSERVED = MAX_FOLDERS * SCAN_LIMIT + MAX_RECORDS * 2
_TEMP_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".temp", ".download", ".lnk")


def _path(value):
    try:
        raw = os.fspath(value)
        if not raw or not str(raw).strip():
            return ""
        return str(Path(raw).expanduser().resolve(strict=False))
    except (TypeError, ValueError, OSError, RuntimeError):
        return ""


def _key(path):
    return hashlib.sha256(os.path.normcase(path).encode("utf-8", errors="surrogatepass")).hexdigest()[:24]


def _temporary(path):
    name = Path(path).name.lower()
    return not name or name.startswith(("~$", ".~", ".")) or name.endswith(_TEMP_SUFFIXES)


def _signature(record):
    value = [record["size"], record["mtime_ns"]]
    if record.get("origin") == "system_recent":
        value.append(record.get("recent_mtime_ns", 0))
    return value


def _inspect(path, source, origin="watch"):
    if _temporary(path):
        return None
    try:
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return None
        return dict(key=_key(path), path=path, name=Path(path).name, source=source,
                    size=info.st_size, mtime_ns=info.st_mtime_ns, mtime=info.st_mtime,
                    origin=origin)
    except (OSError, ValueError):
        return None


def _readable(path):
    try:
        # Opening is enough to reject exclusively held/inaccessible files. No
        # file body is read, parsed, indexed, or sent anywhere.
        with open(path, "rb"):
            return True
    except (OSError, ValueError):
        return False


class _Bridge(QObject):
    completed = Signal(object)


class _Scanner(threading.Thread):
    """Latest request wins; a daemon never holds application shutdown open."""

    def __init__(self, bridge, resolver=None):
        super().__init__(name="recent-file-index", daemon=True)
        self.bridge = bridge
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.guard = threading.Lock()
        self.request = None
        self.system_source = windows_recent.RecentItems(resolver)

    def submit(self, request):
        with self.guard:
            self.request = request
        self.wake.set()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def run(self):
        try:
            while not self.stopping.is_set():
                self.wake.wait()
                self.wake.clear()
                if self.stopping.is_set():
                    break
                with self.guard:
                    request, self.request = self.request, None
                if request is None:
                    continue
                result = self.scan(request)
                if self.stopping.is_set():
                    break
                try:
                    self.bridge.completed.emit(result)
                except RuntimeError:
                    # Its owning Qt window was disposed while an OS call finished.
                    break
        finally:
            self.system_source.close()

    def scan(self, request):
        started = time.perf_counter()
        generation, folders, tracked, system_directory = request
        candidates = {}
        for folder, source in folders:
            if self.stopping.is_set():
                break
            recent = []
            try:
                with os.scandir(folder) as files:
                    for entry in files:
                        if self.stopping.is_set():
                            break
                        row = _inspect(entry.path, source)
                        if row is not None:
                            value = (row["mtime_ns"], row["key"], row)
                            if len(recent) < SCAN_LIMIT:
                                heapq.heappush(recent, value)
                            elif value[:2] > recent[0][:2]:
                                heapq.heapreplace(recent, value)
                candidates.update((row["key"], row) for _, _, row in recent)
            except OSError:
                continue
        # Include manual/pinned/older recorded paths to notice disappearance;
        # this is a bounded metadata check, not a scan of their parent folders.
        for record in tracked:
            if self.stopping.is_set():
                break
            manual = record.get("manual", record.get("origin") == "manual")
            if record.get("origin") == "system_recent" and not record["pinned"] and not manual:
                continue
            origin = "manual" if manual else record.get("origin", "watch")
            row = _inspect(record["path"], "手动添加" if manual else record["source"], origin)
            if row is not None:
                if manual:
                    row["manual_seen_at"] = record.get("manual_seen_at", record["seen_at"])
                    row["_seen_at"] = row["manual_seen_at"]
                    row["manual"] = True
                elif origin == "system_recent":
                    row["recent_mtime_ns"] = record.get("recent_mtime_ns", 0)
                    row["_seen_at"] = record["seen_at"]
                if manual or row["key"] not in candidates:
                    candidates[row["key"]] = row
        tracked_by_key = {row["key"]: row for row in tracked}
        if system_directory:
            recent_rows = self.system_source.scan(system_directory, self.stopping)
        else:
            # A disabled source should release its native object/cache too.
            # This runs on the owning worker thread, just like initialization.
            self.system_source.close()
            recent_rows = []
        for recent in recent_rows:
            row = _inspect(recent["path"], "最近使用", "system_recent")
            if row is None:
                continue
            row["recent_mtime_ns"] = recent["recent_mtime_ns"]
            row["_seen_at"] = recent["used_at"]
            previous = tracked_by_key.get(row["key"])
            other = candidates.get(row["key"])
            if previous and previous.get("manual", previous.get("origin") == "manual"):
                row["manual"] = True
                row["manual_seen_at"] = previous.get("manual_seen_at", previous["seen_at"])
            # A deliberately picked-up file can be newer than its old system
            # Recent entry. Keep that explicit action at the top until reused.
            other_time = (other.get("_seen_at", 0) if other else 0)
            if previous and previous.get("origin") != "system_recent" and other:
                other_time = max(other_time, previous["seen_at"])
            if other_time > recent["used_at"]:
                continue
            candidates[row["key"]] = row
        present = set(candidates)
        # A history entry is an existing-file reference, not an import job. Do
        # not open the target or wait for an actively edited/played file to
        # become idle. Optional folder feeds retain their download-readiness
        # checks so incomplete arrivals do not appear as completed downloads.
        stable = {key: row for key, row in candidates.items() if row["origin"] == "system_recent"}
        pending_candidates = {key: row for key, row in candidates.items() if row["origin"] != "system_recent"}
        if pending_candidates and self.stopping.wait(STABILITY_SECONDS):
            return generation, {}, present, False, {}
        pending = False
        for key, before in pending_candidates.items():
            if self.stopping.is_set():
                break
            after = _inspect(before["path"], before["source"], before["origin"])
            if after is None:
                present.discard(key)
            elif [before["size"], before["mtime_ns"]] == [after["size"], after["mtime_ns"]] and _readable(after["path"]):
                stable[key] = before
            else:
                pending = True
        metrics = dict(self.system_source.metrics if system_directory else {})
        metrics.update(scan_ms=round((time.perf_counter() - started) * 1000, 3), candidates=len(candidates))
        return generation, stable, present, pending, metrics


class RecentFiles(QObject):
    """Recent external files, with independently persisted pins and dismissals.

    ``directories=None`` uses explicitly saved folders, or no directory feed.
    Windows Recent is the normal source; merely downloading/modifying a file
    does not add it to that history. ``directories=[]`` disables folder feeds.
    ``watch=False`` disables filesystem notifications, useful in isolated tests;
    initial discovery and explicit refresh() still run asynchronously.
    """

    changed = Signal()

    def __init__(self, vault, parent=None, directories=None, watch=True, *,
                 system_recent=None, recent_directory=None, resolver=None):
        super().__init__(parent)
        self.vault = vault
        self._stopped = False
        self._generation = 0
        self._seed_pass = True
        self._retry_rounds = 0
        self._scan_metrics = {}
        injected_recent = recent_directory is not None or resolver is not None
        # Every existing test/diagnostic uses watch=False; keep those isolated
        # from actual Recent Items unless explicitly enabled/injected.
        self._system_enabled = bool(vault.setting(SYSTEM_RECENT_KEY, True) if system_recent is None
                                    and (watch or injected_recent) else system_recent)
        self._recent_directory = (windows_recent.local_path(os.fspath(recent_directory)) if recent_directory is not None
                                  else windows_recent.recent_directory() if self._system_enabled else "")
        self._download_folder = _path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation))
        if directories is None:
            # Older versions implicitly scanned Downloads without saving this
            # setting. Only an explicit set_folders() wrote FOLDERS_KEY, so an
            # absent key safely migrates that old implicit feed to no folders
            # while preserving folders the user actually selected.
            directories = vault.setting(FOLDERS_KEY, [])
        self._folders = self._normalize_folders(directories)
        self._records, self._ignored = {}, {}
        # More files can be observed than retained on the shelf. Remember a
        # bounded fingerprint baseline so old candidates evicted by the shelf
        # limit do not acquire a new "seen now" timestamp on every refresh.
        self._observed = OrderedDict()
        self._load()
        # Entries discovered through a folder that is no longer selected do
        # not silently survive an explicit directory configuration change.
        if self._drop_unwatched():
            self._save()
        self._bridge = _Bridge(self)
        self._bridge.completed.connect(self._apply)
        self._worker = _Scanner(self._bridge, resolver)
        self._worker.start()
        self._watcher = QFileSystemWatcher(self)
        self._watch_enabled = bool(watch)
        self._watcher.directoryChanged.connect(self._folder_changed)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self.refresh)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.setInterval(RETRY_START_MS)
        self._retry.timeout.connect(self.refresh)
        self._sync_watcher()
        self.refresh()

    @staticmethod
    def _normalize_folders(paths):
        if not isinstance(paths, (list, tuple)):
            return []
        result, seen = [], set()
        for value in paths:
            path = _path(value)
            if not path or os.path.normcase(path) in seen:
                continue
            try:
                if Path(path).exists() and not Path(path).is_dir():
                    continue
            except OSError:
                continue
            seen.add(os.path.normcase(path))
            result.append(path)
            if len(result) >= MAX_FOLDERS:
                break
        return result

    def _load(self):
        state = self.vault.setting(STATE_KEY, {})
        self._last_saved_state = copy.deepcopy(state)
        if not isinstance(state, dict):
            return
        records = state.get("records", [])
        if isinstance(records, list):
            for record in records[:MAX_RECORDS]:
                if not isinstance(record, dict):
                    continue
                path = _path(record.get("path", ""))
                if not path or _temporary(path):
                    continue
                try:
                    seen_at = float(record["seen_at"])
                    size, mtime_ns = int(record["size"]), int(record["mtime_ns"])
                    if not math.isfinite(seen_at) or size < 0 or mtime_ns < 0:
                        continue
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                key = _key(path)
                self._records[key] = dict(key=key, path=path, name=Path(path).name,
                    source=str(record.get("source", "文件"))[:64], seen_at=seen_at,
                    pinned=bool(record.get("pinned", False)), exists=bool(record.get("exists", True)),
                    size=size, mtime_ns=mtime_ns,
                    origin=record.get("origin") if record.get("origin") in ("manual", "system_recent") else "watch")
                if record.get("origin") == "system_recent":
                    try:
                        self._records[key]["recent_mtime_ns"] = max(0, int(record.get("recent_mtime_ns", seen_at * 1e9)))
                    except (TypeError, ValueError, OverflowError):
                        self._records.pop(key)
                        continue
                if record.get("manual", record.get("origin") == "manual"):
                    self._records[key]["manual"] = True
                    try:
                        manual_seen = float(record.get("manual_seen_at", seen_at))
                    except (TypeError, ValueError, OverflowError):
                        manual_seen = seen_at
                    self._records[key]["manual_seen_at"] = manual_seen if math.isfinite(manual_seen) else seen_at
        ignored = state.get("ignored", {})
        if isinstance(ignored, dict):
            for key, value in list(ignored.items())[-MAX_IGNORED:]:
                if isinstance(key, str) and isinstance(value, dict):
                    signature = value.get("signature")
                    if (isinstance(signature, list) and len(signature) in (2, 3)
                            and all(isinstance(part, int) and part >= 0 for part in signature)):
                        try:
                            at = float(value.get("at", 0))
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if math.isfinite(at):
                            self._ignored[key] = dict(signature=signature, at=at)

    def _save(self):
        ordered = sorted(self._records.values(), key=lambda row: (row["pinned"], row["seen_at"]), reverse=True)
        self._records = {row["key"]: row for row in ordered[:MAX_RECORDS]}
        self._ignored = dict(sorted(self._ignored.items(), key=lambda item: item[1]["at"])[-MAX_IGNORED:])
        state = dict(records=list(self._records.values()), ignored=self._ignored)
        if state == self._last_saved_state:
            return False
        self.vault.set_setting(STATE_KEY, state)
        self._last_saved_state = copy.deepcopy(state)
        return True

    def _drop_unwatched(self):
        previous = len(self._records)
        allowed = {os.path.normcase(path) for path in self._folders}
        self._records = {key: row for key, row in self._records.items()
                         if row.get("manual", row.get("origin") == "manual") or row["pinned"]
                         or (self._system_enabled and row.get("origin") == "system_recent")
                         or os.path.normcase(str(Path(row["path"]).parent)) in allowed}
        return len(self._records) != previous

    def system_recent_enabled(self):
        return self._system_enabled

    def set_system_recent_enabled(self, enabled):
        if self._stopped or bool(enabled) == self._system_enabled:
            return
        self._system_enabled = bool(enabled)
        if self._system_enabled and not self._recent_directory:
            self._recent_directory = windows_recent.recent_directory()
        self.vault.set_setting(SYSTEM_RECENT_KEY, self._system_enabled)
        self._drop_unwatched()
        self._save()
        self.changed.emit()
        self.refresh()

    def scan_metrics(self):
        return dict(self._scan_metrics)

    def folders(self):
        return list(self._folders)

    def set_folders(self, paths):
        if self._stopped:
            return
        self._folders = self._normalize_folders(paths)
        self._drop_unwatched()
        self._seed_pass = True
        self._retry_rounds = 0
        self._observed.clear()
        self.vault.set_setting(FOLDERS_KEY, self._folders)
        self._save()
        self._sync_watcher()
        self.changed.emit()
        self.refresh()

    def _sync_watcher(self):
        if not self._watch_enabled:
            return
        current = self._watcher.directories()
        if current:
            self._watcher.removePaths(current)
        paths = [*self._folders, *([self._recent_directory] if self._system_enabled and self._recent_directory else [])]
        existing = list(dict.fromkeys(path for path in paths if os.path.isdir(path)))
        if existing:
            self._watcher.addPaths(existing)

    def _folder_changed(self, _path):
        self._retry_rounds = 0
        self._debounce.start()

    def refresh(self):
        if self._stopped:
            return
        self._generation += 1
        self._sync_watcher()
        folders = [(path, "下载" if os.path.normcase(path) == os.path.normcase(self._download_folder)
                    else Path(path).name or path) for path in self._folders]
        self._worker.submit((self._generation, folders, [dict(row) for row in self._records.values()],
                             self._recent_directory if self._system_enabled else ""))

    def _apply(self, result):
        generation, stable, present, pending, metrics = result
        if self._stopped or generation != self._generation:
            return
        changed = False
        self._scan_metrics = metrics
        now = time.time()
        for key, candidate in stable.items():
            signature = _signature(candidate)
            observed_unchanged = self._observed.get(key) == signature
            self._observed[key] = signature
            self._observed.move_to_end(key)
            ignored = self._ignored.get(key)
            if ignored and ignored["signature"] == signature:
                continue
            if ignored:
                self._ignored.pop(key, None)
                changed = True
            old = self._records.get(key)
            if "_seen_at" in candidate:
                seen_at = candidate["_seen_at"]
            elif old and old.get("origin") == "system_recent":
                seen_at = candidate["mtime"]
            elif old and _signature(old) == signature:
                seen_at = old["seen_at"]
            else:
                seen_at = candidate["mtime"] if self._seed_pass or observed_unchanged else now
            record = {field: candidate[field] for field in ("key", "path", "name", "source", "size", "mtime_ns", "origin")}
            record.update(seen_at=seen_at, pinned=bool(old and old["pinned"]), exists=True)
            for field in ("recent_mtime_ns", "manual", "manual_seen_at"):
                if field in candidate:
                    record[field] = candidate[field]
            if record != old:
                self._records[key] = record
                changed = True
        for key, row in list(self._records.items()):
            if (key not in present and row.get("origin") == "system_recent" and not row["pinned"]
                    and not row.get("manual")):
                self._records.pop(key)
                changed = True
                continue
            if key not in present and row["exists"]:
                row["exists"] = False
                changed = True
        self._seed_pass = False
        while len(self._observed) > MAX_OBSERVED:
            self._observed.popitem(last=False)
        if changed:
            if self._save():
                self.changed.emit()
        if pending:
            self._retry_rounds += 1
            self._retry.setInterval(min(RETRY_MAX_MS, RETRY_START_MS * (2 ** min(self._retry_rounds - 1, 5))))
            self._retry.start()
        else:
            self._retry_rounds = 0
            self._retry.stop()

    def entries(self, search="", limit=60):
        terms = str(search).casefold().split()
        rows = sorted(self._records.values(), key=lambda row: (row["pinned"], row["seen_at"]), reverse=True)
        if terms:
            rows = [row for row in rows if all(term in (row["name"] + " " + row["source"] + " " + row["path"]).casefold()
                                               for term in terms)]
        return [dict(row) for row in rows[:max(0, min(int(limit), MAX_RECORDS))]]

    def add_paths(self, paths):
        if self._stopped:
            return 0
        count, now = 0, time.time()
        for value in paths:
            path = _path(value)
            if not path:
                continue
            row = _inspect(path, "手动添加", "manual")
            if row is None or not _readable(path):
                continue
            old = self._records.get(row["key"])
            self._ignored.pop(row["key"], None)
            row.pop("mtime", None)
            self._records[row["key"]] = dict(row, seen_at=now, pinned=bool(old and old["pinned"]), exists=True,
                                             manual=True, manual_seen_at=now)
            count += 1
        if count:
            self._save()
            self.changed.emit()
            self.refresh()
        return count

    def set_pinned(self, key, value):
        row = self._records.get(key)
        if self._stopped or row is None:
            return False
        pinned = bool(value)
        if pinned and not row["pinned"] and sum(record["pinned"] for record in self._records.values()) >= MAX_PINNED:
            return False
        if row["pinned"] != pinned:
            row["pinned"] = pinned
            self._save()
            self.changed.emit()
        return True

    def remove(self, key):
        if self._stopped:
            return False
        row = self._records.pop(key, None)
        if row is None:
            return False
        self._ignored[key] = dict(signature=_signature(row), at=time.time())
        self._save()
        self.changed.emit()
        return True

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._debounce.stop()
        self._retry.stop()
        watched = self._watcher.directories()
        if watched:
            self._watcher.removePaths(watched)
        self._worker.stop()
        self._worker.join(timeout=1.0)
