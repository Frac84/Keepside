"""Production classifier + native gallery verification with synthetic data only."""
from __future__ import annotations

import json
from pathlib import Path
import time
import traceback
import uuid

from PIL import Image
from PySide6.QtCore import Qt, QTimer

from .storage import Vault
from .ui import MainWindow


def run_topic_diagnostic(app, destination):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run = root / ("topics-run-" + uuid.uuid4().hex[:8])
    vault = Vault(run / "data")
    vault.set_setting("retention_days", 0)
    fixtures = [
        ("log", "2026-10-19 18:35:22 ERROR Connection timed out while connecting to PostgreSQL at 127.0.0.1:5432", {"tech"}),
        ("receipt", '{"商品":"机械键盘","订单号":"TEST-20261019","金额":399,"状态":"已付款"}', {"receipts"}),
        ("css", "这版深色 UI 的 CSS：\n:root { --canvas: #121824; --card: #1E2640; --accent: #00E676; }\n卡片和背景拉开明度。", {"tech", "design"}),
        ("chat", "小林：周五下午三点一起去图书馆还书，记得带借书证。\n小陈：好的，我已经把时间记下了。", {"chat", "tasks"}),
        ("unknown", "https://example.org/r/8a7f2c?source=clipboard", set()),
        ("short", "可以，这个还行。", set()),
        ("learning", "英语语法复习笔记：现在完成时表示过去发生的动作对现在造成的影响，常与 already 和 yet 连用。", {"learning"}),
        ("design", "海报配色参考：以米白纸色为底，用低饱和灰绿强调标题，正文和说明拉开字号与行距。", {"design"}),
        ("media", "电影观影记录：这部科幻片的配乐非常好听，结尾镜头呼应开场，周末准备再看一次。", {"media"}),
    ]
    keys = {}
    originals = {}
    for name, text, _ in fixtures:
        keys[name], _ = vault.add_text(text)
        originals[keys[name]] = vault.get(keys[name])["content"]
    manual, _ = vault.add_text("人工选择的记录，即使主题不明显也要保留。")
    vault.set_tags(manual, ["design"])
    empty_manual, _ = vault.add_text("def manual_pending():\n    return True")
    vault.set_tags(empty_manual, [])
    legacy, _ = vault.add_text("旧版手动阅读收藏")
    vault.set_category(legacy, "read")
    image, _ = vault.add(Image.new("RGB", (420, 160), "#dde8ed"))
    vault.finish_ocr(image, "课程复习提纲：光合作用把光能转化为化学能，叶绿体是主要场所。")
    window = MainWindow(vault, listen=False, use_tray=False, browser_tools=False)
    window.resize(1440, 900)
    window.show()
    result = {"ok": False, "synthetic_only": True, "clipboard_content_read_or_written": False,
              "browser_input_started": False, "run_directory": str(run), "checks": []}
    started = time.monotonic()
    phase = "classifying"
    saved_fingerprint = None
    timer = QTimer(window)

    def persist():
        (root / "classification-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def select(key):
        window.set_topic("all")
        window.set_item_type("all")
        window.refresh()
        for i in range(window.gallery.count()):
            if window.gallery.item(i).data(Qt.ItemDataRole.UserRole)["id"] == key:
                window.gallery.setCurrentRow(i)
                app.processEvents()
                return
        raise AssertionError("Synthetic item missing from gallery")

    def finish(error=None):
        timer.stop()
        if error:
            result["error"] = error
        result["seconds"] = round(time.monotonic() - started, 3)
        persist()
        window.quit_app()

    def inspect():
        nonlocal phase, saved_fingerprint
        try:
            if time.monotonic() - started > 75:
                raise AssertionError("Classification diagnostic timed out")
            if phase == "classifying":
                rows = {row["id"]: row for row in vault.query(limit=100)}
                if any(row["classification_status"] == "pending" for row in rows.values()):
                    return
                assert not [r["id"] for r in rows.values() if r["classification_status"] == "error"], "Local model failed"
                for name, _, expected in fixtures:
                    assert set(rows[keys[name]]["tags"]) == expected, name + ": tags differ"
                assert rows[manual]["tags"] == ["design"] and rows[manual]["tags_manual"]
                assert rows[empty_manual]["tags"] == [] and rows[empty_manual]["tags_manual"]
                assert rows[legacy]["tags"] == ["legacy_read"] and rows[legacy]["category_manual"]
                assert set(rows[image]["tags"]) == {"learning"}
                assert all(rows[key]["content"] == content for key, content in originals.items())
                result["checks"] += ["real offline model classifies text and existing image OCR",
                                     "JSON receipt / timestamp error / CSS dual tags / conversation + task",
                                     "opaque URL and uninformative short text stay untagged",
                                     "manual tags, manual empty tags and old manual categories survive",
                                     "original text and URLs remain byte-for-byte unchanged"]
                window.refresh()
                app.processEvents()
                assert window.grab().save(str(root / "classification-gallery.png"))
                window.set_topic("design")
                window.set_item_type("text")
                assert window.gallery.count() >= 3
                window.set_topic("unknown")
                assert window.gallery.count() == 2  # The opaque URL is a separate type.
                window.set_item_type("all")
                assert window.gallery.count() == 3
                select(keys["css"])
                assert window.grab().save(str(root / "classification-detail.png"))
                result["checks"].append("topic + content type filtering and native card/detail rendering")
                # Verify guarded stale results through the actual production vault.
                snapshot = vault.classification_input(keys["log"])
                vault.set_tags(keys["log"], ["tasks"])
                assert not vault.apply_classification(keys["log"], ["tech"], {}, snapshot["fingerprint"])
                window.restore_auto_classification([keys["log"]])
                saved_fingerprint = snapshot["fingerprint"]
                phase = "restoring"
            elif phase == "restoring":
                row = vault.get(keys["log"])
                if row["classification_status"] == "pending":
                    return
                assert row["tags"] == ["tech"] and not row["tags_manual"]
                assert row["classification_fingerprint"] == saved_fingerprint
                result["checks"].append("late automatic result cannot overwrite a manual choice; explicit restore reclassifies")
                window.reclassify_all()
                assert list((vault.root / "backups").glob("before-classification-*.sqlite3"))
                phase = "reclassifying"
            elif phase == "reclassifying":
                if vault.pending_classifications():
                    return
                assert vault.get(manual)["tags"] == ["design"] and vault.get(empty_manual)["tags_manual"]
                result["checks"].append("reclassify-all makes a database backup and preserves manual selections")
                result["topic_counts"] = vault.topic_counts()
                result["ok"] = True
                finish()
        except Exception:
            finish(traceback.format_exc())

    timer.timeout.connect(inspect)
    timer.start(100)
    app.exec()
    result["workers_exited"] = not any(worker.isRunning() for worker in (window.capture, window.ocr, window.classifier))
    persist()
    return 0 if result["ok"] and result["workers_exited"] else 1
