"""Read-only release validation plus an SQLite-consistent vault backup.

Called by Publish.ps1 after the old program has exited. No clipboard, biometric
API, browser, image decoding or real file import is involved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def snapshot(directory):
    directory = Path(directory).resolve()
    database = directory / "vault.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Vault database integrity check failed")
        count = db.execute("SELECT count(*) FROM screenshots").fetchone()[0]
        records_digest = hashlib.sha256()
        for row in db.execute("SELECT * FROM screenshots ORDER BY id"):
            # Only the digest is retained; recognized/private content is not
            # written into the publication report or baseline metadata.
            records_digest.update(repr(tuple(row)).encode("utf-8"))
            records_digest.update(b"\n")
        images = dict(db.execute("SELECT id,filename FROM screenshots WHERE item_type='image'"))
        settings = list(db.execute("SELECT key,value FROM settings ORDER BY key"))
    image_root = (directory / "images").resolve()
    missing = []
    image_digests = {}
    for key, name in images.items():
        path = (image_root / name).resolve()
        if not path.is_relative_to(image_root) or not path.is_file():
            missing.append(key)
            continue
        digest = hashlib.sha256()
        with path.open("rb") as original:
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                digest.update(chunk)
        image_digests[key] = digest.hexdigest()
    if missing:
        raise ValueError(f"Original image validation failed: {len(missing)} missing or invalid paths")
    settings_digest = hashlib.sha256(json.dumps(settings, ensure_ascii=False).encode("utf-8")).hexdigest()
    return dict(records=count, records_digest=records_digest.hexdigest(),
                images=images, image_digests=image_digests,
                settings_digest=settings_digest)


def prepare(directory, state_directory):
    directory, state_directory = Path(directory).resolve(), Path(state_directory).resolve()
    state_directory.mkdir(parents=True, exist_ok=True)
    baseline_path = state_directory / "vault-baseline.json"
    backup_path = state_directory / "vault-before.sqlite3"
    if baseline_path.exists() or backup_path.exists():
        raise FileExistsError("The release backup directory has already been used")
    baseline = snapshot(directory)
    with sqlite3.connect((directory / "vault.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=15) as db:
        with sqlite3.connect(backup_path) as backup:
            db.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    return dict(ok=True, records=baseline["records"], images=len(baseline["images"]),
                backup_bytes=backup_path.stat().st_size, integrity="ok")


def verify(directory, state_directory):
    baseline = json.loads((Path(state_directory) / "vault-baseline.json").read_text(encoding="utf-8"))
    current = snapshot(directory)
    if current != baseline:
        raise ValueError("Vault records, original images or settings changed during publishing")
    return dict(ok=True, records=current["records"], images=len(current["images"]),
                missing_original_images=0, settings_unchanged=True, integrity="ok")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "verify"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    result = (prepare if args.action == "prepare" else verify)(args.data_dir, args.state_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
