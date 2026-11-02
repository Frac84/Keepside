"""Topic migration, user corrections, and stale worker protection use temporary vaults."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import threading

from PIL import Image
import pytest

from snapvault.classification import ALL_TOPIC_LABELS, CLASSIFICATION_VERSION
from snapvault.storage import Vault


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "topics")


def classify(vault, key, tags):
    payload = vault.classification_input(key)
    return vault.apply_classification(key, tags, {tag: 0.95 for tag in tags}, payload["fingerprint"])


def test_legacy_manual_categories_are_preserved_without_rewriting_content(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    with sqlite3.connect(root / "vault.sqlite3") as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE screenshots (
                id TEXT PRIMARY KEY,hash TEXT UNIQUE NOT NULL,filename TEXT NOT NULL,captured_at TEXT NOT NULL,
                width INTEGER NOT NULL,height INTEGER NOT NULL,size INTEGER NOT NULL,title TEXT NOT NULL,
                ocr_text TEXT NOT NULL DEFAULT '',search_text TEXT NOT NULL DEFAULT '',favorite INTEGER NOT NULL DEFAULT 0,
                ocr_status TEXT NOT NULL DEFAULT 'pending',ocr_error TEXT NOT NULL DEFAULT '',
                item_type TEXT NOT NULL DEFAULT 'image',content TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'other',category_manual INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO settings VALUES ('_classification_migrated_v1','true');
            INSERT INTO settings VALUES ('theme','"paper"');
        """)
        for index, category in enumerate(("tech", "read", "shopping", "daily", "other", "read")):
            text = "\r\n  原文保留\t" + str(index) + "  \r\n"
            db.execute("""INSERT INTO screenshots
                (id,hash,filename,captured_at,width,height,size,title,ocr_text,search_text,
                 favorite,ocr_status,ocr_error,item_type,content,category,category_manual)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                str(index), "hash-" + str(index), "", "2026-10-19T12:00:00+00:00", 0, 0, 30,
                "不可作为分类输入的标题", text, "原始搜索文字", 1, "done", "old error", "text", text,
                category, int(index < 5)))
        original = {row["id"]: dict(row) for row in db.execute("SELECT * FROM screenshots")}
    vault = Vault(root)
    for key, before in original.items():
        after = vault.get(key)
        assert all(after[field] == value for field, value in before.items())
        if before["category_manual"]:
            expected = "tech" if before["category"] == "tech" else "legacy_" + before["category"]
            assert after["tags"] == [expected]
            assert after["tags_manual"] is True and after["classification_status"] == "manual"
        else:
            assert after["tags"] == [] and after["classification_status"] == "pending"
    assert vault.pending_classifications() == ["5"]
    assert vault.setting("theme") == "paper"
    assert Vault(root).query(sort="oldest") == vault.query(sort="oldest")
    assert list(vault.images.iterdir()) == list(vault.thumbs.iterdir()) == []
    backups = list((root / "backups").glob("*.sqlite3"))
    assert len(backups) == 1 and backups[0].name == "before-topics-migration.sqlite3"
    with sqlite3.connect(backups[0]) as db:
        db.row_factory = sqlite3.Row
        assert {row["id"]: dict(row) for row in db.execute("SELECT * FROM screenshots")} == original
        assert db.execute("SELECT 1 FROM settings WHERE key='_topic_classification_migrated_v1'").fetchone() is None


def test_new_items_wait_for_ocr_and_use_only_exact_body(vault):
    body = "\r\n  def example():\n\treturn '保留'  \r\n"
    text, _ = vault.add_text(body)
    link, _ = vault.add_text("https://example.com/?token=keep#fragment")
    image, _ = vault.add(Image.new("RGB", (4, 4), "red"))
    assert set(vault.pending_classifications()) == {text, link}
    assert vault.classification_input(text)["text"] == body
    assert vault.classification_input(link)["kind"] == "link"
    assert vault.classification_input(image) is None
    vault.finish_ocr(image, "")
    assert image in vault.pending_classifications()
    assert vault.classification_input(image)["text"] == ""
    assert classify(vault, image, [])
    assert image not in vault.pending_classifications()
    assert vault.get(image)["classification_status"] == "done"
    assert image in {row["id"] for row in vault.query(topic="unknown")}


def test_manual_empty_tags_are_protected_and_recorded_locally(vault):
    key, _ = vault.add_text("用户故意留在待整理的内容")
    payload = vault.classification_input(key)
    assert vault.set_tags(key, [])
    row = vault.get(key)
    assert row["tags"] == [] and row["tags_manual"] is True
    assert row["classification_status"] == "manual"
    assert vault.classification_input(key) is None
    assert vault.request_reclassification() == 0
    assert vault.request_reclassification([key]) == 0
    assert vault.pending_classifications(True) == []
    assert not vault.apply_classification(key, ["tech"], {}, payload["fingerprint"])
    assert not vault.fail_classification(key, "late error", payload["fingerprint"])
    with vault.connect() as db:
        correction = dict(db.execute("SELECT * FROM classification_corrections").fetchone())
    assert correction["text"] == row["content"]
    assert json.loads(correction["tags_json"]) == []
    assert correction["fingerprint"] == payload["fingerprint"]


def test_apply_and_failure_cas_reject_changed_ocr_deleted_or_manual_items(vault):
    image, _ = vault.add(Image.new("RGB", (4, 4), "red"))
    vault.finish_ocr(image, "原本的发票内容")
    old = vault.classification_input(image)
    vault.finish_ocr(image, "更新后的 Python 错误日志")
    assert not vault.apply_classification(image, ["receipts"], {}, old["fingerprint"])
    assert not vault.fail_classification(image, "late timeout", old["fingerprint"])
    current = vault.classification_input(image)
    assert current["fingerprint"] != old["fingerprint"]
    assert vault.apply_classification(image, ["tech"], {"tech": 0.99}, current["fingerprint"])
    assert vault.get(image)["tags"] == ["tech"]
    vault.set_tags(image, ["learning"])
    assert not vault.apply_classification(image, ["tech"], {}, current["fingerprint"])
    assert vault.get(image)["tags"] == ["learning"]
    other, _ = vault.add_text("将被删除的内容")
    payload = vault.classification_input(other)
    vault.delete(other)
    assert not vault.apply_classification(other, ["chat"], {}, payload["fingerprint"])
    assert not vault.fail_classification(other, "late failure", payload["fingerprint"])


@pytest.mark.parametrize("failure", ["error", "blank"])
def test_failed_ocr_retry_keeps_topic_metadata_and_prior_text(vault, failure):
    key, _ = vault.add(Image.new("RGB", (4, 4), "red"))
    vault.finish_ocr(key, "Original receipt 支付成功")
    assert classify(vault, key, ["receipts"])
    before = vault.get(key)
    if failure == "error":
        vault.fail_ocr(key, "offline failure")
    else:
        vault.finish_ocr(key, "")
    after = vault.get(key)
    assert all(after[field] == value for field, value in before.items() if field not in {"ocr_status", "ocr_error"})


def test_bulk_tags_topic_filters_counts_and_body_invariants(vault):
    first, _ = vault.add_text("Python 学习资料")
    second, _ = vault.add_text("另外一条内容")
    unknown, _ = vault.add_text("暂时不分类")
    before = vault.get(first)
    assert vault.set_tags_many([first, second, first, "missing"], ["learning", "tech"]) == 2
    for tag in ("learning", "tech"):
        assert {row["id"] for row in vault.query(topic=tag)} == {first, second}
    assert [row["id"] for row in vault.query("Python", topic="tech", item_type="text")] == [first]
    assert [row["id"] for row in vault.query(topic="unknown")] == [unknown]
    assert len(vault.query()) == 3
    counts = vault.topic_counts()
    assert counts["learning"] == counts["tech"] == 2 and counts["unknown"] == 1
    assert set(counts) == {*ALL_TOPIC_LABELS, "unknown"}
    after = vault.get(first)
    for field in ("content", "ocr_text", "title", "filename", "hash", "captured_at", "favorite", "category", "category_manual"):
        assert after[field] == before[field]
    with pytest.raises(ValueError):
        vault.set_tags_many([first, second], ["tech", "chat", "tasks"])
    assert vault.get(first)["tags"] == ["learning", "tech"]
    assert vault.query(topic="tech' OR 1=1 --") == vault.query()


def test_deletion_removes_tag_and_correction_content(vault):
    key, _ = vault.add_text("这段内容删除后不应残留在纠正记录")
    vault.set_tags(key, ["chat"])
    vault.delete(key)
    with vault.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM item_tags").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM classification_corrections").fetchone()[0] == 0
    assert vault.topic_counts()["chat"] == 0


def test_errors_can_be_retried_and_manual_restore_is_explicit(vault):
    key, _ = vault.add_text("待重试内容")
    fingerprint = vault.classification_input(key)["fingerprint"]
    assert vault.fail_classification(key, "missing model", fingerprint)
    assert vault.pending_classifications() == [] and vault.pending_classifications(True) == [key]
    assert vault.request_reclassification([key]) == 1
    assert vault.get(key)["classification_error"] == ""
    vault.set_tags(key, ["tasks"])
    assert vault.reset_tags_to_automatic([key, key]) == 1
    assert vault.get(key)["tags"] == ["tasks"] and not vault.get(key)["tags_manual"]
    assert vault.pending_classifications() == [key]
    with pytest.raises(ValueError):
        vault.reset_tags_to_automatic(None)
    assert classify(vault, key, ["learning"])
    assert vault.get(key)["classification_version"] == CLASSIFICATION_VERSION


def test_late_result_or_batch_failure_cannot_replace_a_completed_result(vault):
    key, _ = vault.add_text("这一条已先完成分类")
    fingerprint = vault.classification_input(key)["fingerprint"]
    assert vault.apply_classification(key, ["learning"], {"learning": 0.9}, fingerprint)
    before = vault.get(key)
    assert not vault.apply_classification(key, ["tech"], {"tech": 0.99}, fingerprint)
    assert not vault.fail_classification(key, "later batch timeout", fingerprint)
    assert vault.get(key) == before
    assert vault.request_reclassification([key]) == 1
    assert vault.apply_classification(key, ["tech"], {"tech": 0.99}, fingerprint)


def test_page_decoration_uses_a_single_tag_query(vault, monkeypatch):
    for index in range(12):
        key, _ = vault.add_text("分页测试 " + str(index))
        vault.set_tags(key, ["learning"])
    calls = []
    original_connect = vault.connect

    @contextmanager
    def traced_connect():
        with original_connect() as db:
            db.set_trace_callback(calls.append)
            yield db

    monkeypatch.setattr(vault, "connect", traced_connect)
    assert len(vault.query()) == 12
    assert len([sql for sql in calls if sql.startswith("SELECT item_id,tag FROM item_tags")]) == 1


def test_independent_vault_instances_serialize_automatic_and_manual_writes(vault, monkeypatch):
    key, _ = vault.add_text("并发写入不应覆盖后到的手动选择")
    fingerprint = vault.classification_input(key)["fingerprint"]
    other = Vault(vault.root)
    automatic_locked = threading.Event()
    manual_started = threading.Event()
    original_write = vault._write_tags

    def paused_write(db, item, tags):
        automatic_locked.set()
        assert manual_started.wait(5)
        original_write(db, item, tags)

    def manual_write():
        assert automatic_locked.wait(5)
        manual_started.set()
        return other.set_tags(key, ["chat"])

    monkeypatch.setattr(vault, "_write_tags", paused_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        manual = pool.submit(manual_write)
        automatic = pool.submit(vault.apply_classification, key, ["tech"], {}, fingerprint)
        assert automatic.result(timeout=10) and manual.result(timeout=10)
    assert vault.get(key)["tags"] == ["chat"] and vault.get(key)["tags_manual"]


def test_missing_old_marker_never_reclassifies_an_existing_category_column(vault):
    key, _ = vault.add_text("Python 接口文档")
    with vault.connect() as db:
        db.execute("UPDATE screenshots SET category='read' WHERE id=?", (key,))
        db.execute("DELETE FROM settings WHERE key IN ('_classification_migrated_v1','_topic_classification_migrated_v1')")
    reopened = Vault(vault.root)
    assert reopened.get(key)["category"] == "read"
    assert reopened.get(key)["content"] == "Python 接口文档"
    assert (vault.root / "backups" / "before-topics-migration.sqlite3").is_file()


def test_existing_backup_is_not_overwritten_and_empty_vaults_need_no_backup(vault):
    assert not (vault.root / "backups").exists()
    key, _ = vault.add_text("需要保护的资料")
    with vault.connect() as db:
        db.execute("DELETE FROM settings WHERE key='_topic_classification_migrated_v1'")
    folder = vault.root / "backups"
    folder.mkdir()
    existing = folder / "before-topics-migration.sqlite3"
    existing.write_bytes(b"earlier backup must stay untouched")
    reopened = Vault(vault.root)
    assert existing.read_bytes() == b"earlier backup must stay untouched"
    backups = [path for path in folder.glob("*.sqlite3") if path != existing]
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT content FROM screenshots WHERE id=?", (key,)).fetchone()[0] == "需要保护的资料"
    assert reopened.get(key)["classification_status"] == "pending"


def test_backup_failure_refuses_migration_without_touching_original_database(vault, monkeypatch):
    key, _ = vault.add_text("备份失败时必须原样保留")
    with vault.connect() as db:
        db.execute("DELETE FROM settings WHERE key='_topic_classification_migrated_v1'")
    before = vault.get(key)
    original_connect = sqlite3.connect

    class LockedBackup:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def backup(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        return LockedBackup(connection) if kwargs.get("uri") else connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        Vault(vault.root)
    assert vault.get(key) == before
    with vault.connect() as db:
        assert db.execute("SELECT 1 FROM settings WHERE key='_topic_classification_migrated_v1'").fetchone() is None
    assert list((vault.root / "backups").iterdir()) == []
