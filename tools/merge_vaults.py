"""Merge portable vaults while both applications are closed.

The target's existing rows and settings always win hash duplicates. The source
is kept untouched, including duplicate metadata, and both databases are backed
up consistently before any changes. Only newly imported items copy auxiliaries.
No content or individual file names are printed by this tool.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3
import uuid


class MergeError(ValueError):
    pass


AUXILIARY = ("image_features", "ocr_layouts", "item_tags", "classification_corrections")


def _readonly(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)


def _check(db):
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise MergeError("Database integrity check failed")


def _backup(path, destination):
    if destination.exists():
        raise FileExistsError("Backup already exists")
    with closing(_readonly(path)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        _check(target)


def _columns(db, table):
    # Identifiers are constants from this module, never user supplied.
    return tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))


def _logical_digest(db):
    result = hashlib.sha256()
    for table in ("screenshots", *AUXILIARY, "settings"):
        result.update(table.encode("ascii"))
        for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            result.update(repr(tuple(row)).encode("utf-8"))
    return result.hexdigest()


def _digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _flat_file(directory, name):
    if (not isinstance(name, str) or not name or name in (".", "..")
            or Path(name).name != name or PureWindowsPath(name).name != name
            or PureWindowsPath(name).drive or ":" in name):
        raise MergeError("Unsafe stored file name")
    path = directory / name
    if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
        raise MergeError("Stored file escapes its directory")
    return path


def _copy_new(source, target, created):
    # Exclusive creation protects any existing bytes, even on name collisions.
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        created.append(target)
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
    if _digest(source) != _digest(target):
        raise MergeError("Copied image byte verification failed")


def _grams(text):
    tokens = {}
    for run in re.findall(r"[^\W_]+", text):
        for index, character in enumerate(run):
            tokens[character] = None
            if index + 1 < len(run):
                tokens[run[index:index + 2]] = None
    return " ".join(tokens)


def _insert(db, table, values):
    columns = tuple(values)
    quoted = ",".join('"' + value + '"' for value in columns)
    return db.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in columns)})',
                      tuple(values.values()))


def merge_vaults(source, target, backup_dir, report=None):
    source, target, backup_dir = (Path(value).resolve() for value in (source, target, backup_dir))
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise MergeError("Source and target must be separate vault directories")
    for root in (source, target):
        if not (root / "vault.sqlite3").is_file():
            raise MergeError("An existing vault database is required")
        if root.is_symlink() or (root / "images").is_symlink() or (root / "thumbnails").is_symlink():
            raise MergeError("Vault directories must not be links")
    if backup_dir.is_relative_to(source) or backup_dir.is_relative_to(target):
        raise MergeError("Keep merge backups outside both vaults")
    if report is not None:
        report = Path(report).resolve()
        if report.is_relative_to(source) or report.is_relative_to(target):
            raise MergeError("Keep merge reports outside both vaults")
        if report.exists():
            raise FileExistsError("Merge report already exists")
        report.parent.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=False)
    _backup(source / "vault.sqlite3", backup_dir / "source-before.sqlite3")
    _backup(target / "vault.sqlite3", backup_dir / "target-before.sqlite3")
    created = []
    copied_images = copied_thumbnails = imported = duplicates = 0
    source_originals = {}
    with closing(_readonly(backup_dir / "source-before.sqlite3")) as incoming, closing(
            sqlite3.connect(target / "vault.sqlite3", timeout=15)) as destination:
        incoming.row_factory = sqlite3.Row
        destination.row_factory = sqlite3.Row
        incoming_columns = _columns(incoming, "screenshots")
        destination_columns = _columns(destination, "screenshots")
        if incoming_columns != destination_columns or not incoming_columns:
            raise MergeError("Vault schemas differ; update both schemas before merging")
        for table in AUXILIARY:
            if not _columns(incoming, table) or _columns(incoming, table) != _columns(destination, table):
                raise MergeError("Auxiliary schemas differ")
        incoming_digest = _logical_digest(incoming)
        with closing(_readonly(backup_dir / "target-before.sqlite3")) as target_backup:
            target_digest = _logical_digest(target_backup)
        settings_before = list(destination.execute("SELECT key,value FROM settings ORDER BY key"))
        before = destination.execute("SELECT count(*) FROM screenshots").fetchone()[0]
        # Preflight all source originals, even a hash-duplicate with corrupt paths.
        for row in incoming.execute("SELECT id,filename FROM screenshots WHERE item_type='image'"):
            original = _flat_file(source / "images", row["filename"])
            thumb = _flat_file(source / "thumbnails", row["id"] + ".png")
            if not original.is_file():
                raise MergeError("Source original image is missing")
            source_originals[row["id"]] = (_digest(original), _digest(thumb) if thumb.is_file() else None)
        destination.execute("BEGIN IMMEDIATE")
        try:
            if _logical_digest(destination) != target_digest:
                raise MergeError("Target changed after backup; close both applications and retry")
            used_names = {row[0] for row in destination.execute("SELECT filename FROM screenshots WHERE item_type='image'")}
            for row in incoming.execute("SELECT * FROM screenshots ORDER BY rowid"):
                if destination.execute("SELECT 1 FROM screenshots WHERE hash=?", (row["hash"],)).fetchone():
                    duplicates += 1
                    continue
                values = dict(row)
                old_id = values["id"]
                new_id = old_id
                # Also reserve thumbnail names; never replace an orphan thumbnail.
                _flat_file(target / "thumbnails", old_id + ".png")
                while (destination.execute("SELECT 1 FROM screenshots WHERE id=?", (new_id,)).fetchone()
                       or (target / "thumbnails" / (new_id + ".png")).exists()):
                    new_id = uuid.uuid4().hex
                values["id"] = new_id
                if row["item_type"] == "image":
                    original = _flat_file(source / "images", row["filename"])
                    name = row["filename"]
                    while name in used_names or _flat_file(target / "images", name).exists():
                        name = "merged_" + uuid.uuid4().hex + original.suffix
                    values["filename"] = name
                    _copy_new(original, _flat_file(target / "images", name), created)
                    copied_images += 1
                    used_names.add(name)
                    thumb = _flat_file(source / "thumbnails", old_id + ".png")
                    if thumb.is_file():
                        _copy_new(thumb, _flat_file(target / "thumbnails", new_id + ".png"), created)
                        copied_thumbnails += 1
                cursor = _insert(destination, "screenshots", values)
                destination.execute("INSERT INTO search_grams(rowid,grams) VALUES (?,?)",
                                    (cursor.lastrowid, _grams(values["search_text"])))
                for table in AUXILIARY:
                    for auxiliary_row in incoming.execute(f'SELECT * FROM "{table}" WHERE item_id=?', (old_id,)):
                        auxiliary_values = dict(auxiliary_row)
                        auxiliary_values["item_id"] = new_id
                        if table == "classification_corrections":
                            auxiliary_values.pop("id")
                        _insert(destination, table, auxiliary_values)
                imported += 1
            _check(destination)
            if list(destination.execute("SELECT key,value FROM settings ORDER BY key")) != settings_before:
                raise MergeError("Target preferences changed during merge")
            if destination.execute("SELECT count(*) FROM screenshots").fetchone()[0] != before + imported:
                raise MergeError("Merged record count mismatch")
            # Source bytes are immutable throughout; concurrent applications must
            # remain closed. The source database itself has never been writable.
            for key, (original_hash, thumb_hash) in source_originals.items():
                filename = incoming.execute("SELECT filename FROM screenshots WHERE id=?", (key,)).fetchone()[0]
                if _digest(_flat_file(source / "images", filename)) != original_hash:
                    raise MergeError("Source image changed during merge")
                if thumb_hash and _digest(_flat_file(source / "thumbnails", key + ".png")) != thumb_hash:
                    raise MergeError("Source thumbnail changed during merge")
            with closing(_readonly(source / "vault.sqlite3")) as current_source:
                if _logical_digest(current_source) != incoming_digest:
                    raise MergeError("Source changed during merge; close both applications and retry")
            destination.commit()
        except BaseException:
            destination.rollback()
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
    result = dict(ok=True, target_before=before, imported=imported, duplicates=duplicates,
                  target_after=before + imported, copied_images=copied_images,
                  copied_thumbnails=copied_thumbnails, source_unchanged=True,
                  target_settings_unchanged=True, integrity="ok")
    if report is not None:
        with report.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source", "target", "backup-dir", "report"):
        parser.add_argument("--" + flag, required=True, type=Path)
    args = parser.parse_args()
    result = merge_vaults(args.source, args.target, args.backup_dir, args.report)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
