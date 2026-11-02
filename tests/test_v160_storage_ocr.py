"""1.6.0 storage and OCR: QR payloads, thumbnail hashes, similar groups, rescans."""
from datetime import datetime, timedelta, timezone
import io
import json
import random
import sqlite3
import sys
import time
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

from snapvault import ocr, ocr_runtime, storage, workers
from snapvault.storage import Vault, image_dhash, images_similar, similar_groups, text_bigrams


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault")


def screenshot(seed, size=(400, 300)):
    rng = random.Random(seed)
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for _ in range(25):
        x, y = rng.randrange(size[0] - 40), rng.randrange(size[1] - 20)
        draw.rectangle((x, y, x + rng.randrange(10, 40), y + rng.randrange(5, 20)), fill=rng.choice(["black", "navy", "gray"]))
    return image


def entry(key, text="", dhash="", size=(1080, 1920), stamp="2026-10-01T00:00:00+00:00"):
    return {"id": key, "captured_at": stamp, "width": size[0], "height": size[1], "dhash": dhash, "grams": text_bigrams(text)}


ARTICLE = "今天下午三点在会议室讨论第四季度的预算安排，请各部门提前准备好材料并发送给财务同事确认"


def test_dhash_ignores_recompression_but_not_different_content():
    original = screenshot(1)
    buffer = io.BytesIO()
    original.save(buffer, "JPEG", quality=70)
    recompressed = Image.open(io.BytesIO(buffer.getvalue()))
    first, second, other = image_dhash(original), image_dhash(recompressed), image_dhash(screenshot(2))
    assert len(first) == 64
    assert (int(first, 16) ^ int(second, 16)).bit_count() <= 10
    assert (int(first, 16) ^ int(other, 16)).bit_count() > 40


def test_similarity_rules_for_text_and_textless_screenshots():
    assert images_similar(entry("a", ARTICLE), entry("b", ARTICLE + "收到"))
    # A longer screenshot that contains the shorter one is still a repeat.
    assert images_similar(entry("a", ARTICLE), entry("b", ARTICLE + "另外下周一上午还有一个关于招聘计划的短会"))
    assert not images_similar(entry("a", ARTICLE), entry("b", "完全不同的一段文字，讲的是周末去爬山时看到的风景和路上的见闻"))
    assert not images_similar(entry("a", ARTICLE), entry("b", ARTICLE, size=(1920, 400)))  # very different shape
    assert not images_similar(entry("a", ARTICLE), entry("b", "", dhash="0" * 64))  # text versus no text
    same, near, far = "f" * 64, "f" * 60 + "0000", "0" * 64
    assert images_similar(entry("a", dhash=same), entry("b", dhash=near))
    assert not images_similar(entry("a", dhash=same), entry("b", dhash=far))
    assert not images_similar(entry("a", dhash=same), entry("b", dhash=same, size=(1080, 1500)))
    assert not images_similar(entry("a", dhash=""), entry("b", dhash=""))


def test_grouping_matches_brute_force_and_orders_newest_first():
    rng = random.Random(7)
    alphabet = "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处理府研质"
    texts = ["".join(rng.choice(alphabet) for _ in range(rng.randrange(40, 120))) for _ in range(40)]
    images = []
    for index, text in enumerate(texts):
        stamp = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat()
        images.append(entry(f"t{index}", text, stamp=stamp))
        if index % 5 == 0:  # a near repeat with a small change
            images.append(entry(f"t{index}-again", text[:-3] + "新消息", stamp=stamp.replace("00+00:00", "30+00:00")))
    for index in range(6):
        images.append(entry(f"plain{index}", "", dhash=f"{index:02x}" * 32, size=(800, 600)))
    images.append(entry("plain0-again", "", dhash="00" * 31 + "01", size=(800, 600)))
    groups = similar_groups(images)
    brute = {frozenset((a["id"], b["id"])) for i, a in enumerate(images) for b in images[i + 1:] if images_similar(a, b)}
    found = {frozenset((x, y)) for group in groups for i, x in enumerate(group) for y in group[i + 1:]}
    assert brute <= found
    assert {frozenset(group) for group in groups} >= {frozenset({"t0", "t0-again"}), frozenset({"plain0", "plain0-again"})}
    newest = [group[0] for group in groups]
    assert newest == sorted(newest, key=lambda key: next(image["captured_at"] for image in images if image["id"] == key), reverse=True)


def test_grouping_stays_fast_on_a_large_library():
    rng = random.Random(3)
    alphabet = [chr(code) for code in range(0x4E00, 0x4E00 + 1500)]
    images = [entry(f"k{index}", "".join(rng.choice(alphabet) for _ in range(rng.randrange(80, 400))))
              for index in range(1500)]
    started = time.monotonic()
    similar_groups(images)
    assert time.monotonic() - started < 20


def test_added_images_are_hashed_and_old_ones_backfilled(vault):
    key, _ = vault.add(screenshot(5))
    with vault.connect() as db:
        assert len(db.execute("SELECT dhash FROM image_features WHERE item_id=?", (key,)).fetchone()[0]) == 64
        db.execute("DELETE FROM image_features")
    assert vault.backfill_image_hashes() == 1
    assert vault.backfill_image_hashes() == 0
    with vault.connect() as db:
        assert db.execute("SELECT dhash FROM image_features WHERE item_id=?", (key,)).fetchone()[0] == image_dhash(
            Image.open(vault.thumbs / f"{key}.png"))


def test_similar_groups_from_the_library_and_signature(vault):
    a, _ = vault.add(screenshot(1, (300, 500)))
    b, _ = vault.add(screenshot(2, (300, 500)))
    c, _ = vault.add(screenshot(3, (300, 500)))
    vault.finish_ocr(a, ARTICLE)
    signature = vault.similarity_signature()
    vault.finish_ocr(b, ARTICLE + "好的")
    assert vault.similarity_signature() != signature
    vault.finish_ocr(c, "另一张截图里是完全不同的内容，讲的是旅行路线和酒店预订的安排")
    groups = vault.similar_groups()
    assert [set(group) for group in groups] == [{a, b}]
    assert [row["id"] for row in vault.rows_by_ids([b, "missing", a])] == [b, a]


def test_qr_payloads_are_stored_searchable_and_kept_on_plain_reocr(vault):
    key, _ = vault.add(screenshot(9))
    vault.finish_ocr(key, "扫码加入群聊", codes=["https://u.wechat.com/abc", "https://u.wechat.com/abc", ""])
    assert vault.image_codes(key) == ["https://u.wechat.com/abc"]
    assert [row["id"] for row in vault.query("u.wechat")] == [key]
    vault.finish_ocr(key, "扫码加入群聊")  # an older caller without codes keeps them
    assert vault.image_codes(key) == ["https://u.wechat.com/abc"]
    assert [row["id"] for row in vault.query("wechat.com")] == [key]
    vault.finish_ocr(key, "扫码加入群聊", codes=[])
    assert vault.image_codes(key) == [] and vault.query("wechat.com") == []


def test_reocr_with_unchanged_text_keeps_automatic_topics(vault):
    key, _ = vault.add(screenshot(4))
    vault.finish_ocr(key, "Python 接口文档")
    snapshot = vault.classification_input(key)
    assert vault.apply_classification(key, ["tech"], {}, snapshot["fingerprint"])
    vault.finish_ocr(key, "Python 接口文档", lines=[{"x": 1, "y": 1, "w": 5, "h": 5, "text": "Python"}], codes=[])
    assert vault.get(key)["classification_status"] == "done" and vault.get(key)["tags"] == ["tech"]
    vault.finish_ocr(key, "完全不同的新文字")
    assert vault.get(key)["classification_status"] == "pending"


def test_images_needing_rescan(vault):
    old, _ = vault.add(screenshot(1))
    vault.finish_ocr(old, "旧版识别的文字")              # no layout, never scanned for QR
    blank, _ = vault.add(screenshot(2))
    vault.finish_ocr(blank, "", codes=[])                 # scanned, no text: nothing to add
    fresh, _ = vault.add(screenshot(3))
    vault.finish_ocr(fresh, "新文字", lines=[{"x": 1, "y": 1, "w": 5, "h": 5, "text": "新文字"}], codes=[])
    pending, _ = vault.add(screenshot(4))                 # not recognized yet
    assert vault.images_needing_rescan() == [old]


def test_v160_upgrade_backs_up_once(vault):
    vault.add_text("需要保护的内容")
    with vault.connect() as db:
        db.execute("DELETE FROM settings WHERE key='_schema_v160_migrated'")
    Vault(vault.root)
    Vault(vault.root)
    assert sorted(path.name for path in (vault.root / "backups").glob("*.sqlite3")) == ["before-1.6.0-upgrade.sqlite3"]


def qr_image(path, payload, height=2600):
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    code = cv2.QRCodeEncoder.create().encode(payload)
    code = cv2.resize(code, (code.shape[1] * 6, code.shape[0] * 6), interpolation=cv2.INTER_NEAREST)
    canvas = numpy.full((height, 900), 255, numpy.uint8)
    canvas[height - code.shape[0] - 100:height - 100, 300:300 + code.shape[1]] = code
    cv2.imencode(".png", canvas)[1].tofile(str(path))


def test_real_qr_decoding_in_a_tall_screenshot(tmp_path):
    path = tmp_path / "长截图 二维码.png"
    qr_image(path, "https://example.com/拾影?from=qr")
    assert ocr_runtime.OcrEngine().decode_codes(path) == ["https://example.com/拾影?from=qr"]
    plain = tmp_path / "plain.png"
    Image.new("RGB", (300, 200), "white").save(plain)
    assert ocr_runtime.OcrEngine().decode_codes(plain) == []
    assert ocr_runtime.OcrEngine().decode_codes(tmp_path / "missing.png") == []


def test_worker_returns_codes_and_tolerates_scan_failures(tmp_path, monkeypatch):
    image = tmp_path / "input.png"
    Image.new("RGB", (40, 20), "white").save(image)
    for decode, expected in ((lambda path: ["https://qr.example"], ["https://qr.example"]),
                             (lambda path: (_ for _ in ()).throw(RuntimeError("native")), [])):
        channel = tmp_path / f"channel{len(expected)}"
        channel.mkdir()
        ocr_runtime.write_worker_identity(channel / "owner.json")
        (channel / "request.json").write_text(json.dumps({"request_id": "r", "input": str(image)}), encoding="utf-8")
        engine = SimpleNamespace(recognize=lambda path: ("文字", []), decode_codes=decode)
        monkeypatch.setattr(ocr_runtime, "OcrEngine", lambda: engine)
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        assert ocr_runtime.serve_main(["--ocr-worker", "--channel-dir", str(channel), "--max-jobs", "1"]) == 0
        result = json.loads((channel / "response.json").read_text(encoding="utf-8"))
        assert result["ok"] and result["codes"] == expected


def test_queue_saves_codes_from_the_session(vault, monkeypatch):
    key, _ = vault.add(screenshot(6))
    worker = workers.OcrWorker(vault)
    monkeypatch.setattr(ocr, "check_availability", lambda: None)

    class Session:
        def recognize(self, path, *, stop_event=None):
            return {"text": "扫码", "lines": [], "codes": ["WIFI:S:home;;"]}

        def close(self):
            pass

    monkeypatch.setattr(ocr, "OcrSession", Session)
    finish = vault.finish_ocr

    def complete(*args, **kwargs):
        finish(*args, **kwargs)
        worker.stopping.set()

    monkeypatch.setattr(vault, "finish_ocr", complete)
    worker.enqueue(key)
    worker.run()
    assert vault.image_codes(key) == ["WIFI:S:home;;"]


def test_capture_queue_reports_tags_and_backfills_hashes(vault):
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    worker = workers.CaptureWorker(vault)
    tagged, hashed, saved = [], [], []
    worker.saved.connect(lambda key, created: saved.append(key))
    worker.tagged.connect(lambda tag, key, created: tagged.append((tag, key, created)))
    worker.hashed.connect(hashed.append)
    image = QImage(50, 40, QImage.Format.Format_RGBA8888)
    image.fill(0xFF336699)
    assert worker.submit("image", image, tag=7)
    assert worker.submit("text", "未加标记的文字")
    assert worker.submit("hashes")
    assert worker.submit("stop")
    worker.run()
    assert len(saved) == 2 and tagged == [(7, saved[0], True)] and hashed == [0]
