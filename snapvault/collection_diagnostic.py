"""Isolated mixed-content QA; never read or write the system clipboard."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import traceback
import uuid

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QImage

from . import ui
from .classification import CATEGORY_CODES
from .branding import asset_path
from .smoke import fixture
from .storage import Vault
from .theme import THEMES, theme


class _SyntheticClipboard(QObject):
    """An in-memory destination for the application's copy-original action."""

    dataChanged = Signal()

    def __init__(self):
        super().__init__()
        self.last_text = None

    def setText(self, value):
        self.last_text = value
        self.dataChanged.emit()

    def setImage(self, _value):
        raise AssertionError("The collection diagnostic must not copy an image")

    def mimeData(self):
        raise AssertionError("The collection diagnostic must not read clipboard data")

    def text(self):
        raise AssertionError("The collection diagnostic must not read clipboard text")

    def image(self):
        raise AssertionError("The collection diagnostic must not read clipboard images")


def run_collection_diagnostic(app, destination, include_themes=False):
    """Save synthetic mixed content, run real image OCR, and render the UI."""
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    result_path = root / "collection-result.json"
    run_root = root / ("collection-run-" + uuid.uuid4().hex[:8])
    run_root.mkdir()
    result = {"ok": False, "synthetic_only": True, "system_clipboard_accessed": False,
              "browser_input_started": False, "run_directory": str(run_root), "checks": []}
    window = None
    timer = None
    clipboard = _SyntheticClipboard()
    original_application = ui.QApplication
    original_sequence = ui.MainWindow.__dict__["sequence"]
    original_policy = ui.should_exclude_clipboard
    original_font = app.font()
    loaded_fonts = []

    class DiagnosticApplication:
        @staticmethod
        def clipboard():
            return clipboard

        @staticmethod
        def instance():
            return app

        @staticmethod
        def quit():
            app.quit()

    def persist():
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_root / "collection-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        # Neither Qt's clipboard accessor nor native marker/sequence APIs are
        # consulted in this diagnostic. The normal application is unchanged.
        ui.QApplication = DiagnosticApplication
        ui.MainWindow.sequence = staticmethod(lambda: 0)
        ui.should_exclude_clipboard = lambda mime=None: False
        if app.platformName() == "offscreen":
            # Qt's offscreen plugin does not enumerate Windows fonts reliably.
            # Load local system fonts only for readable diagnostic captures.
            font_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for name in ("segoeui.ttf", "segoeuib.ttf", "msyh.ttc", "msyhbd.ttc", "consola.ttf"):
                path = font_root / name
                if path.is_file():
                    font_id = QFontDatabase.addApplicationFont(str(path))
                    if font_id >= 0:
                        loaded_fonts.append(font_id)
            app.setFont(QFont("Microsoft YaHei", 9))
            result["offscreen_font_files_loaded"] = len(loaded_fonts)
        vault = Vault(run_root / "test-data")
        vault.set_setting("retention_days", 0)
        window = ui.MainWindow(vault, listen=False, use_tray=False, browser_tools=False)
        window.resize(1440, 900)
        window.show()
        assert window.tab_scroller is None
        assert not window.collect_text and not vault.setting("collect_text", False)
        result["checks"].append("new text collection remains disabled by default")
        samples = {
            "tech": "def collect_fixture(message):\r\n    return {'saved': message, 'count': 3}  \r\n",
            "read": "阅读笔记：本地收集工具\n文章摘要：保存后可以在本机回看。\n书摘与学习资料。",
            "shopping": "购物订单：SYN-20261018\n商品：纸质笔记本\n物流：等待发货；优惠券已使用。",
            "daily": "日程提醒：周五 15:30 会议\n待办：带上笔记，确认下次预约时间。",
            "other": "清风拂过窗台，午后有一束柔和的光。\n这是单独保存的一段合成文字。",
        }
        url = "https://github.com/example/fixture?tab=readme&token=synthetic%2Fonly#section-2"
        failures = []
        window.capture.failed.connect(failures.append)
        for text in (*samples.values(), url, samples["tech"]):
            assert window.capture.submit("text", text)
        synthetic_image = fixture("Collection OCR fixture", [
            "SQLite API documentation", "Only synthetic image data is used.", "收藏、搜索与分类都在本机完成。",
        ])
        image_path = run_root / "synthetic-input.png"
        synthetic_image.save(image_path)
        synthetic_image.close()
        assert window.capture.submit("image", QImage(str(image_path)))
        started = time.monotonic()

        def select(key):
            window.refresh()
            for index in range(window.gallery.count()):
                row = window.gallery.item(index).data(Qt.ItemDataRole.UserRole)
                if row["id"] == key:
                    window.gallery.setCurrentRow(index)
                    app.processEvents()
                    assert window.active_id == key
                    return
            raise AssertionError("Synthetic record is missing from the gallery")

        def grab(filename):
            app.processEvents()
            assert window.grab().save(str(run_root / filename)), filename

        def inspect():
            try:
                rows = vault.query(limit=30)
                images = [row for row in rows if row["item_type"] == "image"]
                pending = (len(rows) != 7 or len(images) != 1 or images[0]["ocr_status"] == "pending"
                           or any(row["classification_status"] == "pending" for row in rows))
                if pending and not failures and time.monotonic() - started < 90:
                    return
                timer.stop()
                assert not failures, failures
                assert len(rows) == 7, "Text deduplication or capture queue failed"
                assert len(images) == 1 and images[0]["ocr_status"] == "done", images
                assert "API" in images[0]["ocr_text"], "Real isolated image OCR did not recognize the fixture"
                by_content = {row["content"]: row for row in rows if row["item_type"] != "image"}
                assert set(by_content) == set(samples.values()) | {url}
                for category, content in samples.items():
                    assert by_content[content]["category"] == category, (category, by_content[content])
                assert by_content[url]["item_type"] == "link"
                assert by_content[url]["content"] == url
                assert vault.type_counts() == {"image": 1, "text": 5, "link": 1}
                counts = vault.category_counts()
                assert set(counts) == set(CATEGORY_CODES) and all(counts.values()), counts
                assert not vault.pending(include_errors=True)
                assert len(vault.query("纸质笔记本", category="shopping", item_type="text")) == 1
                assert len(vault.query("fixture", category="tech", item_type="link")) == 1
                result["checks"].extend([
                    "real queued text, URL, code and image capture with exact text deduplication",
                    "real image OCR runs in the existing isolated backend",
                    "all five deterministic categories and image/text/link query filters",
                    "URL query parameters and fragment remain exact without fetching the URL",
                    "Chinese full-text search combines with category and type filters",
                ])
                window.refresh()
                grab("mixed-gallery.png")
                window.set_topic("learning")
                assert by_content[samples["read"]]["id"] in {
                    window.gallery.item(index).data(Qt.ItemDataRole.UserRole)["id"]
                    for index in range(window.gallery.count())
                }
                grab("reading-category.png")
                window.set_topic("all")
                window.set_item_type("text")
                assert window.gallery.count() == 5
                select(by_content[samples["tech"]]["id"])
                assert window.ocr_text.toPlainText() == samples["tech"].replace("\r\n", "\n")
                assert all(not control.isEnabled() for control in (
                    window.copy_btn, window.open_btn, window.pin_btn, window.retry_btn))
                window.copy_text()
                assert clipboard.last_text == samples["tech"], "Copy-original rewrote indentation or CRLF"
                grab("code-original.png")
                window.set_item_type("all")
                select(by_content[url]["id"])
                assert window.ocr_text.toPlainText() == url
                assert window.open_link_btn.isVisible()
                window.copy_text()
                assert clipboard.last_text == url
                grab("link-original.png")
                result["checks"].extend([
                    "category and content-type controls filter the native gallery",
                    "non-image details disable image/OCR/pinning actions",
                    "copy-original preserves code indentation, CRLF and full URL using an in-memory clipboard",
                    "mixed gallery, reading category, code and link detail screenshots",
                ])
                if include_themes:
                    for name in ("chevron-light.png", "chevron-dark.png"):
                        assert not QImage(str(asset_path(name))).isNull(), "Theme dropdown resource did not decode"
                    original_rows = vault.query(limit=30)
                    checks = {}
                    for theme_id, colors in THEMES.items():
                        previous_theme = window.theme_id
                        window.show_page("preferences")
                        panel = window.preferences_panel
                        panel.pages.setCurrentIndex(2)
                        panel.select_theme(theme_id)
                        assert window.theme_id == theme_id
                        panel.cancel()
                        assert window.theme_id == previous_theme, "Cancel did not restore the saved theme"
                        window.show_page("preferences")
                        panel = window.preferences_panel
                        panel.pages.setCurrentIndex(2)
                        panel.select_theme(theme_id)
                        grab(f"theme-{theme_id}-appearance.png")
                        assert panel.apply()
                        assert vault.setting("theme") == theme_id and window.theme_id == theme_id
                        select(by_content[samples["tech"]]["id"])
                        grab(f"theme-{theme_id}-gallery.png")
                        assert vault.query(limit=30) == original_rows
                        assert theme()["canvas"] == colors["canvas"]
                        checks[theme_id] = {"preview": True, "cancel": True, "saved": True,
                                            "name": colors["name"], "canvas": colors["canvas"]}
                    result["themes"] = checks
                    result["checks"].append("all configured themes preview, cancel, save and render without changing content")
                result.update(ok=True, total=len(rows), type_counts=vault.type_counts(),
                              category_counts=counts, image_ocr_characters=len(images[0]["ocr_text"]),
                              seconds=round(time.monotonic() - started, 3))
                result["application_material"] = dict(window.glass_state)
                result["application_material"]["root_enabled"] = bool(window.centralWidget().property("nativeGlass"))
                result["application_material"]["window_opacity"] = window.windowOpacity()
            except Exception:
                result["error"] = traceback.format_exc()
            finally:
                if not timer.isActive():
                    persist()
                    window.quit_app()

        timer = QTimer(window)
        timer.timeout.connect(inspect)
        timer.start(100)
        app.exec()
        assert not window.capture.isRunning() and not window.ocr.isRunning()
        result["workers_exited"] = True
        persist()
    except Exception:
        result["ok"] = False
        result["error"] = traceback.format_exc()
        if timer is not None:
            timer.stop()
        if window is not None and not window.quitting:
            window.quit_app()
            deadline = time.monotonic() + 10
            while (window.capture.isRunning() or window.ocr.isRunning()) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
        persist()
    finally:
        ui.QApplication = original_application
        ui.MainWindow.sequence = original_sequence
        ui.should_exclude_clipboard = original_policy
        app.setFont(original_font)
        for font_id in loaded_fonts:
            QFontDatabase.removeApplicationFont(font_id)
    return 0 if result["ok"] else 1
