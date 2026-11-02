"""Release preparation and verification operate on synthetic vaults only."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest


_spec = importlib.util.spec_from_file_location(
    "publish_release", Path(__file__).parents[1] / "tools" / "publish_release.py")
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)


def vault(path):
    (path / "images").mkdir(parents=True)
    (path / "images" / "sample.png").write_bytes(b"synthetic original")
    with sqlite3.connect(path / "vault.sqlite3") as db:
        db.executescript("""
            CREATE TABLE screenshots(id TEXT PRIMARY KEY, filename TEXT, item_type TEXT, content TEXT);
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO screenshots VALUES ('image-1','sample.png','image','');
            INSERT INTO screenshots VALUES ('text-1','','text','synthetic private text');
            INSERT INTO settings VALUES ('recent_files_state_v1','synthetic recent paths');
        """)
    return path


def test_release_backup_is_consistent_and_keeps_originals_in_place(tmp_path):
    data = vault(tmp_path / "data")
    state = tmp_path / "rollback"
    result = publish.prepare(data, state)
    assert result["ok"] and result["records"] == 2 and result["images"] == 1
    with sqlite3.connect(state / "vault-before.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT count(*) FROM screenshots").fetchone() == (2,)
    assert (data / "images" / "sample.png").read_bytes() == b"synthetic original"
    assert not (state / "images").exists(), "Publication need not duplicate original media"
    baseline = (state / "vault-baseline.json").read_text(encoding="utf-8")
    assert "synthetic private text" not in baseline and "synthetic recent paths" not in baseline
    assert publish.verify(data, state)["settings_unchanged"]


@pytest.mark.parametrize("statement", [
    "UPDATE screenshots SET content='changed' WHERE id='text-1'",
    "UPDATE settings SET value='changed' WHERE key='recent_files_state_v1'",
    "DELETE FROM screenshots WHERE id='text-1'",
])
def test_verification_rejects_changed_records_or_settings(tmp_path, statement):
    data = vault(tmp_path / "data")
    state = tmp_path / "rollback"
    publish.prepare(data, state)
    with sqlite3.connect(data / "vault.sqlite3") as db:
        db.execute(statement)
    with pytest.raises(ValueError, match="changed during publishing"):
        publish.verify(data, state)


@pytest.mark.parametrize("filename", ["missing.png", "../outside.png"])
def test_backup_requires_original_images_inside_the_image_directory(tmp_path, filename):
    data = vault(tmp_path / "data")
    (data / "outside.png").write_bytes(b"not an image original")
    with sqlite3.connect(data / "vault.sqlite3") as db:
        db.execute("UPDATE screenshots SET filename=? WHERE id='image-1'", (filename,))
    with pytest.raises(ValueError, match="Original image validation failed"):
        publish.prepare(data, tmp_path / "rollback")
    assert not (tmp_path / "rollback" / "vault-before.sqlite3").exists()


def test_existing_backup_is_never_overwritten(tmp_path):
    data = vault(tmp_path / "data")
    state = tmp_path / "rollback"
    publish.prepare(data, state)
    original = (state / "vault-before.sqlite3").read_bytes()
    with pytest.raises(FileExistsError):
        publish.prepare(data, state)
    assert (state / "vault-before.sqlite3").read_bytes() == original


def test_verification_rejects_changed_original_bytes_with_same_filename_and_size(tmp_path):
    data = vault(tmp_path / "data")
    state = tmp_path / "rollback"
    publish.prepare(data, state)
    image = data / "images" / "sample.png"
    old_size = image.stat().st_size
    image.write_bytes(b"modified original!")
    assert image.stat().st_size == old_size
    with pytest.raises(ValueError, match="changed during publishing"):
        publish.verify(data, state)
