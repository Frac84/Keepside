"""Synthetic, native shelf diagnostic and an optional interactive preview.

No real clipboard, browser engine, global shortcut or original library is used.
The actual MainWindow and ShelfPanel are rendered around a fresh fixture vault.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
import traceback
import uuid
import zipfile

from PIL import Image, ImageDraw
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPixmap

from . import hotkey, recent_panel, ui
from .branding import APP_NAME
from .file_transfer import file_mime_data
from .recent_panel import ShelfPanel
from .storage import Vault


class _Clipboard(QObject):
    dataChanged = Signal()

    def __init__(self):
        super().__init__()
        self.last_mime = None
        self.last_text = ""
        self.last_image = QImage()

    def setMimeData(self, mime):
        self.last_mime = mime
        self.dataChanged.emit()

    def setText(self, text):
        self.last_text = text
        self.dataChanged.emit()

    def setImage(self, image):
        self.last_image = image.copy()
        self.dataChanged.emit()

    def mimeData(self):
        from PySide6.QtCore import QMimeData
        return self.last_mime or QMimeData()

    def text(self):
        return self.last_text

    def image(self):
        return self.last_image.copy()


class _Capture(QObject):
    saved = Signal(str, bool)
    failed = Signal(str)
    cleaned = Signal(int)

    def __init__(self, vault):
        super().__init__()
        self.vault = vault

    def start(self):
        pass

    def isRunning(self):
        return False

    def submit(self, kind, value=None):
        # Preview interactions may only add data to the synthetic fixture vault.
        if kind == "text" and isinstance(value, str) and value.strip():
            key, created = self.vault.add_text(value)
            self.saved.emit(key, created)
        return True


class _Ocr(QObject):
    changed = Signal(str)
    availability = Signal(object, str)

    def __init__(self, _vault):
        super().__init__()
        self.stopping = threading.Event()

    def start(self):
        pass

    def isRunning(self):
        return False

    def enqueue(self, _key):
        pass


def _picture(variant=0):
    image = Image.new("RGB", (900, 560), "#e5eff9" if not variant else "#eff0fe")
    draw = ImageDraw.Draw(image)
    if not variant:
        draw.rectangle((0, 300, 900, 560), fill="#b4cfba")
        draw.ellipse((680, 55, 790, 165), fill="#f1c978")
        draw.polygon([(0, 360), (270, 130), (530, 380)], fill="#91afba")
        draw.polygon([(230, 390), (590, 180), (900, 400)], fill="#6b9697")
        draw.polygon([(0, 470), (300, 350), (650, 560), (0, 560)], fill="#7eac8f")
    else:
        draw.rounded_rectangle((55, 45, 845, 515), radius=20, fill="white")
        draw.rounded_rectangle((90, 80, 405, 120), radius=8, fill="#5b5bd6")
        for index, width in enumerate((620, 540, 650, 580, 420)):
            draw.rounded_rectangle((90, 170 + index * 54, 90 + width, 189 + index * 54),
                                   radius=5, fill="#e1e3ec")
    return image


def _pdf(path):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 22 Tf 50 760 Td (Synthetic shelf preview document) Tj ET"
    objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    output, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(str(index).encode() + b" 0 obj\n" + value + b"\nendobj\n")
    start = len(output)
    output.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n")
    path.write_bytes(output)


def run_shelf_diagnostic(app, destination, preview=False):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_root = root / ("shelf-run-" + uuid.uuid4().hex[:8])
    run_root.mkdir()
    result_path = root / "shelf-result.json"
    result = dict(ok=False, synthetic_only=True, preview=bool(preview),
                  system_clipboard_accessed=False, browser_input_started=False,
                  native_glass_visual_verified=False, screenshots_show_compositor_blur=False,
                  screenshot_material="native request" if preview else "readable fallback rendering",
                  run_directory=str(run_root), checks=[], opened_file_urls=[])
    clipboard = _Clipboard()
    window = panel = timer = None
    loaded_fonts = []
    original_font = app.font()
    originals = {
        "application": ui.QApplication, "capture": ui.CaptureWorker, "ocr": ui.OcrWorker,
        "sequence": ui.MainWindow.__dict__["sequence"],
        "policy": ui.should_exclude_clipboard, "source": ui.clipboard_source_app,
        "panel_application": recent_panel.QApplication,
        "desktop_services": recent_panel.QDesktopServices,
        "foreground": hotkey.foreground_window, "focus": hotkey.focus_window,
        "paste": hotkey.send_paste,
    }

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

    class DiagnosticDesktopServices:
        @staticmethod
        def openUrl(url):
            # Never launch a real viewer from a diagnostic or synthetic preview.
            result["opened_file_urls"].append(url.toString())
            return True

    def persist():
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        result_path.write_text(payload, encoding="utf-8")
        (run_root / "shelf-result.json").write_text(payload, encoding="utf-8")

    def stop():
        if timer:
            timer.stop()
        if panel:
            panel.shutdown()
        if window:
            window.recent_files.stop()

    def capture(name, widget=None):
        app.processEvents()
        target = root / name
        # QWidget.grab renders Qt's surface; compositor blur must be checked
        # separately against the real visible preview through Computer Use.
        assert (widget or panel).grab().save(str(target)), f"Cannot render {name}"
        result.setdefault("screenshots", {})[name] = str(target)

    def rows():
        return [panel.file_results.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(panel.file_results.count())]

    def select(key):
        panel.file_results.clearSelection()
        for index in range(panel.file_results.count()):
            item = panel.file_results.item(index)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole)["key"] == key)

    try:
        ui.QApplication = recent_panel.QApplication = DiagnosticApplication
        recent_panel.QDesktopServices = DiagnosticDesktopServices
        ui.CaptureWorker, ui.OcrWorker = _Capture, _Ocr
        ui.MainWindow.sequence = staticmethod(lambda: 0)
        ui.should_exclude_clipboard = lambda mime=None: False
        ui.clipboard_source_app = lambda **kwargs: ""
        hotkey.foreground_window = lambda: 0
        hotkey.focus_window = lambda _target: False
        hotkey.send_paste = lambda: None
        if app.platformName() == "offscreen":
            font_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for name in ("segoeui.ttf", "segoeuib.ttf", "msyh.ttc", "msyhbd.ttc"):
                path = font_root / name
                if path.is_file():
                    identifier = QFontDatabase.addApplicationFont(str(path))
                    if identifier >= 0:
                        loaded_fonts.append(identifier)
            app.setFont(QFont("Microsoft YaHei", 9))
        downloads = run_root / "Downloads"
        downloads.mkdir()
        photo = downloads / "周末旅行.png"
        image = _picture()
        image.save(photo)
        image.close()
        report = downloads / "课程资料.pdf"
        _pdf(report)
        archive = downloads / "项目素材.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("readme.txt", "Synthetic preview archive, created locally.")
        sentinel = downloads / "移出演示.txt"
        sentinel.write_text("Removing a shelf entry must preserve this file.", encoding="utf-8")
        (downloads / "尚未完成.mp4.crdownload").write_bytes(b"incomplete synthetic download")
        vault = Vault(run_root / "test-data")
        vault.set_setting("retention_days", 0)
        vault.set_setting("quick_paste_autopaste", False)
        now = datetime.now(timezone.utc)
        source = _picture(1)
        screenshot, _ = vault.add(source, now - timedelta(minutes=3))
        source.close()
        vault.finish_ocr(screenshot, "课堂笔记\n本地文件可以随手拖出去，截图也能直接使用。")
        original = vault.images / vault.get(screenshot)["filename"]
        vault.add_text("周末清单\n带相机、充电器和雨伞。", now - timedelta(minutes=8))
        window = ui.MainWindow(vault, listen=False, use_tray=False,
                               browser_tools=False, classify=False, hotkeys=False)
        window.setWindowTitle(f"{APP_NAME} · 浮台预览 · 合成内容")
        window.resize(1240, 800)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, not preview)
        window.show()
        window.cleanup_timer.stop()
        window.recent_files.set_folders([downloads])
        assert not window.tabs_enabled or window.tab_scroller is None
        assert window.tab_scroller is None and not window.listening
        panel = ShelfPanel(window)
        window.quick_panel = panel
        panel.setWindowTitle(f"{APP_NAME} · 浮台预览")
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, not preview)
        panel.keep_open.setChecked(True)
        # Existing merge/copy fixtures intentionally cover every source. The
        # production default is now Windows recent-opened history only.
        panel.source_filter.setCurrentIndex(panel.source_filter.findData("all"))
        panel.open(files=True)
        # Main-window screenshot commands in preview also use a synthetic image.
        window.grab_fullscreen = lambda: QPixmap(str(photo))
        window.grab_screens = lambda: []
        app.aboutToQuit.connect(stop)
        started = time.monotonic()
        timer = QTimer()
        timer.setInterval(80)

        def inspect():
            try:
                if len(window.recent_files.entries()) != 4:
                    assert time.monotonic() - started < 12, "Synthetic Downloads discovery timed out"
                    return
                assert {row["name"] for row in window.recent_files.entries()} == {
                    photo.name, report.name, archive.name, sentinel.name}
                panel.refresh()
                complete = panel.files_mode and len(rows()) == 5 and any(
                    row.get("vault_id") == screenshot for row in rows())
                if not complete:
                    assert time.monotonic() - started < 12, (
                        "Synthetic shelf merge timed out", len(rows()), panel.files_mode,
                        panel.search.text(), vault.type_counts())
                    return
                timer.stop()
                result["checks"].append("asynchronous selected-folder discovery excludes incomplete downloads")
                result["checks"].append("actual MainWindow and ShelfPanel merge downloaded files and library screenshots")
                select("vault:" + screenshot)
                panel.toggle_pin()
                assert rows()[0]["key"] == "vault:" + screenshot and rows()[0]["pinned"]
                panel.remove_entries()
                assert original.is_file() and vault.get(screenshot) is not None
                assert all(row.get("vault_id") != screenshot for row in rows())
                # Restore the synthetic screenshot so the preview remains useful.
                panel._hidden_shots.discard(screenshot)
                panel._pinned_shots.add(screenshot)
                vault.set_setting("recent_hidden_screenshots", [])
                vault.set_setting("recent_pinned_screenshots", [screenshot])
                sentinel_key = next(row["key"] for row in rows() if row["name"] == sentinel.name)
                select(sentinel_key)
                panel.remove_entries()
                assert sentinel.is_file()
                result["checks"].append("pin priority and shelf removal preserve original screenshot and external file")
                payload = file_mime_data([photo, report])
                assert [Path(url.toLocalFile()) for url in payload.urls()] == [photo, report]
                assert all(url.isLocalFile() for url in payload.urls())
                result["checks"].append("multi-file native MIME payload contains local file URLs")
                select(next(row["key"] for row in rows() if row["name"] == report.name))
                panel.open_current()
                assert result["opened_file_urls"][-1] == recent_panel.QUrl.fromLocalFile(str(report)).toString()
                result["checks"].append("open-file action dispatches a local URL to an isolated default-app recorder")
                panel.refresh()
                select("vault:" + screenshot)
                panel.accept_current(paste=False)
                exported = Path(clipboard.last_mime.urls()[0].toLocalFile())
                assert exported.read_bytes() == original.read_bytes()
                assert exported.parent.parent == vault.root / "drag-cache"
                result["checks"].append("screenshot file copy creates a stable export through an in-memory clipboard")
                panel.keep_open.setChecked(False)
                panel._dragging = True
                panel.show()
                panel._hide_if_inactive()
                assert panel.isVisible()
                panel._dragging = False
                panel.keep_open.setChecked(True)
                result["checks"].append("drag-in-progress prevents focus-loss dismissal")
                panel.begin_window_drag(panel.pos() + QPoint(20, 20))
                panel.move_window_drag(panel.pos() + QPoint(60, 50))
                panel.end_window_drag()
                assert vault.setting("shelf_panel_position") == [panel.x(), panel.y()]
                state = panel.suspend_for_capture()
                assert not panel.isVisible() and (panel._orb is None or not panel._orb.isVisible())
                panel.resume_after_capture(state)
                assert panel.isVisible()
                result["checks"].append("title drag persists position and screenshot capture hides shelf surfaces")
                # A compositor preview may coexist with the real application.
                # Test the ball offscreen, never add another desktop launcher.
                visible_preview = not panel.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                if visible_preview:
                    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                panel.collapse()
                ball = panel._orb
                assert ball is not None and ball.isVisible() and not panel.isVisible()
                capture("shelf-ball-preview.png", ball)
                state = panel.suspend_for_capture()
                assert not panel.isVisible() and not ball.isVisible()
                panel.resume_after_capture(state)
                assert ball.isVisible() and not panel.isVisible()
                panel.open_from_ball()
                assert panel.isVisible() and panel.files_mode and not ball.isVisible()
                result["checks"].append("collapse-to-ball, ball capture suspension and reopening preserve shelf access")
                if visible_preview:
                    vault.set_setting("shelf_ball_enabled", False)
                    panel.hide_ball()
                    panel.hide()
                    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
                    panel.show()
                library_theme, original_dark = window.theme_id, panel.shelf_dark
                for dark, name in ((False, "shelf-preview-light.png"),
                                   (True, "shelf-preview-dark.png")):
                    panel.set_appearance(dark)
                    panel.refresh()
                    panel.status.setText("合成预览：可搜索、固定、拖动文件，原图库未参与。")
                    capture(name)
                    result.setdefault("glass_requests", {})["dark" if dark else "light"] = dict(panel.glass_state)
                assert window.theme_id == library_theme
                panel.set_appearance(original_dark)
                panel.refresh()
                panel.status.setText("合成预览：可搜索、固定、拖动文件，原图库未参与。")
                capture("shelf-preview.png")
                result["glass_state"] = dict(panel.glass_state)
                result["native_glass_api_confirmed"] = bool(panel.glass_state.get("enabled"))
                result["checks"].append("independent light and dark shelf appearances render without changing the library theme")
                result["visible_entries"] = [row["name"] for row in rows()]
                if not preview:
                    available = recent_panel.windows_hello.availability()
                    result["windows_hello"] = dict(usable=available.usable, status=available.status,
                                                   authentication_prompted=False)
                    if available.usable:
                        assert panel.lock_files()
                        assert vault.setting("recent_files_locked_v1") is True
                        assert panel.file_results.count() == 0 and panel.file_entries() == []
                        assert panel.lock_card.isVisible() and not panel.search.isVisible()
                        for dark, name in ((False, "shelf-locked-preview-light.png"),
                                           (True, "shelf-locked-preview-dark.png")):
                            panel.set_appearance(dark)
                            panel.refresh()
                            capture(name)
                        result["checks"].append("manual lock persists and hides recent file metadata in both themes without authentication")
                result["ok"] = True
                persist()
                if preview:
                    window.statusBar().showMessage("浮台预览：仅使用合成文件，剪贴板操作保存在预览内存中。")
                    panel.show()
                    panel.raise_()
                else:
                    window.quit_app()
            except Exception as exc:
                timer.stop()
                result["error"] = str(exc)
                result["traceback"] = traceback.format_exc()
                persist()
                if window:
                    window.quit_app()
                else:
                    app.quit()

        timer.timeout.connect(inspect)
        timer.start()
        app.exec()
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
        persist()
    finally:
        stop()
        if panel:
            panel.shutdown()
        if window:
            window.hide()
        ui.QApplication = originals["application"]
        ui.CaptureWorker, ui.OcrWorker = originals["capture"], originals["ocr"]
        ui.MainWindow.sequence = originals["sequence"]
        ui.should_exclude_clipboard, ui.clipboard_source_app = originals["policy"], originals["source"]
        recent_panel.QApplication = originals["panel_application"]
        recent_panel.QDesktopServices = originals["desktop_services"]
        hotkey.foreground_window, hotkey.focus_window = originals["foreground"], originals["focus"]
        hotkey.send_paste = originals["paste"]
        app.setFont(original_font)
        for identifier in loaded_fonts:
            QFontDatabase.removeApplicationFont(identifier)
    return 0 if result["ok"] else 1
