from pathlib import Path
import sqlite3

from PIL import Image
import pytest

from snapvault.storage import Vault
from tools import merge_vaults as merge


@pytest.fixture
def vaults(tmp_path):
    source, target = Vault(tmp_path / "source"), Vault(tmp_path / "target")
    return source, target


def rows(vault, table):
    with sqlite3.connect(vault.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def files(vault):
    return {str(file.relative_to(vault.root)): file.read_bytes()
            for directory in (vault.images, vault.thumbs) for file in directory.iterdir()}


def run(source, target, tmp_path, name="backup"):
    return merge.merge_vaults(source.root, target.root, tmp_path / name, tmp_path / (name + ".json"))


def test_all_metadata_original_bytes_auxiliaries_search_settings_and_idempotence(vaults, tmp_path):
    source, target = vaults
    key, _ = source.add(Image.new("RGBA", (41, 31), (12, 123, 51, 78)))
    source.finish_ocr(key, "合并图像 OCR 搜索测试 xyzmerge", lines=[{"text": "合并图像", "box": [1, 2, 3, 4]}], codes=["qr-test"])
    source.set_tags(key, ["tech"])
    with source.connect() as db:
        db.execute("UPDATE screenshots SET favorite=1,title='Saved title',captured_at='2024-02-03T04:05:06+00:00' WHERE id=?", (key,))
    source.add_text("原样保留 code\n    x = 1")
    source.add_text("https://example.com/a?x=1#exact")
    source.set_setting("theme", "source-theme")
    source.set_setting("recent_files_locked_v1", False)
    target.add_text("Target original")
    target.set_setting("theme", "target-theme")
    target.set_setting("recent_files_locked_v1", True)
    source_before = {table: rows(source, table) for table in ("screenshots", "image_features", "ocr_layouts", "item_tags", "classification_corrections", "settings")}
    target_settings = rows(target, "settings")
    image_bytes = files(source)
    result = run(source, target, tmp_path)
    assert result["target_before"] == 1 and result["target_after"] == 4
    assert result["imported"] == 3 and result["copied_images"] == result["copied_thumbnails"] == 1
    imported = {row["id"]: row for row in rows(target, "screenshots")}
    for row in source_before["screenshots"]:
        assert imported[row["id"]] == row
    for table in ("image_features", "ocr_layouts", "item_tags", "classification_corrections"):
        actual = rows(target, table)
        for expected in source_before[table]:
            if table == "classification_corrections":
                expected = {key: value for key, value in expected.items() if key != "id"}
                assert expected in [{key: value for key, value in row.items() if key != "id"} for row in actual]
            else:
                assert expected in actual
    assert files(target) == image_bytes
    assert rows(target, "settings") == target_settings
    for table, before in source_before.items():
        assert rows(source, table) == before
    assert files(source) == image_bytes
    with sqlite3.connect(target.db_path) as db:
        assert db.execute("SELECT count(*) FROM search_index WHERE search_index MATCH 'xyzmerge'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM search_grams WHERE search_grams MATCH '合并'").fetchone()[0] == 1
    again = run(source, target, tmp_path, "backup-again")
    assert again["imported"] == 0 and again["duplicates"] == 3
    assert again["target_after"] == 4 and files(target) == image_bytes
    assert rows(target, "settings") == target_settings
    for name, expected_count in (("source-before.sqlite3", 3), ("target-before.sqlite3", 1)):
        with sqlite3.connect(tmp_path / "backup" / name) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT count(*) FROM screenshots").fetchone()[0] == expected_count


def test_hash_duplicate_keeps_existing_target_metadata(vaults, tmp_path):
    source, target = vaults
    source.add_text("same exact text")
    key, _ = target.add_text("same exact text")
    with target.connect() as db:
        db.execute("UPDATE screenshots SET title='Original target',favorite=1 WHERE id=?", (key,))
    before = rows(target, "screenshots")
    result = run(source, target, tmp_path)
    assert result["duplicates"] == 1 and result["imported"] == 0
    assert rows(target, "screenshots") == before


def test_all_existing_target_rows_auxiliaries_and_image_bytes_unchanged(vaults, tmp_path):
    source, target = vaults
    source.add(Image.new("RGB", (45, 35), "red"))
    target_id, _ = target.add(Image.new("RGB", (40, 30), "blue"))
    target.finish_ocr(target_id, "Target OCR exact", lines=[{"text": "Target", "box": [0, 0, 20, 10]}], codes=["target-qr"])
    target.set_tags(target_id, ["tech"])
    target.add_text("Target exact text\n   spacing")
    with target.connect() as db:
        db.execute("UPDATE screenshots SET favorite=1 WHERE id=?", (target_id,))
    before_rows = {table: rows(target, table) for table in ("screenshots", "image_features", "ocr_layouts", "item_tags", "classification_corrections", "settings")}
    before_files = files(target)
    run(source, target, tmp_path)
    for table, previous in before_rows.items():
        current = rows(target, table)
        for row in previous:
            assert row in current
    for name, contents in before_files.items():
        assert (target.root / name).read_bytes() == contents


def test_id_and_file_collisions_are_remapped_without_overwriting(vaults, tmp_path):
    source, target = vaults
    source_id, _ = source.add(Image.new("RGB", (50, 40), "red"))
    target_id, _ = target.add(Image.new("RGB", (50, 40), "blue"))
    source_row, target_row = rows(source, "screenshots")[0], rows(target, "screenshots")[0]
    source_bytes = (source.images / source_row["filename"]).read_bytes()
    (source.images / source_row["filename"]).rename(source.images / target_row["filename"])
    (source.thumbs / (source_id + ".png")).rename(source.thumbs / (target_id + ".png"))
    with sqlite3.connect(source.db_path) as db:
        db.execute("UPDATE screenshots SET id=?,filename=? WHERE id=?", (target_id, target_row["filename"], source_id))
        for table in ("image_features", "ocr_layouts", "item_tags", "classification_corrections"):
            db.execute(f'UPDATE "{table}" SET item_id=? WHERE item_id=?', (target_id, source_id))
    before = files(target)
    run(source, target, tmp_path)
    current = rows(target, "screenshots")
    imported = next(row for row in current if row["hash"] == source_row["hash"])
    assert imported["id"] != target_id
    assert imported["filename"] != target_row["filename"]
    assert (target.images / imported["filename"]).read_bytes() == source_bytes
    assert (target.thumbs / (imported["id"] + ".png")).is_file()
    for name, data in before.items():
        assert (target.root / name).read_bytes() == data
    assert rows(target, "image_features")[-1]["item_id"] == imported["id"]


def test_rollback_removes_only_new_files_and_reverts_rows(vaults, tmp_path, monkeypatch):
    source, target = vaults
    source.add(Image.new("RGB", (40, 30), "red"))
    target.add(Image.new("RGB", (40, 30), "blue"))
    before_rows, before_files = rows(target, "screenshots"), files(target)
    actual_insert = merge._insert

    def fail_aux(db, table, values):
        if table == "image_features":
            raise RuntimeError("injected auxiliary failure")
        return actual_insert(db, table, values)

    monkeypatch.setattr(merge, "_insert", fail_aux)
    with pytest.raises(RuntimeError, match="injected"):
        run(source, target, tmp_path)
    assert rows(target, "screenshots") == before_rows
    assert files(target) == before_files
    with sqlite3.connect(target.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("name", ["../outside.png", "..\\outside.png", "C:\\secret.png", "image.png:secret", "/tmp/file.png"])
def test_malicious_stored_image_paths_fail_before_changes(vaults, tmp_path, name):
    source, target = vaults
    key, _ = source.add(Image.new("RGB", (20, 30), "red"))
    with source.connect() as db:
        db.execute("UPDATE screenshots SET filename=? WHERE id=?", (name, key))
    with pytest.raises(merge.MergeError, match="Unsafe"):
        run(source, target, tmp_path)
    assert rows(target, "screenshots") == [] and files(target) == {}


def test_missing_original_fails_without_deleting_source(vaults, tmp_path):
    source, target = vaults
    source.add(Image.new("RGB", (20, 30), "red"))
    image = next(source.images.iterdir())
    image.unlink()
    with pytest.raises(merge.MergeError, match="missing"):
        run(source, target, tmp_path)
    assert len(rows(source, "screenshots")) == 1
    assert rows(target, "screenshots") == [] and files(target) == {}


def test_backup_never_reused_and_identical_vault_rejected(vaults, tmp_path):
    source, target = vaults
    with pytest.raises(merge.MergeError, match="separate"):
        merge.merge_vaults(source.root, source.root, tmp_path / "backup")
    run(source, target, tmp_path)
    with pytest.raises(FileExistsError):
        run(source, target, tmp_path)
