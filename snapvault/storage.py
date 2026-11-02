"""Durable local storage. Image identity is based on pixels, not PNG metadata."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid

from PIL import Image, ImageOps

from .classification import (
    ALL_TOPIC_LABELS, CATEGORY_CODES, CLASSIFICATION_VERSION, TOPIC_LABELS,
    classify_content, is_single_http_url,
)


MAX_TEXT_BYTES = 1024 * 1024
# Each schema upgrade that touches an existing library takes one consistent
# backup first. The first missing marker names the backup.
UPGRADE_BACKUPS = (
    ("_topic_classification_migrated_v1", "before-topics-migration"),
    ("_schema_v150_migrated", "before-1.5.0-upgrade"),
    ("_schema_v160_migrated", "before-1.6.0-upgrade"),
)
_KEY_SUFFIX = re.compile(r"_([0-9a-f]{32})\.png$")


def utc_now():
    return datetime.now(timezone.utc)


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold()


def search_grams(text):
    """Unigrams and bigrams let 1–2 character terms use an index.

    The trigram FTS index cannot answer terms shorter than three characters.
    Tokens only contain letters and digits, matching FTS5 unicode61 token
    characters, so the index is a superset filter and exact matching still
    happens on search_text.
    """
    grams = {}
    for run in re.findall(r"[^\W_]+", text):
        for index, character in enumerate(run):
            grams[character] = None
            if index + 1 < len(run):
                grams[run[index:index + 2]] = None
    return " ".join(grams)


def _indexable_short_term(term):
    return 0 < len(term) <= 2 and term.isalnum()


def image_dhash(image):
    """256-bit difference hash of a thumbnail as 64 hex characters.

    Each bit says whether brightness rises between neighbouring cells of a
    17×16 grey grid, so recompression and small colour shifts barely move it.
    """
    grey = image.convert("L").resize((17, 16), Image.Resampling.BILINEAR)
    pixels = grey.tobytes()
    value = 0
    for row in range(16):
        for column in range(16):
            value = (value << 1) | (pixels[row * 17 + column + 1] > pixels[row * 17 + column])
    return f"{value:064x}"


def text_bigrams(text):
    compact = re.sub(r"\s+", "", normalized(text or ""))
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


TEXT_RICH_BIGRAMS = 15
MIN_TEXT_JACCARD = 0.4


def images_similar(first, second):
    """Whether two screenshots look like near-repeats of the same thing.

    Text-rich screenshots compare their OCR text (character bigrams), which
    survives small crops and scrolling. Screenshots with little text fall
    back to the thumbnail hash and a near-identical shape.
    """
    ratio = (first["width"] / max(1, first["height"])) / max(1e-6, second["width"] / max(1, second["height"]))
    if not 1 / 1.6 <= ratio <= 1.6:
        return False
    a, b = first["grams"], second["grams"]
    if len(a) >= TEXT_RICH_BIGRAMS and len(b) >= TEXT_RICH_BIGRAMS:
        shared = len(a & b)
        if not shared:
            return False
        jaccard = shared / len(a | b)
        containment = shared / min(len(a), len(b))
        return jaccard >= 0.6 or (containment >= 0.9 and jaccard >= MIN_TEXT_JACCARD)
    if len(a) >= TEXT_RICH_BIGRAMS or len(b) >= TEXT_RICH_BIGRAMS:
        return False  # one has real text, the other hardly any
    if not first["dhash"] or not second["dhash"] or not 1 / 1.1 <= ratio <= 1.1:
        return False
    return (int(first["dhash"], 16) ^ int(second["dhash"], 16)).bit_count() <= 20


def similar_groups(images):
    """Group near-repeat screenshots; returns lists of ids, newest group first.

    Text-rich images only meet candidates that share one of their rarest
    bigrams, and the rest only meet images of almost the same shape, so the
    work stays far below comparing every pair in a large library.
    """
    images = [dict(image) for image in images]
    parent = {image["id"]: image["id"] for image in images}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def join(first, second):
        if images_similar(first, second):
            parent[find(first["id"])] = find(second["id"])

    # Prefix filtering: with tokens ordered rarest first, two sets whose
    # Jaccard similarity reaches t must share one of the first
    # len - ceil(t * len) + 1 tokens of each. t is the lowest Jaccard value
    # images_similar() accepts, so no similar pair is skipped.
    rich = [image for image in images if len(image["grams"]) >= TEXT_RICH_BIGRAMS]
    frequency = {}
    for image in rich:
        for gram in image["grams"]:
            frequency[gram] = frequency.get(gram, 0) + 1
    index = {}
    for position, image in enumerate(rich):
        ordered = sorted(image["grams"], key=lambda gram: (frequency[gram], gram))
        prefix = len(ordered) - math.ceil(MIN_TEXT_JACCARD * len(ordered)) + 1
        for gram in ordered[:prefix]:
            index.setdefault(gram, []).append(position)
    seen = set()
    for positions in index.values():
        for offset, left in enumerate(positions):
            for right in positions[offset + 1:]:
                if (left, right) not in seen:
                    seen.add((left, right))
                    join(rich[left], rich[right])
    plain = sorted((image for image in images if len(image["grams"]) < TEXT_RICH_BIGRAMS and image["dhash"]),
                   key=lambda image: image["width"] / max(1, image["height"]))
    for position, image in enumerate(plain):
        shape = image["width"] / max(1, image["height"])
        for other in plain[position + 1:]:
            if other["width"] / max(1, other["height"]) > shape * 1.1:
                break
            join(image, other)
    groups = {}
    for image in images:
        groups.setdefault(find(image["id"]), []).append(image)
    result = [sorted(members, key=lambda image: (image["captured_at"], image["id"]), reverse=True)
              for members in groups.values() if len(members) > 1]
    result.sort(key=lambda members: (members[0]["captured_at"], members[0]["id"]), reverse=True)
    return [[image["id"] for image in members] for members in result]


def safe_title(text):
    lines = [re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', s).strip() for s in text.splitlines()]
    return re.sub(r"\s+", " ", " ".join(s for s in lines if s))[:64].strip(" .") or "未命名截图"


def content_title(text):
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return safe_title(first)[:48]


class Vault:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.images = self.root / "images"
        self.thumbs = self.root / "thumbnails"
        self.images.mkdir(parents=True, exist_ok=True)
        self.thumbs.mkdir(exist_ok=True)
        self.db_path = self.root / "vault.sqlite3"
        self.lock = threading.RLock()
        self._backup_before_upgrades()
        with self.connect() as db:
            had_search_index = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_index'").fetchone() is not None
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS screenshots (
                    id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL,
                    filename TEXT NOT NULL, captured_at TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL, size INTEGER NOT NULL,
                    title TEXT NOT NULL, ocr_text TEXT NOT NULL DEFAULT '',
                    search_text TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0,
                    ocr_status TEXT NOT NULL DEFAULT 'pending', ocr_error TEXT NOT NULL DEFAULT '',
                    item_type TEXT NOT NULL DEFAULT 'image', content TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'other', category_manual INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS capture_time ON screenshots(captured_at DESC);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                    search_text, content='screenshots', content_rowid='rowid', tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS shots_insert AFTER INSERT ON screenshots BEGIN
                    INSERT INTO search_index(rowid, search_text) VALUES (new.rowid, new.search_text);
                END;
                CREATE TRIGGER IF NOT EXISTS shots_delete AFTER DELETE ON screenshots BEGIN
                    INSERT INTO search_index(search_index,rowid,search_text)
                    VALUES('delete',old.rowid,old.search_text);
                END;
                CREATE TRIGGER IF NOT EXISTS shots_update AFTER UPDATE OF search_text ON screenshots BEGIN
                    INSERT INTO search_index(search_index,rowid,search_text)
                    VALUES('delete',old.rowid,old.search_text);
                    INSERT INTO search_index(rowid,search_text) VALUES(new.rowid,new.search_text);
                END;
            """)
            self._migrate_content_schema(db)
            self._migrate_topic_schema(db)
            if not had_search_index:
                db.execute("INSERT INTO search_index(search_index) VALUES ('rebuild')")
            self._migrate_v150_schema(db)
            self._migrate_v160_schema(db)

    @staticmethod
    def _migrate_v160_schema(db):
        """Additive 1.6.0 layer: thumbnail hash and QR payloads per image.

        codes stays NULL until an image has been scanned for QR codes, so old
        screenshots can be found and rescanned on request.
        """
        db.executescript("""
            CREATE TABLE IF NOT EXISTS image_features (
                item_id TEXT PRIMARY KEY REFERENCES screenshots(id) ON DELETE CASCADE,
                dhash TEXT NOT NULL DEFAULT '',
                codes TEXT
            );
        """)
        marker = "_schema_v160_migrated"
        if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone() is None:
            db.execute("INSERT INTO settings(key,value) VALUES (?,?)", (marker, "true"))

    @staticmethod
    def _migrate_v150_schema(db):
        """Additive 1.5.0 layer: OCR layout, review history and short-term index."""
        columns = {row["name"] for row in db.execute("PRAGMA table_info(classification_corrections)")}
        for name, definition in (("previous_tags_json", "TEXT NOT NULL DEFAULT ''"),
                                 ("previous_status", "TEXT NOT NULL DEFAULT ''"),
                                 ("source", "TEXT NOT NULL DEFAULT 'edit'")):
            if name not in columns:
                db.execute(f"ALTER TABLE classification_corrections ADD COLUMN {name} {definition}")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS ocr_layouts (
                item_id TEXT PRIMARY KEY REFERENCES screenshots(id) ON DELETE CASCADE,
                lines TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS search_grams USING fts5(
                grams, content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 0'
            );
        """)
        marker = "_schema_v150_migrated"
        if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone() is None:
            db.execute("DELETE FROM search_grams")
            db.executemany("INSERT INTO search_grams(rowid,grams) VALUES (?,?)",
                           ((row[0], search_grams(row[1])) for row in
                            db.execute("SELECT rowid,search_text FROM screenshots").fetchall()))
            db.execute("INSERT INTO settings(key,value) VALUES (?,?)", (marker, "true"))

    @staticmethod
    def _index_grams(db, rowid, search_text, *, replace=True):
        if replace:
            db.execute("DELETE FROM search_grams WHERE rowid=?", (rowid,))
        db.execute("INSERT INTO search_grams(rowid,grams) VALUES (?,?)", (rowid, search_grams(search_text)))

    @staticmethod
    def _migrate_content_schema(db):
        columns = {row["name"] for row in db.execute("PRAGMA table_info(screenshots)")}
        had_category = "category" in columns
        additions = {
            "item_type": "TEXT NOT NULL DEFAULT 'image'",
            "content": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT 'other'",
            "category_manual": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE screenshots ADD COLUMN {name} {definition}")
        db.execute("CREATE INDEX IF NOT EXISTS item_categories ON screenshots(category,item_type)")
        marker = "_classification_migrated_v1"
        if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone() is None:
            # Only existing text is inspected. No image/OCR job, rename or search
            # rewrite occurs during this once-only additive migration.
            if not had_category:
                rows = db.execute("SELECT id,ocr_text FROM screenshots WHERE item_type='image' AND category_manual=0").fetchall()
                for row in rows:
                    db.execute("UPDATE screenshots SET category=? WHERE id=?", (classify_content(row["ocr_text"]), row["id"]))
            db.execute("INSERT INTO settings(key,value) VALUES (?,?)", (marker, "true"))

    def _backup_before_upgrades(self):
        """Take a consistent, non-overwriting SQLite backup before touching old schemas."""
        if not self.db_path.exists():
            return
        with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=15)) as source:
            source.execute("BEGIN")
            has_rows = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screenshots'").fetchone()
            if not has_rows or source.execute("SELECT 1 FROM screenshots LIMIT 1").fetchone() is None:
                return
            has_settings = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
            present = {row[0] for row in source.execute("SELECT key FROM settings")} if has_settings else set()
            missing = [name for marker, name in UPGRADE_BACKUPS if marker not in present]
            if not missing:
                return
            stem = missing[0]
            folder = self.root / "backups"
            folder.mkdir(exist_ok=True)
            target = folder / f"{stem}.sqlite3"
            if target.exists():
                target = folder / f"{stem}-{uuid.uuid4().hex}.sqlite3"
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            deadline = time.monotonic() + 15

            def check_progress(status, remaining, total):
                if time.monotonic() >= deadline:
                    raise TimeoutError("旧资料库备份超时，已停止升级，请关闭其他捕风捉影窗口后重试。")

            try:
                with closing(sqlite3.connect(temporary)) as destination:
                    source.backup(destination, pages=256, progress=check_progress, sleep=0.05)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("旧资料库备份校验失败，已停止升级。")
                # Windows rename refuses an existing destination, protecting an
                # earlier backup even if another instance raced this migration.
                while True:
                    try:
                        temporary.rename(target)
                        break
                    except FileExistsError:
                        target = folder / f"{stem}-{uuid.uuid4().hex}.sqlite3"
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _legacy_topic(category):
        return "tech" if category == "tech" else "legacy_" + category

    @classmethod
    def _migrate_topic_schema(cls, db):
        """Add the topic layer without rewriting any saved content or OCR metadata."""
        columns = {row["name"] for row in db.execute("PRAGMA table_info(screenshots)")}
        additions = {
            "tags_manual": "INTEGER NOT NULL DEFAULT 0",
            "classification_status": "TEXT NOT NULL DEFAULT 'pending'",
            "classification_error": "TEXT NOT NULL DEFAULT ''",
            "classification_version": "TEXT NOT NULL DEFAULT ''",
            "classification_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "classification_scores": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE screenshots ADD COLUMN {name} {definition}")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS item_tags (
                item_id TEXT NOT NULL REFERENCES screenshots(id) ON DELETE CASCADE,
                tag TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(item_id,tag)
            );
            CREATE INDEX IF NOT EXISTS topic_items ON item_tags(tag,item_id);
            CREATE INDEX IF NOT EXISTS classification_queue ON screenshots(tags_manual,classification_status);
            CREATE TABLE IF NOT EXISTS classification_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL REFERENCES screenshots(id) ON DELETE CASCADE,
                text TEXT NOT NULL, kind TEXT NOT NULL, tags_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
                classification_version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS correction_items ON classification_corrections(item_id);
        """)
        marker = "_topic_classification_migrated_v1"
        if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone() is None:
            rows = db.execute("SELECT * FROM screenshots WHERE category_manual=1").fetchall()
            for row in rows:
                tag = cls._legacy_topic(row["category"])
                if tag in ALL_TOPIC_LABELS:
                    db.execute("INSERT OR IGNORE INTO item_tags(item_id,tag,position) VALUES (?,?,0)", (row["id"], tag))
                db.execute("""UPDATE screenshots SET tags_manual=1,classification_status='manual',
                    classification_fingerprint=? WHERE id=?""", (cls._content_fingerprint(row), row["id"]))
            db.execute("INSERT INTO settings(key,value) VALUES (?,?)", (marker, "true"))

    @staticmethod
    def _content_text(row):
        return row["ocr_text"] if row["item_type"] == "image" else row["content"]

    @classmethod
    def _content_fingerprint(cls, row):
        return hashlib.sha256((row["item_type"] + "\0" + cls._content_text(row)).encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_tags(tags):
        if not isinstance(tags, (list, tuple)) or any(not isinstance(tag, str) or tag not in ALL_TOPIC_LABELS for tag in tags):
            raise ValueError("未知的内容标签")
        tags = list(dict.fromkeys(tags))
        if len(tags) > 2:
            raise ValueError("每条内容最多设置两个标签")
        return tags

    @staticmethod
    def _write_tags(db, key, tags):
        db.execute("DELETE FROM item_tags WHERE item_id=?", (key,))
        db.executemany("INSERT INTO item_tags(item_id,tag,position) VALUES (?,?,?)",
                       ((key, tag, position) for position, tag in enumerate(tags)))

    @staticmethod
    def _decorate_rows(db, rows):
        """Load tags with one bounded query for a page, never one query per item."""
        results = [dict(row) for row in rows]
        by_id = {row["id"]: row for row in results}
        for row in results:
            row["tags"] = []
            row["tags_manual"] = bool(row["tags_manual"])
        keys = list(by_id)
        # Stay below older SQLite parameter limits even for callers using a large page.
        for start in range(0, len(keys), 900):
            batch = keys[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            for tag in db.execute(f"SELECT item_id,tag FROM item_tags WHERE item_id IN ({placeholders}) ORDER BY position,tag", batch):
                by_id[tag["item_id"]]["tags"].append(tag["tag"])
        return results

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.db_path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def add(self, image: Image.Image, captured_at=None):
        image = ImageOps.exif_transpose(image).convert("RGBA")
        if image.width * image.height > 80_000_000:
            raise ValueError("图片超过 8000 万像素，请缩小后再导入")
        digest = hashlib.sha256(f"{image.width}x{image.height}:".encode() + image.tobytes()).hexdigest()
        with self.connect() as db:
            existing = db.execute("SELECT id FROM screenshots WHERE hash=?", (digest,)).fetchone()
        if existing:
            return existing[0], False
        stamp = captured_at or utc_now()
        if stamp.tzinfo is None:
            raise ValueError("captured_at must include timezone")
        key = uuid.uuid4().hex
        filename = f"{stamp.astimezone():%Y-%m-%d_%H-%M-%S}_截图_{key}.png"
        target = self.images / filename
        temp = target.with_suffix(".tmp")
        thumb = self.thumbs / f"{key}.png"
        thumb_temp = thumb.with_suffix(".tmp")
        try:
            # PNG encoding can take most of a second for a large screenshot.
            # Keep it outside the shared lock so gallery queries stay responsive;
            # the hash is checked again before anything becomes visible.
            image.save(temp, format="PNG")
            small = image.copy()
            small.thumbnail((480, 320))
            small.save(thumb_temp, format="PNG")
            dhash = image_dhash(small)
            with self.connect() as db:
                existing = db.execute("SELECT id FROM screenshots WHERE hash=?", (digest,)).fetchone()
                if existing:
                    for path in (temp, thumb_temp):
                        path.unlink(missing_ok=True)
                    return existing[0], False
                os.replace(temp, target)
                os.replace(thumb_temp, thumb)
                search = normalized(filename)
                cursor = db.execute("""INSERT INTO screenshots
                    (id,hash,filename,captured_at,width,height,size,title,search_text)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (
                    key, digest, filename, stamp.astimezone(timezone.utc).isoformat(),
                    image.width, image.height, target.stat().st_size, "未命名截图", search))
                self._index_grams(db, cursor.lastrowid, search, replace=False)
                db.execute("INSERT INTO image_features(item_id,dhash) VALUES (?,?)", (key, dhash))
        except Exception:
            for path in (temp, target, thumb_temp, thumb):
                path.unlink(missing_ok=True)
            raise
        return key, True

    def add_text(self, text, captured_at=None):
        """Save exact clipboard text; content classification never rewrites it."""
        if not isinstance(text, str):
            raise TypeError("只能保存纯文本内容")
        if not text.strip():
            raise ValueError("文本为空，未保存")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise ValueError("文本超过 1 MiB，未保存")
        item_type = "link" if is_single_http_url(text) else "text"
        digest = item_type + ":" + hashlib.sha256(encoded).hexdigest()
        with self.connect() as db:
            existing = db.execute("SELECT id FROM screenshots WHERE hash=?", (digest,)).fetchone()
            if existing:
                return existing[0], False
            stamp = captured_at or utc_now()
            if stamp.tzinfo is None:
                raise ValueError("captured_at must include timezone")
            key = uuid.uuid4().hex
            title = content_title(text)
            if title == "未命名截图":
                title = "链接" if item_type == "link" else "文本片段"
            category = classify_content(text, text if item_type == "link" else "")
            search = normalized(title + " " + text)
            cursor = db.execute("""INSERT INTO screenshots
                (id,hash,filename,captured_at,width,height,size,title,ocr_text,search_text,
                 ocr_status,item_type,content,category)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                key, digest, "", stamp.astimezone(timezone.utc).isoformat(), 0, 0,
                len(encoded), title, text, search, "done", item_type, text, category))
            self._index_grams(db, cursor.lastrowid, search, replace=False)
        return key, True

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM screenshots WHERE id=?", (key,)).fetchone()
            return self._decorate_rows(db, [row])[0] if row else None

    def image_codes(self, key):
        """QR code payloads found in an image, or [] when none or not scanned yet."""
        with self.connect() as db:
            row = db.execute("SELECT codes FROM image_features WHERE item_id=?", (key,)).fetchone()
        try:
            codes = json.loads(row[0]) if row and row[0] else []
        except ValueError:
            return []
        return [code for code in codes if isinstance(code, str) and code]

    def images_needing_rescan(self):
        """Recognized images without text positions or a QR scan (from before 1.5/1.6)."""
        with self.connect() as db:
            rows = db.execute("""SELECT id FROM screenshots s WHERE item_type='image' AND ocr_status='done'
                AND (NOT EXISTS (SELECT 1 FROM image_features f WHERE f.item_id=s.id AND f.codes IS NOT NULL)
                     OR (trim(ocr_text)!='' AND NOT EXISTS (SELECT 1 FROM ocr_layouts l WHERE l.item_id=s.id)))
                ORDER BY captured_at DESC,id DESC""").fetchall()
        return [row[0] for row in rows]

    def backfill_image_hashes(self, limit=None):
        """Hash thumbnails of images saved before 1.6.0; returns how many were added."""
        with self.connect() as db:
            rows = db.execute("""SELECT s.id,s.filename FROM screenshots s LEFT JOIN image_features f ON f.item_id=s.id
                WHERE s.item_type='image' AND (f.item_id IS NULL OR f.dhash='')
                ORDER BY s.captured_at DESC LIMIT ?""", (-1 if limit is None else limit,)).fetchall()
        added = 0
        for row in rows:
            source = self.thumbs / f"{row['id']}.png"
            if not source.is_file():
                source = self.images / row["filename"]
            try:
                with Image.open(source) as opened:
                    small = opened.copy()
                small.thumbnail((480, 320))
                dhash = image_dhash(small)
            except (OSError, ValueError):
                continue
            with self.connect() as db:
                if db.execute("SELECT 1 FROM screenshots WHERE id=?", (row["id"],)).fetchone() is None:
                    continue
                db.execute("""INSERT INTO image_features(item_id,dhash) VALUES (?,?)
                    ON CONFLICT(item_id) DO UPDATE SET dhash=excluded.dhash""", (row["id"], dhash))
            added += 1
        return added

    def similarity_signature(self):
        """Changes whenever an image, its OCR text or its hash changes."""
        with self.connect() as db:
            images = db.execute("""SELECT COUNT(*),COALESCE(MAX(rowid),0),COALESCE(SUM(length(ocr_text)),0)
                FROM screenshots WHERE item_type='image'""").fetchone()
            hashes = db.execute("SELECT COUNT(*) FROM image_features WHERE dhash!=''").fetchone()[0]
        return (*tuple(images), hashes)

    def similar_groups(self):
        with self.connect() as db:
            rows = db.execute("""SELECT s.id,s.captured_at,s.width,s.height,s.ocr_text,COALESCE(f.dhash,'') AS dhash
                FROM screenshots s LEFT JOIN image_features f ON f.item_id=s.id
                WHERE s.item_type='image'""").fetchall()
        return similar_groups({"id": row["id"], "captured_at": row["captured_at"], "width": row["width"],
                               "height": row["height"], "dhash": row["dhash"], "grams": text_bigrams(row["ocr_text"])}
                              for row in rows)

    def rows_by_ids(self, keys):
        """Decorated rows in the given order; missing ids are skipped."""
        keys = list(dict.fromkeys(keys))
        found = {}
        with self.connect() as db:
            for start in range(0, len(keys), 900):
                batch = keys[start:start + 900]
                placeholders = ",".join("?" for _ in batch)
                for row in self._decorate_rows(db, db.execute(
                        f"SELECT * FROM screenshots WHERE id IN ({placeholders})", batch).fetchall()):
                    found[row["id"]] = row
        return [found[key] for key in keys if key in found]

    def ocr_lines(self, key):
        """Line boxes in original image pixels: [{x, y, w, h, text}, ...]."""
        with self.connect() as db:
            row = db.execute("SELECT lines FROM ocr_layouts WHERE item_id=?", (key,)).fetchone()
        if not row:
            return []
        try:
            lines = json.loads(row[0])
        except ValueError:
            return []
        return [line for line in lines if isinstance(line, dict) and isinstance(line.get("text"), str)
                and all(isinstance(line.get(name), (int, float)) for name in ("x", "y", "w", "h"))]

    def fail_ocr(self, key, error):
        """Record a failed attempt without replacing an earlier usable result."""
        with self.connect() as db:
            db.execute("UPDATE screenshots SET ocr_status='error',ocr_error=? WHERE id=? AND item_type='image'", (error, key))

    @staticmethod
    def _layout_json(lines):
        cleaned = []
        for line in lines or ():
            try:
                text = str(line["text"])
                box = [round(float(line[name]), 1) for name in ("x", "y", "w", "h")]
            except (KeyError, TypeError, ValueError):
                continue
            # value == value rejects NaN; the bound rejects infinities.
            if text and all(value == value and abs(value) < 1e7 for value in box) and box[2] > 0 and box[3] > 0:
                cleaned.append(dict(zip(("x", "y", "w", "h"), box), text=text))
        return json.dumps(cleaned, ensure_ascii=False)

    def finish_ocr(self, key, text, error="", lines=None, codes=None):
        if error:
            self.fail_ocr(key, error)
            return
        old_path = new_path = None
        with self.connect() as db:
            row = db.execute("SELECT rowid,* FROM screenshots WHERE id=?", (key,)).fetchone()
            if not row or row["item_type"] != "image":
                return
            if not text.strip() and row["ocr_text"].strip():
                # Keep this decision in the same transaction as the result read.
                # A blank retry must not erase searchable text or rename files.
                db.execute("UPDATE screenshots SET ocr_status='error',ocr_error=? WHERE id=?", (
                    "本次识别未返回可读文字，已保留原有识别结果。", key))
                return
            title = content_title(text) if text.strip() else row["title"]
            filename = row["filename"]
            if text.strip():
                stamp = datetime.fromisoformat(row["captured_at"]).astimezone()
                filename = f"{stamp:%Y-%m-%d_%H-%M-%S}_{title}_{key}.png"
                old_path, new_path = self.images / row["filename"], self.images / filename
                if old_path != new_path:
                    # Copy first, commit the new reference, then remove the old name.
                    # A crash at any boundary leaves at least one intact original.
                    temp = new_path.with_suffix(".tmp")
                    try:
                        shutil.copyfile(old_path, temp)
                        os.replace(temp, new_path)
                    finally:
                        temp.unlink(missing_ok=True)
            if codes is None:
                stored = db.execute("SELECT codes FROM image_features WHERE item_id=?", (key,)).fetchone()
                try:
                    known = json.loads(stored[0]) if stored and stored[0] else []
                except ValueError:
                    known = []
            else:
                known = list(dict.fromkeys(str(code)[:2000] for code in codes if str(code)))[:20]
                db.execute("""INSERT INTO image_features(item_id,codes) VALUES (?,?)
                    ON CONFLICT(item_id) DO UPDATE SET codes=excluded.codes""", (key, json.dumps(known, ensure_ascii=False)))
            # QR payloads are searchable like the text around them.
            search = normalized(" ".join([title, text, filename, *known]))
            category = row["category"] if row["category_manual"] else classify_content(text)
            db.execute("""UPDATE screenshots SET title=?,ocr_text=?,search_text=?,
                ocr_status=?,ocr_error=?,filename=?,category=? WHERE id=?""", (
                title, text, search, "done", "", filename, category, key))
            self._index_grams(db, row["rowid"], search)
            if lines:
                db.execute("INSERT OR REPLACE INTO ocr_layouts(item_id,lines) VALUES (?,?)", (key, self._layout_json(lines)))
            else:
                db.execute("DELETE FROM ocr_layouts WHERE item_id=?", (key,))
            if not row["tags_manual"] and (text != row["ocr_text"] or row["classification_status"] != "done"):
                # Re-recognizing unchanged text (e.g. to add text positions)
                # keeps the automatic topics it already has.
                db.execute("""UPDATE screenshots SET classification_status='pending',classification_error='',
                    classification_version='',classification_fingerprint='',classification_scores='{}' WHERE id=?""", (key,))
        if old_path and new_path and old_path != new_path:
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                pass  # Safe leftover, removed with this record during cleanup.

    def toggle_favorite(self, key):
        with self.connect() as db:
            db.execute("UPDATE screenshots SET favorite=1-favorite WHERE id=?", (key,))

    def set_category(self, key, category):
        if category not in CATEGORY_CODES:
            raise ValueError("未知的内容分类")
        with self.connect() as db:
            db.execute("UPDATE screenshots SET category=?,category_manual=1 WHERE id=?", (category, key))
            self._set_manual_tags(db, key, [self._legacy_topic(category)])

    def _set_manual_tags(self, db, key, tags, source="edit"):
        row = db.execute("SELECT * FROM screenshots WHERE id=?", (key,)).fetchone()
        if row is None:
            return False
        fingerprint = self._content_fingerprint(row)
        # Record what the automatic layer showed at this moment, so local
        # agreement can be measured against real decisions later.
        previous = [tag["tag"] for tag in db.execute(
            "SELECT tag FROM item_tags WHERE item_id=? ORDER BY position,tag", (key,))]
        self._write_tags(db, key, tags)
        db.execute("""UPDATE screenshots SET tags_manual=1,classification_status='manual',
            classification_error='',classification_version=?,classification_fingerprint=?,
            classification_scores='{}' WHERE id=?""", (CLASSIFICATION_VERSION, fingerprint, key))
        db.execute("""INSERT INTO classification_corrections
            (item_id,text,kind,tags_json,fingerprint,created_at,classification_version,
             previous_tags_json,previous_status,source)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (key, self._content_text(row), row["item_type"],
            json.dumps(tags, ensure_ascii=False), fingerprint, utc_now().isoformat(), CLASSIFICATION_VERSION,
            json.dumps(previous, ensure_ascii=False), row["classification_status"], source))
        return True

    def set_tags(self, key, tags, source="edit"):
        tags = self._validate_tags(tags)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._set_manual_tags(db, key, tags, source)

    def set_tags_many(self, keys, tags, source="edit"):
        tags = self._validate_tags(tags)
        keys = list(dict.fromkeys(keys))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return sum(self._set_manual_tags(db, key, tags, source) for key in keys)

    def review_queue(self, scope="unknown", limit=2000):
        """IDs for the keyboard review flow; manual decisions are never revisited."""
        condition = {
            "unknown": "NOT EXISTS (SELECT 1 FROM item_tags WHERE item_id=screenshots.id)",
            "auto": "classification_status='done'",
        }.get(scope)
        if condition is None:
            raise ValueError("未知的整理范围")
        with self.connect() as db:
            rows = db.execute(f"""SELECT id FROM screenshots WHERE tags_manual=0 AND {condition}
                ORDER BY captured_at DESC,id DESC LIMIT ?""", (limit,)).fetchall()
        return [row["id"] for row in rows]

    def classification_agreement(self):
        """Compare each item's first manual decision with the automatic tags it replaced."""
        with self.connect() as db:
            rows = db.execute("""SELECT c.tags_json,c.previous_tags_json FROM classification_corrections c
                JOIN (SELECT item_id,MIN(id) AS id FROM classification_corrections
                      WHERE previous_status='done' GROUP BY item_id) first ON first.id=c.id""").fetchall()
            manual = db.execute("SELECT COUNT(*) FROM screenshots WHERE tags_manual=1").fetchone()[0]
        reviewed = agree = 0
        added = removed = 0
        for row in rows:
            try:
                after, before = set(json.loads(row["tags_json"])), set(json.loads(row["previous_tags_json"]))
            except (TypeError, ValueError):
                continue
            reviewed += 1
            agree += after == before
            added += len(after - before)
            removed += len(before - after)
        return {"reviewed": reviewed, "agree": agree, "added": added, "removed": removed,
                "manual": manual, "trainable": len(self.training_examples())}

    def training_examples(self):
        """Current manual decisions on the seven topics, newest first."""
        with self.connect() as db:
            rows = self._decorate_rows(db, db.execute("""SELECT * FROM screenshots WHERE tags_manual=1
                ORDER BY captured_at DESC,id DESC""").fetchall())
        examples = []
        for row in rows:
            text = row["ocr_text"] if row["item_type"] == "image" else row["content"]
            if not text.strip() or any(tag not in TOPIC_LABELS for tag in row["tags"]):
                continue
            examples.append({"key": row["id"], "text": text, "kind": row["item_type"], "tags": row["tags"]})
        return examples

    def personal_head_path(self, candidate=False):
        return self.root / "models" / ("personal-head.candidate.json" if candidate else "personal-head.json")

    def write_personal_head(self, head):
        """Store a newly trained head as a candidate; nothing uses it until activated."""
        target = self.personal_head_path(candidate=True)
        target.parent.mkdir(exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(head), encoding="utf-8")
        os.replace(temporary, target)

    def activate_personal_head(self, report):
        os.replace(self.personal_head_path(candidate=True), self.personal_head_path())
        self.set_setting("personal_model", {"enabled": True, "trained_at": utc_now().isoformat(), "report": report})

    def discard_personal_candidate(self):
        self.personal_head_path(candidate=True).unlink(missing_ok=True)

    def personal_model(self):
        info = self.setting("personal_model", {})
        info = dict(info) if isinstance(info, dict) else {}
        info["available"] = self.personal_head_path().is_file()
        info["enabled"] = bool(info.get("enabled")) and info["available"]
        return info

    def set_personal_model_enabled(self, enabled):
        info = self.personal_model()
        info.pop("available", None)
        info["enabled"] = bool(enabled) and self.personal_head_path().is_file()
        self.set_setting("personal_model", info)
        return info["enabled"]

    def active_personal_head(self):
        return self.personal_head_path() if self.personal_model()["enabled"] else None

    def anchor_examples(self, limit=300):
        """Automatic items whose current model behaviour personal training should keep."""
        with self.connect() as db:
            rows = db.execute("""SELECT id,item_type,ocr_text,content FROM screenshots
                WHERE tags_manual=0 AND classification_status='done'
                ORDER BY captured_at DESC,id DESC LIMIT ?""", (limit,)).fetchall()
        return [{"key": row["id"], "kind": row["item_type"],
                 "text": row["ocr_text"] if row["item_type"] == "image" else row["content"]}
                for row in rows if (row["ocr_text"] if row["item_type"] == "image" else row["content"]).strip()]

    def reset_tags_to_automatic(self, keys):
        """Explicitly resume automatic tagging for selected items only."""
        if keys is None:
            raise ValueError("请选择要恢复自动分类的内容")
        with self.connect() as db:
            count = 0
            for key in dict.fromkeys(keys):
                count += db.execute("""UPDATE screenshots SET tags_manual=0,
                    classification_status='pending',classification_error='',classification_version='',
                    classification_fingerprint='',classification_scores='{}' WHERE id=?""", (key,)).rowcount
            return count

    def classification_input(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM screenshots WHERE id=?", (key,)).fetchone()
            if row is None or row["tags_manual"]:
                return None
            if row["item_type"] == "image" and row["ocr_status"] != "done" and not row["ocr_text"].strip():
                return None
            return {"key": key, "text": self._content_text(row), "kind": row["item_type"],
                    "fingerprint": self._content_fingerprint(row)}

    def pending_classifications(self, include_errors=False):
        statuses = ("pending", "error") if include_errors else ("pending",)
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as db:
            rows = db.execute(f"""SELECT id FROM screenshots WHERE tags_manual=0
                AND classification_status IN ({placeholders})
                AND (item_type!='image' OR ocr_status='done' OR trim(ocr_text)!='')
                ORDER BY captured_at,id""", statuses).fetchall()
        return [row["id"] for row in rows]

    def request_reclassification(self, keys=None):
        with self.connect() as db:
            if keys is None:
                return db.execute("""UPDATE screenshots SET classification_status='pending',
                    classification_error='',classification_version='',classification_fingerprint='',
                    classification_scores='{}' WHERE tags_manual=0""").rowcount
            count = 0
            for key in dict.fromkeys(keys):
                count += db.execute("""UPDATE screenshots SET classification_status='pending',
                    classification_error='',classification_version='',classification_fingerprint='',
                    classification_scores='{}' WHERE id=? AND tags_manual=0""", (key,)).rowcount
            return count

    def apply_classification(self, key, tags, scores, expected_fingerprint, version=CLASSIFICATION_VERSION):
        tags = self._validate_tags(tags)
        encoded_scores = json.dumps(scores, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM screenshots WHERE id=?", (key,)).fetchone()
            if (row is None or row["tags_manual"] or row["classification_status"] not in {"pending", "error"}
                    or self._content_fingerprint(row) != expected_fingerprint):
                return False
            self._write_tags(db, key, tags)
            db.execute("""UPDATE screenshots SET classification_status='done',classification_error='',
                classification_version=?,classification_fingerprint=?,classification_scores=? WHERE id=?""",
                (version, expected_fingerprint, encoded_scores, key))
            return True

    def fail_classification(self, key, error, expected_fingerprint, version=CLASSIFICATION_VERSION):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM screenshots WHERE id=?", (key,)).fetchone()
            if (row is None or row["tags_manual"] or row["classification_status"] not in {"pending", "error"}
                    or self._content_fingerprint(row) != expected_fingerprint):
                return False
            db.execute("""UPDATE screenshots SET classification_status='error',classification_error=?,
                classification_version=?,classification_fingerprint=? WHERE id=?""",
                (str(error), version, expected_fingerprint, key))
            return True

    def topic_counts(self):
        counts = dict.fromkeys(ALL_TOPIC_LABELS, 0)
        with self.connect() as db:
            for row in db.execute("SELECT tag,COUNT(*) AS count FROM item_tags GROUP BY tag"):
                if row["tag"] in counts:
                    counts[row["tag"]] = row["count"]
            counts["unknown"] = db.execute("""SELECT COUNT(*) FROM screenshots
                WHERE NOT EXISTS (SELECT 1 FROM item_tags WHERE item_id=screenshots.id)""").fetchone()[0]
        return counts

    def category_counts(self):
        counts = dict.fromkeys(CATEGORY_CODES, 0)
        with self.connect() as db:
            for row in db.execute("SELECT category,COUNT(*) AS count FROM screenshots GROUP BY category"):
                if row["category"] in counts:
                    counts[row["category"]] = row["count"]
        return counts

    def type_counts(self):
        counts = dict.fromkeys(("image", "text", "link"), 0)
        with self.connect() as db:
            for row in db.execute("SELECT item_type,COUNT(*) AS count FROM screenshots GROUP BY item_type"):
                if row["item_type"] in counts:
                    counts[row["item_type"]] = row["count"]
        return counts

    def query(self, search="", scope="all", limit=120, offset=0, now=None, *, kind="all", sort="newest", category="all", item_type="all", topic="all"):
        clauses, args = [], []
        kind_filters = {
            "text": "ocr_text!=''",
            "pending": "item_type='image' AND ocr_status='pending'",
            "error": "item_type='image' AND ocr_status='error'",
            "favorites": "favorite=1",
            "images": "item_type='image'",
            "plaintext": "item_type='text'",
            "links": "item_type='link'",
        }
        # Only fixed SQL fragments can come from UI filters or persisted values.
        # Unknown values keep the original, unfiltered/newest-first behavior.
        if isinstance(kind, str) and kind in kind_filters:
            clauses.append(kind_filters[kind])
        if isinstance(category, str) and category in CATEGORY_CODES:
            clauses.append("category=?")
            args.append(category)
        if isinstance(item_type, str) and item_type in {"image", "text", "link"}:
            clauses.append("item_type=?")
            args.append(item_type)
        if topic == "unknown":
            clauses.append("NOT EXISTS (SELECT 1 FROM item_tags WHERE item_id=screenshots.id)")
        elif isinstance(topic, str) and topic in ALL_TOPIC_LABELS:
            clauses.append("EXISTS (SELECT 1 FROM item_tags WHERE item_id=screenshots.id AND tag=?)")
            args.append(topic)
        direction = "ASC" if sort == "oldest" else "DESC"
        terms = normalized(search).split()
        long_terms = [term for term in terms if len(term) >= 3]
        if long_terms:
            clauses.append("rowid IN (SELECT rowid FROM search_index WHERE search_index MATCH ?)")
            args.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
        short_terms = [term for term in terms if _indexable_short_term(term)]
        if short_terms:
            clauses.append("rowid IN (SELECT rowid FROM search_grams WHERE search_grams MATCH ?)")
            args.append(" AND ".join(f'"{term}"' for term in short_terms))
        for term in terms:
            clauses.append("instr(search_text, ?) > 0")
            args.append(term)
        local = (now or utc_now()).astimezone()
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if scope == "favorites":
            clauses.append("favorite=1")
        elif scope in ("today", "yesterday", "week"):
            start = midnight if scope == "today" else midnight - timedelta(days=1 if scope == "yesterday" else 6)
            clauses.append("captured_at>=?")
            args.append(start.astimezone(timezone.utc).isoformat())
            if scope == "yesterday":
                clauses.append("captured_at<?")
                args.append(midnight.astimezone(timezone.utc).isoformat())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = f"captured_at {direction},id {direction}"
        if sort == "relevance" and terms:
            # A title hit outweighs body hits; repeated body hits are capped so a
            # long document cannot bury a short exact match. Ties stay newest first.
            scores = []
            for term in terms:
                scores.append("(instr(lower(title),?)>0)*4"
                              "+min(10,(length(search_text)-length(replace(search_text,?,'')))/length(?))")
                args.extend((term, term, term))
            order = f"({'+'.join(scores)}) DESC,captured_at DESC,id DESC"
        with self.connect() as db:
            rows = db.execute("SELECT * FROM screenshots" + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
                              (*args, limit, offset)).fetchall()
            return self._decorate_rows(db, rows)

    def pending(self, include_errors=False):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM screenshots WHERE item_type='image' AND (ocr_status='pending'" +
                              (" OR ocr_status='error')" if include_errors else ")")).fetchall()
        return [r[0] for r in rows]

    def _delete_row(self, db, key, leftovers):
        row = db.execute("SELECT rowid,filename,item_type FROM screenshots WHERE id=?", (key,)).fetchone()
        if not row:
            return False
        if row["item_type"] == "image":
            if row["filename"]:
                (self.images / row["filename"]).unlink(missing_ok=True)
            # Includes copies left by an interrupted content-derived rename.
            for leftover in leftovers:
                leftover.unlink(missing_ok=True)
            (self.thumbs / f"{key}.png").unlink(missing_ok=True)
        db.execute("DELETE FROM search_grams WHERE rowid=?", (row["rowid"],))
        db.execute("DELETE FROM screenshots WHERE id=?", (key,))
        return True

    def delete(self, key):
        # Hold the same lock as OCR/favorites so cleanup cannot race a new favorite.
        with self.connect() as db:
            self._delete_row(db, key, self.images.glob(f"*_{key}.png"))

    def _image_files_by_key(self):
        found = {}
        with os.scandir(self.images) as entries:
            for entry in entries:
                match = _KEY_SUFFIX.search(entry.name)
                if match:
                    found.setdefault(match[1], []).append(Path(entry.path))
        return found

    def cleanup(self, now=None):
        days = int(self.setting("retention_days", 7))
        if days <= 0:
            return 0
        cutoff = ((now or utc_now()) - timedelta(days=days)).isoformat()
        removed = 0
        with self.lock:
            with self.connect() as db:
                ids = [r[0] for r in db.execute("SELECT id FROM screenshots WHERE favorite=0 AND captured_at<?", (cutoff,))]
            if not ids:
                return 0
            # One directory scan for the whole batch instead of one per record.
            files = self._image_files_by_key()
            for key in ids:
                try:
                    with self.connect() as db:
                        removed += self._delete_row(db, key, files.get(key, ()))
                except OSError:
                    continue  # A file held open elsewhere is retried by the next cleanup.
        return removed

    def stats(self):
        today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        with self.connect() as db:
            row = db.execute("""SELECT COUNT(*) AS total, COALESCE(SUM(size),0) AS size,
                COALESCE(SUM(favorite),0) AS favorites,
                COALESCE(SUM(captured_at>=?),0) AS today,
                COALESCE(SUM(captured_at>=?),0) AS week,
                COALESCE(SUM(captured_at>=? AND captured_at<?),0) AS yesterday,
                COALESCE(SUM(ocr_text!=''),0) AS text,
                COALESCE(SUM(item_type='image' AND ocr_status='pending'),0) AS pending,
                COALESCE(SUM(item_type='image' AND ocr_status='error'),0) AS error FROM screenshots""",
                (today.astimezone(timezone.utc).isoformat(),
                 (today-timedelta(days=6)).astimezone(timezone.utc).isoformat(),
                 (today-timedelta(days=1)).astimezone(timezone.utc).isoformat(),
                 today.astimezone(timezone.utc).isoformat())).fetchone()
        return dict(row)

    def checkpoint(self):
        with self.connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
