"""1.5.0 storage: short-term index, relevance, OCR layout, upgrade backup, locking."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading

from PIL import Image
import pytest

from snapvault import storage
from snapvault.storage import Vault, search_grams


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault")


def picture(color="red", size=(120, 80)):
    return Image.new("RGBA", size, color)


def ids(rows):
    return [row["id"] for row in rows]


def test_grams_cover_every_one_and_two_character_substring_of_word_runs():
    grams = set(search_grams("订单号: ab-12").split())
    assert {"订", "单", "号", "订单", "单号", "a", "b", "ab", "1", "2", "12"} <= grams
    assert "号:" not in grams and "b-" not in grams


def test_short_terms_use_the_index_and_still_match_exactly(vault, monkeypatch):
    order, _ = vault.add_text("京东订单 已发货")
    note, _ = vault.add_text("预订酒店")
    other, _ = vault.add_text("完全无关的内容")
    image, _ = vault.add(picture())
    vault.finish_ocr(image, "快递单号查询")
    assert set(ids(vault.query("订"))) == {order, note}
    assert ids(vault.query("单号")) == [image]
    assert set(ids(vault.query("单"))) == {order, image}
    assert ids(vault.query("订 发货")) == [order]
    assert ids(vault.query("号查")) == [image]
    # Terms with punctuation cannot use the index and fall back to exact scans.
    sharp, _ = vault.add_text("C# 与 .NET")
    assert ids(vault.query("c#")) == [sharp]
    with vault.connect() as db:
        plan = " ".join(str(row[-1]) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM search_grams WHERE search_grams MATCH '\"订\"'"))
    assert "VIRTUAL TABLE" in plan.upper()
    assert other not in ids(vault.query("订"))


def test_grams_follow_ocr_updates_and_deletion(vault):
    key, _ = vault.add(picture())
    vault.finish_ocr(key, "旧文字")
    assert ids(vault.query("旧")) == [key]
    vault.finish_ocr(key, "新内容")
    assert ids(vault.query("旧")) == [] and ids(vault.query("新")) == [key]
    vault.delete(key)
    with vault.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM search_grams WHERE search_grams MATCH '\"新\"'").fetchone()[0] == 0


def test_existing_library_gets_grams_and_one_upgrade_backup(vault):
    key, _ = vault.add_text("短词 索引")
    with vault.connect() as db:
        db.execute("DELETE FROM settings WHERE key='_schema_v150_migrated'")
        db.execute("DELETE FROM search_grams")
    assert ids(vault.query("短")) == []
    reopened = Vault(vault.root)
    assert ids(reopened.query("短")) == [key]
    backups = sorted(path.name for path in (vault.root / "backups").glob("*.sqlite3"))
    assert backups == ["before-1.5.0-upgrade.sqlite3"]
    with sqlite3.connect(vault.root / "backups" / backups[0]) as db:
        assert db.execute("SELECT content FROM screenshots WHERE id=?", (key,)).fetchone()[0] == "短词 索引"
    Vault(vault.root)
    assert len(list((vault.root / "backups").glob("*.sqlite3"))) == 1


def test_relevance_ranks_title_and_repeated_hits_but_falls_back_to_time(vault):
    now = datetime.now(timezone.utc)
    body, _ = vault.add_text("会议纪要\n下午讨论 预算", now - timedelta(minutes=3))
    title, _ = vault.add_text("预算 表格\n第二行", now - timedelta(minutes=2))
    repeated, _ = vault.add_text("备注\n预算 预算 预算", now - timedelta(minutes=1))
    newest = ids(vault.query("预算"))
    assert newest == [repeated, title, body]
    ranked = ids(vault.query("预算", sort="relevance"))
    assert ranked[0] == title and ranked.index(repeated) < ranked.index(body)
    assert ids(vault.query("", sort="relevance")) == ids(vault.query(""))


def test_layout_is_stored_separately_and_follows_ocr_rules(vault):
    key, _ = vault.add(picture())
    lines = [{"x": 1.04, "y": 2, "w": 30, "h": 9, "text": "第一行"},
             {"x": "bad", "y": 2, "w": 3, "h": 3, "text": "坏数据"},
             {"x": 1, "y": 20, "w": 0, "h": 9, "text": "零宽"}]
    vault.finish_ocr(key, "第一行", lines=lines)
    assert vault.ocr_lines(key) == [{"x": 1.0, "y": 2.0, "w": 30.0, "h": 9.0, "text": "第一行"}]
    assert "lines" not in vault.get(key) and "ocr_layouts" not in str(vault.query("第一行")[0].keys())
    vault.finish_ocr(key, "")  # blank retry keeps text and layout
    assert vault.ocr_lines(key)[0]["text"] == "第一行"
    vault.finish_ocr(key, "换了内容")  # new text without layout clears stale boxes
    assert vault.ocr_lines(key) == []
    vault.finish_ocr(key, "第一行", lines=lines)
    vault.delete(key)
    with vault.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ocr_layouts").fetchone()[0] == 0


def test_png_encoding_runs_outside_the_shared_lock(vault, monkeypatch):
    seen = []
    original = Image.Image.save

    def save(image, *args, **kwargs):
        probe = threading.Thread(target=lambda: seen.append(vault.lock.acquire(timeout=1) and (vault.lock.release() or True)))
        probe.start()
        probe.join()
        return original(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", save)
    key, created = vault.add(picture())
    assert created and seen == [True, True]
    assert vault.get(key)["width"] == 120
    assert not list(vault.images.glob("*.tmp")) and not list(vault.thumbs.glob("*.tmp"))


def test_duplicate_found_after_encoding_discards_its_files(vault, monkeypatch):
    original = Image.Image.save
    raced = []

    def save(image, *args, **kwargs):
        if not raced:
            raced.append(None)
            # A concurrent capture of the same pixels finishes first.
            raced[0] = vault.add(picture("blue"))
        return original(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", save)
    key, created = vault.add(picture("blue"))
    first, first_created = raced[0]
    assert first_created and (key, created) == (first, False)
    assert len(list(vault.images.glob("*.png"))) == 1 and len(list(vault.thumbs.glob("*.png"))) == 1
    assert not list(vault.images.glob("*.tmp")) and not list(vault.thumbs.glob("*.tmp"))


def test_cleanup_scans_once_removes_leftovers_and_skips_locked_files(vault, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    keys = [vault.add(picture(color), old)[0] for color in ("red", "green", "blue")]
    leftover = vault.images / f"2026-01-01_00-00-00_interrupted_{keys[0]}.png"
    leftover.write_bytes(b"copy left by an interrupted rename")
    vault.set_setting("retention_days", 7)
    scans = []
    original_scan = vault._image_files_by_key
    monkeypatch.setattr(vault, "_image_files_by_key", lambda: scans.append(True) or original_scan())
    locked = vault.get(keys[1])["filename"]
    original_unlink = Path.unlink

    def unlink(path, missing_ok=False):
        if path.name == locked:
            raise PermissionError("held open by a viewer")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert vault.cleanup() == 2
    assert scans == [True]
    assert not leftover.exists()
    assert vault.get(keys[0]) is None and vault.get(keys[2]) is None
    assert vault.get(keys[1]) is not None  # retried by the next cleanup


def test_upgrade_markers_are_named_in_order():
    assert [name for _marker, name in storage.UPGRADE_BACKUPS] == ["before-topics-migration", "before-1.5.0-upgrade", "before-1.6.0-upgrade"]
