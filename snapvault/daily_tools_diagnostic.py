"""Exercise the visible daily actions using owned synthetic files and clipboard."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import time
import traceback
import uuid

from PIL import Image
from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPixmap
from PySide6.QtWidgets import QFileDialog

from . import hotkey, recent_panel, ui
from .annotation import Annotation, AnnotationDialog
from .basket_diagnostic import _replacements
from .file_transfer import file_mime_data
from .image_compression import CompressionDialog, pil_from_qimage
from .shelf_diagnostic import _Capture, _Clipboard, _Ocr
from .storage import Vault


def run_daily_tools_diagnostic(app, destination, *, preview=False):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fixture = root / ("daily-run-" + uuid.uuid4().hex[:8])
    fixture.mkdir()
    report = dict(ok=False, synthetic_only=True, system_clipboard_accessed=False,
                  real_screen_captured=False, authentication_prompted=False, checks=[])
    clipboard = _Clipboard()

    class Application:
        clipboard = staticmethod(lambda: clipboard)
        instance = staticmethod(lambda: app)
        quit = staticmethod(app.quit)

    class Capture(_Capture):
        def submit(self, kind, value=None):
            if kind == "image" and isinstance(value, QImage):
                with pil_from_qimage(value) as image:
                    key, created = self.vault.add(image)
                self.saved.emit(key, created)
                return True
            return super().submit(kind, value)

    source = fixture / "synthetic-original.png"
    with Image.frombytes("RGB", (960, 640), random.Random(1729).randbytes(960*640*3)) as image:
        image.save(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    replacements = [(ui, "QApplication", Application), (recent_panel, "QApplication", Application),
                    (ui, "CaptureWorker", Capture), (ui, "OcrWorker", _Ocr),
                    (ui.MainWindow, "sequence", staticmethod(lambda: 0)),
                    (hotkey, "foreground_window", lambda: 0),
                    (QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: (str(source), "PNG"))]
    old_font = app.font()
    fonts = []
    window = None
    with _replacements(replacements):
        try:
            if app.platformName() == "offscreen":
                font_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
                for name in ("segoeui.ttf", "segoeuib.ttf", "msyh.ttc", "msyhbd.ttc"):
                    path = font_root / name
                    if path.is_file():
                        font_id = QFontDatabase.addApplicationFont(str(path))
                        if font_id >= 0:
                            fonts.append(font_id)
                app.setFont(QFont("Microsoft YaHei UI", 9))
            vault = Vault(fixture / "synthetic-data")
            for key, value in (("retention_days", 0), ("shelf_ball_enabled", False),
                               ("recent_files_system_enabled", False), ("shelf_keep_open", True),
                               ("theme", "glass_day")):
                vault.set_setting(key, value)
            with Image.open(source) as image:
                original_key, _ = vault.add(image)
            original_path = vault.images / vault.get(original_key)["filename"]
            original_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
            window = ui.MainWindow(vault, listen=False, use_tray=False, browser_tools=False,
                                   classify=False, hotkeys=False)
            window.setWindowTitle("捕风捉影 · 日常操作验收 · " + fixture.name)
            window.grab_screens = lambda: [(QRect(0, 0, 960, 640), QPixmap.fromImage(QImage(str(source))))]
            window.show()

            def wait_for(predicate, timeout=20):
                deadline = time.monotonic() + timeout
                while not predicate():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Synthetic image worker did not finish")
                    app.processEvents()
                    time.sleep(.005)
                app.processEvents()

            def capture(widget, name):
                app.processEvents()
                assert widget.grab().save(str(root / name))

            def exercise():
                try:
                    assert all(button.isVisible() for button in
                               (window.basket_button, window.annotate_capture_button, window.compress_file_button))
                    capture(window, "daily-main-day.png")
                    window.resize(1080, 700)
                    capture(window, "daily-main-compact.png")
                    report["checks"].append("visible daily actions at normal and minimum main-window sizes")
                    files = [source, fixture / "synthetic-note.txt"]
                    files[1].write_text("Synthetic local file reference", encoding="utf-8")
                    clipboard.setMimeData(file_mime_data(files))
                    window.paste_from_window()
                    panel = window.quick_panel
                    assert panel.basket_mode and panel.basket_count == 2
                    assert len(vault.query()) == 1
                    capture(panel, "daily-basket.png")
                    panel.collapse()
                    report["checks"].append("file-manager copy goes directly into basket without a tab choice or library import")

                    window.active_id = original_key
                    window.annotate_image()
                    editor = window.image_tool_dialogs[-1]
                    assert isinstance(editor, AnnotationDialog)
                    original = editor.canvas.source.copy()
                    editor.canvas.add_annotation(Annotation("arrow", (QPointF(80, 80), QPointF(430, 240))))
                    editor.canvas.add_annotation(Annotation("rectangle", (QPointF(70, 70), QPointF(460, 270))))
                    editor.copy_button.click()
                    assert clipboard.last_image == editor.rendered_image() and editor.canvas.source == original
                    editor.library_button.click()
                    assert len(vault.query()) == 2
                    capture(editor, "daily-annotation-day.png")
                    window.set_theme("glass_night")
                    capture(editor, "daily-annotation-night.png")
                    editor.close()
                    report["checks"].append("annotation edits flatten into a copy/new library item while preserving original pixels")

                    window.active_id = original_key
                    window.compress_image()
                    compressor = window.image_tool_dialogs[-1]
                    assert isinstance(compressor, CompressionDialog)
                    compressor.target.setValue(30)
                    wait_for(lambda: compressor.worker is None and compressor.result is not None)
                    result = compressor.result
                    assert len(result.data) <= 30*1024 and not QImage.fromData(result.data).isNull()
                    compressor.copy_button.click()
                    exported = Path(clipboard.last_mime.urls()[0].toLocalFile())
                    assert exported.read_bytes() == result.data
                    report["compressed_bytes"] = len(result.data)
                    report["target_bytes"] = 30*1024
                    capture(compressor, "daily-compression-night.png")
                    window.set_theme("glass_day")
                    capture(compressor, "daily-compression-day.png")
                    compressor.close()
                    assert exported.read_bytes() == result.data
                    report["checks"].append("background compression and clipboard file preserve exact bytes below the requested limit")

                    window.grab_screens = lambda: []  # no screen is read by this test
                    screenshot = QImage(str(source))
                    window._screenshot_taken(screenshot, "annotate")
                    assert isinstance(window.image_tool_dialogs[-1], AnnotationDialog)
                    window.image_tool_dialogs[-1].close()
                    report["checks"].append("screenshot annotation action opens the real editor and keeps capture flow")

                    window.active_id = original_key
                    pin = window.pin_image()
                    pin.set_click_through(True)
                    assert pin._restore_control is not None and pin._restore_control.isVisible()
                    pin._restore_control.restore_button.click()
                    assert not pin.click_through and not pin._restore_control.isVisible()
                    pin.close()
                    report["checks"].append("a visible companion restores click-through directly without the tray")
                    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
                    assert hashlib.sha256(original_path.read_bytes()).hexdigest() == original_hash
                    report.update(ok=True, original_images_unchanged=True, checks_completed=len(report["checks"]))
                except Exception:
                    report["error"] = traceback.format_exc()
                finally:
                    window.quit_app()

            if preview:
                report.update(ok=True, preview=True)
                QTimer.singleShot(180000, window.quit_app)
            else:
                QTimer.singleShot(250, exercise)
            app.exec()
            report["windows_closed_cleanly"] = not window.capture.isRunning() and not window.ocr.isRunning()
        except Exception:
            report["ok"] = False
            report["error"] = traceback.format_exc()
        finally:
            if window is not None:
                for dialog in list(window.image_tool_dialogs):
                    if hasattr(dialog, "shutdown"):
                        dialog.shutdown()
                    dialog.close()
                window.close_all_pins()
                if window.quick_panel is not None:
                    window.quick_panel.shutdown()
                window.recent_files.stop()
                window.hide()
            app.setFont(old_font)
            for font_id in fonts:
                QFontDatabase.removeApplicationFont(font_id)
            (root / "daily-tools-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
