"""Synthetic end-to-end verification, isolated from the user's real vault.

The smoke test never reads or writes the system clipboard. A separate integration
test covers real clipboard events and restores the previous clipboard afterward.
"""
from datetime import timedelta
import json
from pathlib import Path
import time
import traceback

from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QTabWidget

from .storage import Vault, utc_now
from .ui import MainWindow
from .inspector import ImageInspector
from .branding import APP_NAME, app_icon


def fixture(title, lines, background="#f8fafc", accent="#386de5"):
    image = Image.new("RGB", (1000, 620), background)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 32)
    heading = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 45)
    draw.rounded_rectangle((40, 35, 960, 100), radius=12, fill=accent)
    for x in (68, 102, 136):
        draw.ellipse((x, 58, x+14, 72), fill="white")
    draw.text((55, 145), title, font=heading, fill="#23304a")
    for i, line in enumerate(lines):
        draw.text((55, 245+i*60), line, font=font, fill="#536078")
    return image


def run_smoke(app, destination):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Each run gets a new isolated vault; previous QA evidence is preserved.
    import uuid
    vault = Vault(root / ("test-data-" + uuid.uuid4().hex[:8]))
    vault.set_setting("retention_days", 0)
    samples = [
        ("Gemini connection timeout", ["gemini.google.com", "ERROR_CONNECTION_TIMED_OUT", "Please check your network connection."], "#eef3fd", "#4876d4"),
        ("截图回收站：记住重要的事", ["本地保存，安心查找", "学校作业：周五完成项目报告", "中文和 English 都可以搜索"], "#f4f6ee", "#66896c"),
        ("Build successful", ["All 24 checks passed", "Local workspace / " + APP_NAME, "Ready for the next idea."], "#f2f6f9", "#496578"),
        ("周末的小计划", ["买咖啡豆，整理照片", "阅读：Designing Data-Intensive Applications", "别忘了给重要截图加星标"], "#fff7ea", "#b88b48"),
        ("API reference", ["POST /v1/screenshots", "Content-Type: image/png", "201 Created"], "#f2f0fa", "#8271b0"),
        ("灵感随手记", ["Never lose a screenshot again.", "Capture. Remember. Find.", "所有数据都留在这台电脑里。"], "#edf7f6", "#4c918d"),
    ]
    keys = []
    for i, (title, lines, bg, accent) in enumerate(samples):
        key, _ = vault.add(fixture(title, lines, bg, accent), utc_now()-timedelta(hours=i*5))
        keys.append(key)
    vault.toggle_favorite(keys[0])
    vault.toggle_favorite(keys[3])
    window = MainWindow(vault, listen=False, use_tray=False, browser_tools=False)
    window.resize(1430, 900)
    window.show()
    started = time.monotonic()
    result = {"ok": False}
    test_errors = []
    window.capture.failed.connect(test_errors.append)
    # Exercise QImage -> worker -> PNG storage independently of seeded samples.
    probe = QImage(77, 43, QImage.Format.Format_ARGB32)
    probe.fill(0xffd9eaf8)
    window.capture.submit("image", probe)
    window.capture.submit("image", probe.copy())

    def finish():
        try:
            rows = vault.query(limit=100)
            if (len(rows) != 7 or any(r["ocr_status"] == "pending" for r in rows)) and time.monotonic()-started < 50:
                return
            timer.stop()
            assert not test_errors, test_errors
            assert len(rows) == 7, len(rows)
            assert all(r["ocr_status"] == "done" for r in rows), [(r["title"], r["ocr_status"], r["ocr_error"]) for r in rows]
            assert vault.query("gemini connection"), "English full text query failed"
            assert vault.query("学校"), "Chinese query failed"
            # Keep the visual QA gallery focused on the six content fixtures.
            probe_row = next(r for r in rows if r["width"] == 77)
            vault.delete(probe_row["id"])
            window.active_id = keys[0]
            window.refresh()
            window.gallery.setCurrentRow(next(i for i in range(window.gallery.count()) if window.gallery.item(i).data(256)["id"] == keys[0]))
            app.processEvents()
            window.grab().save(str(root / "snapvault-preview.png"))
            window.search.setText("学校")
            window.refresh()
            assert window.gallery.count() >= 1
            window.grab().save(str(root / "search-preview.png"))
            window.set_scope("favorites")
            window.search.clear()
            window.refresh()
            assert window.gallery.count() == 2
            def capture_settings():
                dialog = app.activeModalWidget()
                tabs = dialog.findChild(QTabWidget)
                tabs.setCurrentIndex(1)
                app.processEvents()
                dialog.grab().save(str(root / "browser-settings-preview.png"))
                dialog.reject()
            QTimer.singleShot(100, capture_settings)
            window.settings_dialog()
            window.set_scope("all")
            window.refresh()
            window.show_page("status")
            app.processEvents()
            window.grab().save(str(root / "status-preview.png"))
            window.show_page("preferences")
            app.processEvents()
            window.grab().save(str(root / "capture-settings-preview.png"))
            window.preferences_panel.cancel()
            window.search.setText("no-matching-fixture-q8z")
            window.refresh()
            app.processEvents()
            assert window.gallery.count() == 0 and window.empty.isVisible()
            window.grab().save(str(root / "no-results-preview.png"))
            window.search.clear()
            window.refresh()
            inspector = ImageInspector(window)
            inspector.show()
            app.processEvents()
            inspector.view.actual_size()
            assert inspector.view.has_image
            assert abs(inspector.view.zoom_percent - 100) < 0.1
            inspector.navigate(1)
            assert window.gallery.currentRow() == 1
            app.processEvents()
            inspector.grab().save(str(root / "inspector-preview.png"))
            inspector.reject()
            assert not app_icon().isNull(), "Application icon missing from build"
            assert APP_NAME in window.windowTitle()
            original = vault.get(window.active_id)
            source_bytes = (vault.images / original["filename"]).read_bytes()
            pin = window.pin_image()
            assert pin is not None and pin.parent() is None
            assert pin.image_size.width() == original["width"]
            pin.set_scale(0.7)
            pin.set_opacity(0.8)
            app.processEvents()
            pin.toolbar.show()
            pin.grab().save(str(root / "pinned-preview.png"))
            window.hide()
            app.processEvents()
            assert pin.isVisible(), "Hiding the library hid the independent pin"
            assert abs(pin.opacity - 0.8) < 0.01
            assert (vault.images / original["filename"]).read_bytes() == source_bytes
            window.close_all_pins()
            app.processEvents()
            assert not window.pinned_windows
            window.show()
            window.resize(1080, 700)
            app.processEvents()
            app.processEvents()
            window.grab().save(str(root / "narrow-preview.png"))
            result.update(ok=True, total=len(rows), languages=window.ocr_languages,
                          checks=["QImage capture", "pixel deduplication", "offline RapidOCR in isolated processes",
                                  "Chinese and English search", "favorites filter", "gallery and detail rendering",
                                  "unified settings rendering", "embedded settings and service status",
                                  "no-results state", "original-image inspector navigation and 100% zoom",
                                  "pinned image zoom, opacity, independent lifetime and original preservation",
                                  "packaged application name and icon",
                                  "narrow-window rendering"])
        except Exception:
            result["error"] = traceback.format_exc()
        finally:
            if not timer.isActive():
                (root / "smoke-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                window.quit_app()

    timer = QTimer(window)
    timer.timeout.connect(finish)
    timer.start(250)
    app.exec()
    return 0 if result["ok"] else 1
