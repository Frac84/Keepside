from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from PIL import Image
import pytest

from snapvault.storage import Vault, safe_title
from snapvault.ocr import clean_text


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault")


def picture(color="red", size=(120, 80)):
    return Image.new("RGBA", size, color)


def test_save_lossless_and_pixel_dedup(vault):
    source = picture()
    key, fresh = vault.add(source)
    duplicate, again = vault.add(source.convert("RGB"))
    assert fresh and not again and duplicate == key
    row = vault.get(key)
    with Image.open(vault.images / row["filename"]) as restored:
        assert restored.tobytes() == source.tobytes()
    assert (vault.thumbs / f"{key}.png").is_file()
    assert vault.stats()["total"] == 1


def test_dimension_and_alpha_are_part_of_identity(vault):
    a, _ = vault.add(picture(size=(120, 80)))
    b, _ = vault.add(picture(size=(80, 120)))
    c, _ = vault.add(picture(color=(255, 0, 0, 120)))
    assert len({a, b, c}) == 3


def test_ocr_search_multiple_words_chinese_and_literals(vault):
    key, _ = vault.add(picture())
    vault.finish_ocr(key, "Gemini ERROR_CONNECTION_TIMED_OUT\n学校作业 100% _ <tag>")
    assert "Gemini ERROR_CONNECTION_TIMED_OUT" in vault.get(key)["filename"]
    assert len(list(vault.images.glob("*.png"))) == 1
    for query in ["gemini timed", "学校", "校作", "100%", "_", "<tag>", "ＧＥＭＩＮＩ"]:
        assert [r["id"] for r in vault.query(query)] == [key], query
    assert not vault.query("Gemini nonexistent")
    assert not vault.query("' OR 1=1 --")
    assert not vault.query('"')


def test_reindex_updates_and_deletes_old_text(vault):
    key, _ = vault.add(picture())
    vault.finish_ocr(key, "Alpha pineapple")
    vault.finish_ocr(key, "Beta strawberry")
    assert not vault.query("pineapple")
    assert len(vault.query("strawberry")) == 1
    row = vault.get(key)
    vault.delete(key)
    assert not vault.query("strawberry")
    assert not (vault.images / row["filename"]).exists()
    assert not (vault.thumbs / f"{key}.png").exists()


@pytest.mark.parametrize("failure_api", ["fail_ocr", "finish_ocr"])
def test_failed_retry_preserves_text_title_filename_and_search_index(vault, failure_api):
    key, _ = vault.add(picture())
    vault.finish_ocr(key, "Original pineapple\n学校作业 keep this text")
    before = vault.get(key)
    original_bytes = (vault.images / before["filename"]).read_bytes()
    if failure_api == "fail_ocr":
        vault.fail_ocr(key, "Synthetic model timeout")
    else:
        vault.finish_ocr(key, "Incorrect partial replacement", "Synthetic model timeout")
    after = vault.get(key)
    assert after["ocr_status"] == "error"
    assert after["ocr_error"] == "Synthetic model timeout"
    for field in before:
        if field not in {"ocr_status", "ocr_error"}:
            assert after[field] == before[field], field
    assert (vault.images / after["filename"]).read_bytes() == original_bytes
    assert list(vault.images.glob("*.png")) == [vault.images / before["filename"]]
    assert [row["id"] for row in vault.query("pineapple 学校")] == [key]
    assert [row["id"] for row in vault.query("pineapple", kind="error")] == [key]
    assert [row["id"] for row in vault.query("pineapple", kind="text")] == [key]
    assert not vault.query("replacement")


@pytest.mark.parametrize("blank", ["", " \n\t\u3000"])
def test_blank_retry_marks_failure_without_erasing_existing_result(vault, blank):
    key, _ = vault.add(picture())
    vault.finish_ocr(key, "Existing strawberry receipt")
    before = vault.get(key)
    vault.finish_ocr(key, blank)
    after = vault.get(key)
    assert after["ocr_status"] == "error" and after["ocr_error"]
    assert "保留" in after["ocr_error"]
    for field in ("ocr_text", "title", "filename", "search_text"):
        assert after[field] == before[field]
    assert [row["id"] for row in vault.query("strawberry")] == [key]
    assert (vault.images / before["filename"]).is_file()


def test_successful_retry_replaces_preserved_result_and_clears_failure(vault):
    key, _ = vault.add(picture())
    vault.finish_ocr(key, "Earlier pineapple")
    old_path = vault.images / vault.get(key)["filename"]
    vault.fail_ocr(key, "Synthetic failure")
    vault.finish_ocr(key, "Recovered strawberry\nNew searchable text")
    row = vault.get(key)
    assert row["ocr_status"] == "done" and row["ocr_error"] == ""
    assert row["ocr_text"] == "Recovered strawberry\nNew searchable text"
    assert row["title"] == "Recovered strawberry"
    assert "Recovered strawberry" in row["filename"]
    assert (vault.images / row["filename"]).is_file() and not old_path.exists()
    assert [result["id"] for result in vault.query("strawberry")] == [key]
    assert not vault.query("pineapple")


def test_failure_after_deletion_does_not_restore_row_or_files(vault):
    key, _ = vault.add(picture())
    vault.finish_ocr(key, "Original searchable content")
    vault.delete(key)
    vault.fail_ocr(key, "Late failure")
    vault.finish_ocr(key, "Late partial text", "Late failure")
    vault.finish_ocr(key, "")
    assert vault.get(key) is None
    assert vault.pending(True) == []
    assert not vault.query("searchable") and not vault.query("partial")
    assert not list(vault.images.iterdir()) and not list(vault.thumbs.iterdir())
    assert vault.stats()["total"] == 0


def test_cleanup_protects_favorites_and_exact_boundary(vault):
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    expired, _ = vault.add(picture("red"), now-timedelta(days=8))
    favorite, _ = vault.add(picture("blue"), now-timedelta(days=80))
    boundary, _ = vault.add(picture("green"), now-timedelta(days=7))
    vault.toggle_favorite(favorite)
    assert vault.cleanup(now) == 1
    assert not vault.get(expired)
    assert vault.get(favorite) and vault.get(boundary)
    vault.set_setting("retention_days", 0)
    assert vault.cleanup(now+timedelta(days=100)) == 0


def test_resume_pending_and_failed_recognition(vault):
    a, _ = vault.add(picture("red"))
    b, _ = vault.add(picture("blue"))
    vault.finish_ocr(b, "", "Missing OCR pack")
    reopened = Vault(vault.root)
    assert reopened.pending() == [a]
    assert set(reopened.pending(True)) == {a, b}
    reopened.finish_ocr(b, "Recovered OCR")
    assert reopened.get(b)["ocr_error"] == ""
    assert reopened.get(b)["ocr_status"] == "done"


def test_parallel_duplicate_capture_is_atomic(vault):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: vault.add(picture()), range(12)))
    assert sum(created for _, created in results) == 1
    assert len(list(vault.images.glob("*.png"))) == 1


def test_failed_write_leaves_no_database_record(vault, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Disk full")
    monkeypatch.setattr(Image.Image, "save", fail)
    with pytest.raises(OSError, match="Disk full"):
        vault.add(picture())
    assert vault.stats()["total"] == 0
    assert list(vault.images.iterdir()) == []


def test_ocr_result_after_delete_does_not_resurrect(vault):
    key, _ = vault.add(picture())
    vault.delete(key)
    vault.finish_ocr(key, "late result")
    assert vault.stats()["total"] == 0
    assert vault.query("late") == []


def test_filters_pagination_and_settings_persist(vault):
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    a, _ = vault.add(picture("red"), now)
    b, _ = vault.add(picture("blue"), now-timedelta(days=1))
    vault.add(picture("green"), now-timedelta(days=9))
    vault.toggle_favorite(b)
    assert [r["id"] for r in vault.query(scope="today", now=now)] == [a]
    assert [r["id"] for r in vault.query(scope="yesterday", now=now)] == [b]
    assert [r["id"] for r in vault.query(scope="favorites")] == [b]
    assert len(vault.query(scope="week", now=now)) == 2
    assert vault.query(limit=1, offset=1)[0]["id"] == b
    vault.set_setting("paused", True)
    assert Vault(vault.root).setting("paused") is True


def test_kind_filters_intersect_with_search_and_time_scope(vault):
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    receipt, _ = vault.add(picture("red"), now)
    favorite, _ = vault.add(picture("blue"), now-timedelta(minutes=1))
    yesterday, _ = vault.add(picture("green"), now-timedelta(days=1))
    unrelated, _ = vault.add(picture("yellow"), now-timedelta(minutes=2))
    pending, _ = vault.add(picture("purple"), now-timedelta(minutes=3))
    failed, _ = vault.add(picture("orange"), now-timedelta(minutes=4))
    for key in (receipt, favorite, yesterday):
        vault.finish_ocr(key, "Receipt payment confirmation")
    vault.finish_ocr(unrelated, "Meeting agenda")
    vault.finish_ocr(failed, "", "Missing OCR language")
    vault.toggle_favorite(favorite)
    vault.toggle_favorite(yesterday)

    assert [r["id"] for r in vault.query("receipt", "today", now=now, kind="text")] == [receipt, favorite]
    assert [r["id"] for r in vault.query("receipt", "today", now=now, kind="favorites")] == [favorite]
    assert [r["id"] for r in vault.query("receipt", "favorites", now=now, kind="text")] == [favorite, yesterday]
    assert [r["id"] for r in vault.query(scope="yesterday", now=now, kind="text")] == [yesterday]
    assert [r["id"] for r in vault.query(scope="today", now=now, kind="pending")] == [pending]
    assert [r["id"] for r in vault.query(scope="today", now=now, kind="error")] == [failed]
    assert not vault.query("receipt", "today", now=now, kind="pending")


def test_sort_orders_timestamp_and_tied_ids_before_pagination(vault):
    now = datetime.now(timezone.utc)
    newer, older = [], []
    for color, destination, stamp in [
        ("red", newer, now), ("blue", newer, now), ("green", newer, now),
        ("yellow", older, now-timedelta(hours=1)),
        ("purple", older, now-timedelta(hours=1)),
    ]:
        key, _ = vault.add(picture(color), stamp)
        vault.finish_ocr(key, "Invoice match")
        destination.append(key)
    ignored, _ = vault.add(picture("orange"), now+timedelta(hours=1))
    vault.finish_ocr(ignored, "Unrelated document")
    oldest_first = sorted(older) + sorted(newer)
    newest_first = sorted(newer, reverse=True) + sorted(older, reverse=True)

    for order, expected in [("oldest", oldest_first), ("newest", newest_first)]:
        assert [r["id"] for r in vault.query("invoice", kind="text", sort=order)] == expected
        assert [r["id"] for r in vault.query("invoice", limit=2, offset=1, kind="text", sort=order)] == expected[1:3]


def test_invalid_kind_and_sort_fall_back_without_bypassing_filters(vault):
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    newest, _ = vault.add(picture("red"), now)
    older, _ = vault.add(picture("blue"), now-timedelta(minutes=1))
    yesterday, _ = vault.add(picture("green"), now-timedelta(days=1))
    unrelated, _ = vault.add(picture("yellow"), now)
    for key in (newest, older, yesterday):
        vault.finish_ocr(key, "Needle receipt")
    vault.finish_ocr(unrelated, "Meeting agenda")
    for kind, order in [
        ("favorites OR 1=1; DROP TABLE screenshots; --", "ASC; DROP TABLE settings; --"),
        ("unknown", "unknown"), (None, None), (["text"], {"order": "oldest"}),
    ]:
        rows = vault.query("needle", "today", now=now, kind=kind, sort=order)
        assert [r["id"] for r in rows] == [newest, older]
    assert vault.stats()["total"] == 4
    vault.set_setting("still_writable", True)
    assert vault.setting("still_writable") is True


def test_stats_report_chip_and_yesterday_counts_without_changing_existing_keys(vault):
    empty = vault.stats()
    assert empty == dict.fromkeys(("total", "size", "favorites", "today", "week", "yesterday", "text", "pending", "error"), 0)
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    text_today, _ = vault.add(picture("red"), now)
    blank_today, _ = vault.add(picture("blue"), now)
    vault.add(picture("green"), now)  # Pending recognition.
    failed_today, _ = vault.add(picture("yellow"), now)
    text_yesterday, _ = vault.add(picture("purple"), now-timedelta(days=1))
    text_old, _ = vault.add(picture("orange"), now-timedelta(days=8))
    for key in (text_today, text_yesterday, text_old):
        vault.finish_ocr(key, "Readable text")
    vault.finish_ocr(blank_today, "")
    vault.finish_ocr(failed_today, "", "Synthetic OCR failure")
    vault.toggle_favorite(text_today)
    vault.toggle_favorite(text_yesterday)

    stats = vault.stats()
    assert stats.pop("size") > 0
    assert stats == {"total": 6, "favorites": 2, "today": 4, "week": 5,
                     "yesterday": 1, "text": 3, "pending": 1, "error": 1}


def test_file_title_cannot_escape_path():
    result = safe_title('../foo\\bar: baz? <test>\nline\x00')
    assert not any(c in result for c in '<>:"/\\|?*\x00\n')
    assert len(safe_title("A" * 500)) == 64


def test_windows_chinese_spacing_preserves_english_and_newlines():
    assert clean_text("学 校 作 业\nGemini connection timeout\n本 地 ， 安 全") == "学校作业\nGemini connection timeout\n本地，安全"


def test_text_preserves_code_whitespace_bytes_and_deduplicates(vault):
    text = "\r\n    def example():\r\n\treturn '原样保存'  \r\n"
    key, created = vault.add_text(text)
    row = vault.get(key)
    assert created and row["item_type"] == "text" and row["category"] == "tech"
    assert row["content"] == row["ocr_text"] == text
    assert row["size"] == len(text.encode("utf-8"))
    assert row["filename"] == "" and row["width"] == row["height"] == 0
    assert row["ocr_status"] == "done" and row["hash"].startswith("text:")
    assert not list(vault.images.iterdir()) and not list(vault.thumbs.iterdir())
    assert vault.add_text(text) == (key, False)
    changed, fresh = vault.add_text(text.replace("\r\n", "\n"))
    assert fresh and changed != key
    assert [item["id"] for item in vault.query("原样", item_type="text", limit=10)] == [changed, key]


def test_links_preserve_all_parameters_and_are_separate_from_images(vault):
    url = "https://www.jd.com/item?id=1&q=A%2FB&token=keep-me#fragment"
    key, created = vault.add_text(url)
    row = vault.get(key)
    assert created and row["item_type"] == "link" and row["category"] == "shopping"
    assert row["content"] == row["ocr_text"] == url
    assert row["hash"].startswith("link:")
    assert vault.add_text(url) == (key, False)
    text_key, _ = vault.add_text(" " + url)
    assert vault.get(text_key)["item_type"] == "text"
    image_key, _ = vault.add(picture())
    assert vault.get(image_key)["item_type"] == "image"
    assert not vault.get(image_key)["hash"].startswith(("text:", "link:"))
    assert vault.type_counts() == {"image": 1, "text": 1, "link": 1}


def test_empty_oversized_and_invalid_timestamp_text_never_creates_records(vault):
    from snapvault.storage import MAX_TEXT_BYTES

    for text in ("", " \r\n\t\u3000", "x" * (MAX_TEXT_BYTES + 1), "汉" * (MAX_TEXT_BYTES // 3 + 1)):
        with pytest.raises(ValueError):
            vault.add_text(text)
    with pytest.raises(TypeError):
        vault.add_text(None)
    with pytest.raises(ValueError):
        vault.add_text("valid content", datetime(2026, 1, 1))
    assert vault.stats()["total"] == 0


def test_concurrent_text_dedup_is_atomic(vault):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: vault.add_text("原样内容\nsecond line"), range(12)))
    assert sum(created for _, created in results) == 1
    assert len({key for key, _ in results}) == 1
    assert vault.stats()["total"] == 1


def test_non_images_never_enter_ocr_and_deletion_does_not_touch_image_files(vault):
    image_key, _ = vault.add(picture())
    original_path = vault.images / vault.get(image_key)["filename"]
    for text in ("plain content", "https://example.com/?keep=1"):
        key, _ = vault.add_text(text)
        before = vault.get(key)
        vault.finish_ocr(key, "unexpected OCR replacement")
        vault.finish_ocr(key, "", "unexpected OCR failure")
        vault.fail_ocr(key, "unexpected OCR failure")
        assert vault.get(key) == before
        assert key not in vault.pending(True)
        vault.delete(key)
        assert vault.get(key) is None
        assert original_path.is_file()
    assert vault.pending() == [image_key]
    assert vault.type_counts() == {"image": 1, "text": 0, "link": 0}


def test_manual_category_survives_ocr_and_automatic_category_updates_on_success(vault):
    key, _ = vault.add(picture())
    vault.finish_ocr(key, "请保存发票和订单")
    assert vault.get(key)["category"] == "shopping"
    vault.finish_ocr(key, "def greet():\n    return 1")
    assert vault.get(key)["category"] == "tech"
    vault.set_category(key, "read")
    vault.finish_ocr(key, "新的数据库接口文档")
    row = vault.get(key)
    assert row["category"] == "read" and row["category_manual"] == 1
    with pytest.raises(ValueError):
        vault.set_category(key, "all")
    assert Vault(vault.root).get(key)["category"] == "read"


def test_type_category_search_time_and_favorite_filters_intersect(vault):
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    image, _ = vault.add(picture(), now)
    vault.finish_ocr(image, "Needle Python code")
    plain, _ = vault.add_text("Needle Python code fragment", now-timedelta(minutes=1))
    link, _ = vault.add_text("https://github.com/org/needle", now-timedelta(minutes=2))
    old, _ = vault.add_text("Needle Python older fragment", now-timedelta(days=1))
    vault.toggle_favorite(plain)
    assert [row["id"] for row in vault.query("needle", "today", category="tech", item_type="text")] == [plain]
    assert [row["id"] for row in vault.query("needle", "favorites", category="tech")] == [plain]
    assert [row["id"] for row in vault.query(kind="images")] == [image]
    assert [row["id"] for row in vault.query(kind="plaintext", sort="oldest")] == [old, plain]
    assert [row["id"] for row in vault.query(kind="links")] == [link]
    assert {row["id"] for row in vault.query(kind="text")} == {image, plain, link, old}
    for category, item_type in [("tech' OR 1=1 --", "text' OR 1=1 --"), ([], {})]:
        assert [row["id"] for row in vault.query("needle", "today", kind="favorites", category=category, item_type=item_type)] == [plain]
    assert vault.category_counts() == {"tech": 4, "read": 0, "shopping": 0, "daily": 0, "other": 0}


def test_text_and_link_cleanup_obey_the_same_favorite_and_retention_rules(vault):
    now = datetime.now(timezone.utc)
    expired_text, _ = vault.add_text("old plain text", now-timedelta(days=8))
    expired_link, _ = vault.add_text("https://example.com/old", now-timedelta(days=8))
    protected, _ = vault.add_text("saved permanent text", now-timedelta(days=20))
    fresh, _ = vault.add_text("https://example.com/fresh", now)
    vault.toggle_favorite(protected)
    assert vault.cleanup(now) == 2
    assert vault.get(expired_text) is vault.get(expired_link) is None
    assert vault.get(protected) and vault.get(fresh)
    assert vault.stats()["favorites"] == 1
    assert vault.type_counts() == {"image": 0, "text": 1, "link": 1}
    assert not vault.query("old plain")


def test_legacy_schema_migrates_once_using_only_existing_ocr(tmp_path, monkeypatch):
    import sqlite3
    from snapvault import storage

    root = tmp_path / "legacy"
    root.mkdir()
    stamp = "2026-01-02T03:04:05+00:00"
    with sqlite3.connect(root / "vault.sqlite3") as db:
        db.execute("""CREATE TABLE screenshots (
            id TEXT PRIMARY KEY,hash TEXT UNIQUE NOT NULL,filename TEXT NOT NULL,captured_at TEXT NOT NULL,
            width INTEGER NOT NULL,height INTEGER NOT NULL,size INTEGER NOT NULL,title TEXT NOT NULL,
            ocr_text TEXT NOT NULL DEFAULT '',search_text TEXT NOT NULL DEFAULT '',favorite INTEGER NOT NULL DEFAULT 0,
            ocr_status TEXT NOT NULL DEFAULT 'pending',ocr_error TEXT NOT NULL DEFAULT '')""")
        values = ("legacy", "original-hash", "original-name.png", stamp, 120, 80, 100,
                  "Original title", "Python 接口文档", "python 接口文档 original title", 1, "done", "")
        db.execute("INSERT INTO screenshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        db.row_factory = sqlite3.Row
        original = dict(db.execute("SELECT * FROM screenshots").fetchone())
    migrated = Vault(root)
    row = migrated.get("legacy")
    assert all(row[field] == value for field, value in original.items())
    assert row["item_type"] == "image" and row["content"] == ""
    assert row["category"] == "tech" and row["category_manual"] == 0
    assert [item["id"] for item in migrated.query("python")] == ["legacy"]
    assert not list(migrated.images.iterdir()) and not list(migrated.thumbs.iterdir())

    def forbidden(*args, **kwargs):
        raise AssertionError("Existing rows must not be reclassified on every startup")

    monkeypatch.setattr(storage, "classify_content", forbidden)
    reopened = Vault(root)
    assert reopened.get("legacy") == row
