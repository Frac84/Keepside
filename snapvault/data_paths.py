"""Resolve a user-selected vault independently of a portable program folder.

The publisher writes a per-user path binding, never user data, into LocalAppData.
Opening a new program version cannot silently replace that vault with an empty
neighboring ``data`` folder. Explicit --data-dir always wins.
"""
from __future__ import annotations
from .i18n import tr

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import uuid


class DataLocationError(ValueError):
    pass


@dataclass(frozen=True)
class DataLocation:
    directory: Path
    source: str
    local_conflict: Path | None = None


def default_binding_file():
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / "AppData" / "Local"
    return root / "SnapVault" / "profile.json"


def _path(value):
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DataLocationError(tr('资料库路径无效，请使用 --data-dir 指定正确位置。')) from exc


def _check_bound_vault(directory):
    database = directory / "vault.sqlite3"
    if not directory.is_dir() or not database.is_file():
        raise DataLocationError(tr('原资料库暂时不可用：{v0}\n请连接原磁盘或使用 --data-dir 指定资料库；没有创建新的空资料库。', v0=directory))
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("integrity check failed")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screenshots'").fetchone():
                raise sqlite3.DatabaseError("not a vault database")
    except (OSError, sqlite3.Error) as exc:
        raise DataLocationError(tr('无法读取原资料库：{v0}\n原资料未被覆盖，请先检查磁盘或恢复备份。', v0=directory)) from exc


def has_local_data(directory):
    """Conservative conflict detection; no image/text bodies are inspected."""
    database = directory / "vault.sqlite3"
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3) as db:
            if db.execute("SELECT EXISTS(SELECT 1 FROM screenshots)").fetchone()[0]:
                return True
            # Preferences and recent-file paths can matter even in a zero-image vault.
            return bool(db.execute("SELECT EXISTS(SELECT 1 FROM settings)").fetchone()[0])
    except (OSError, sqlite3.Error):
        # An unreadable local database must never be silently discarded.
        return True


def resolve_data_location(base, explicit=None, binding_file=None):
    if explicit is not None:
        return DataLocation(_path(explicit), "explicit")
    local = _path(base) / "data"
    binding = Path(binding_file) if binding_file is not None else default_binding_file()
    if not binding.exists():
        return DataLocation(local, "portable")
    try:
        content = json.loads(binding.read_text(encoding="utf-8-sig"))
        value = content["data_dir"]
        if content.get("version") != 1 or not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
            raise ValueError("invalid binding")
        directory = _path(value)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DataLocationError(tr('资料库连接配置损坏，已停止创建空资料库。请使用 --data-dir 指定原资料库。')) from exc
    _check_bound_vault(directory)
    conflict = local if local != directory and has_local_data(local) else None
    return DataLocation(directory, "bound", conflict)


def bind_data_directory(directory, binding_file=None):
    """Explicit native-app/publisher operation; never called by path resolution."""
    directory = _path(directory)
    _check_bound_vault(directory)
    binding = Path(binding_file) if binding_file is not None else default_binding_file()
    binding.parent.mkdir(parents=True, exist_ok=True)
    temporary = binding.with_name(binding.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps({"version": 1, "data_dir": str(directory)}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(binding)
    finally:
        temporary.unlink(missing_ok=True)
    return binding
