"""A bounded, persistent basket of local file references.

Only paths are saved. Files are never opened, copied, decoded, moved or deleted;
even a multi-gigabyte video costs one reference and a few metadata queries.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from PySide6.QtCore import QObject, Signal

from .windows_recent import local_path


SETTING_KEY = "file_basket_v1"
MAX_FILES = 200
_UNAVAILABLE_ATTRIBUTES = 0x400 | 0x1000 | 0x40000 | 0x400000


@dataclass(frozen=True)
class BasketAddResult:
    added: int = 0
    duplicates: int = 0
    skipped: int = 0
    full: int = 0


def _canonical(value):
    """Validate spelling before any filesystem access, including load time."""
    try:
        raw = os.fspath(value)
    except TypeError:
        return ""
    if (not isinstance(raw, str) or not raw.strip() or "\0" in raw
            or raw.startswith(("\\\\", "//")) or "://" in raw
            or not os.path.isabs(raw)):
        return ""
    path = os.path.normpath(raw)
    if os.name == "nt":
        drive, tail = os.path.splitdrive(path)
        if len(drive) != 2 or drive[1] != ":" or ":" in tail:
            return ""
    return path


def _key(path):
    return hashlib.sha256(os.path.normcase(path).casefold().encode("utf-8")).hexdigest()


def _metadata(path):
    """Do not follow reparse points, offline placeholders or remote drives."""
    try:
        if not local_path(path):
            return None
        # Check ancestors before the leaf so a junction cannot send the leaf
        # stat to a network share or recall an offline placeholder.
        value = Path(path)
        for parent in reversed(value.parents):
            info = os.stat(parent, follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & _UNAVAILABLE_ATTRIBUTES):
                return None
        info = os.stat(value, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & _UNAVAILABLE_ATTRIBUTES):
            return None
        return info
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _values(value):
    if isinstance(value, (str, bytes, os.PathLike)) or value is None:
        return (value,)
    try:
        return iter(value)
    except TypeError:
        return (value,)


class FileBasket(QObject):
    """Insertion-ordered references, with persistence preceding notification."""

    changed = Signal()
    LIMIT = MAX_FILES

    def __init__(self, vault, parent=None, *, limit=MAX_FILES):
        super().__init__(parent)
        self.vault = vault
        self.limit = max(1, min(MAX_FILES, int(limit)))
        self._paths = []
        state = vault.setting(SETTING_KEY, {})
        if not isinstance(state, dict) or state.get("version") != 1:
            return
        saved = state.get("paths", [])
        if not isinstance(saved, list):
            return
        seen = set()
        for value in saved:
            path = _canonical(value)
            key = _key(path) if path else ""
            if path and key not in seen:
                self._paths.append(path)
                seen.add(key)
                if len(self._paths) == self.limit:
                    break

    @property
    def count(self):
        return len(self._paths)

    def _commit(self, paths):
        # Pass a distinct list to the writer so a retaining/mock writer cannot
        # mutate the in-memory state after returning.
        self.vault.set_setting(SETTING_KEY, {"version": 1, "paths": list(paths)})
        self._paths = list(paths)
        self.changed.emit()

    def add_paths(self, paths):
        next_paths = list(self._paths)
        seen = {_key(path) for path in next_paths}
        added = duplicates = skipped = full = 0
        for value in _values(paths):
            path = _canonical(value)
            if not path:
                skipped += 1
                continue
            key = _key(path)
            if key in seen:
                duplicates += 1
                continue
            if _metadata(path) is None:
                skipped += 1
                continue
            seen.add(key)
            if len(next_paths) >= self.limit:
                full += 1
                continue
            next_paths.append(path)
            added += 1
        if added:
            self._commit(next_paths)
        return BasketAddResult(added, duplicates, skipped, full)

    def entries(self, text=""):
        needle = str(text or "").strip().casefold()
        rows = []
        for path in self._paths:
            name = Path(path).name
            if needle and needle not in (name + "\n" + path).casefold():
                continue
            info = _metadata(path)
            rows.append(dict(key=_key(path), path=path, name=name, exists=info is not None,
                             size=info.st_size if info is not None else 0,
                             mtime_ns=info.st_mtime_ns if info is not None else 0))
        return rows

    def remove(self, keys):
        selected = set(_values(keys))
        remaining = [path for path in self._paths if _key(path) not in selected]
        removed = len(self._paths) - len(remaining)
        if removed:
            self._commit(remaining)
        return removed

    def clear(self):
        removed = len(self._paths)
        if removed:
            self._commit([])
        return removed

    def existing_paths(self, keys=None):
        selected = None if keys is None else set(_values(keys))
        return [Path(path) for path in self._paths
                if (selected is None or _key(path) in selected) and _metadata(path) is not None]
