"""Real local OCR on a synthetic temporary image, with no vault or clipboard."""
import hashlib
import json
import os
from pathlib import Path
import traceback

from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage

from . import workers


def run_screenshot_privacy_diagnostic(app, destination):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    report = dict(ok=False, synthetic_only=True, system_clipboard_accessed=False,
                  real_vault_accessed=False, real_screen_captured=False,
                  temporary_files_cleaned=False, checks=[])
    worker, timer = None, None
    observed = []
    original_session = workers.ocr.OcrSession
    try:
        source = Image.new("RGB", (540, 100), "white")
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
        font = ImageFont.truetype(str(font_path), 36)
        ImageDraw.Draw(source).text((18, 25), "LOCAL OCR 1140", fill="black", font=font)
        rgba = source.convert("RGBA")
        image = QImage(rgba.tobytes(), rgba.width, rgba.height,
                       QImage.Format.Format_RGBA8888).copy()
        before = hashlib.sha256(bytes(image.constBits())).hexdigest()

        class NoVault:
            def __getattr__(self, name):
                report["real_vault_accessed"] = True
                raise AssertionError("Transient OCR requested vault attribute: " + name)

        class Session:
            def __init__(self):
                self.inner = original_session()
            def recognize(self, path, **kwargs):
                observed.append(Path(path))
                return self.inner.recognize(path, **kwargs)
            def close(self):
                self.inner.close()

        workers.ocr.OcrSession = Session
        worker = workers.OcrWorker(NoVault())

        def completed(tag, result, error):
            text = result.get("text", "")
            report["characters"] = len(text)
            if error:
                report["error"] = error
            else:
                report["recognized_fixture"] = "LOCAL" in text.upper() and "1140" in text
            worker.stopping.set()
            app.quit()

        worker.transient.connect(completed)
        if not worker.enqueue_image(image, "synthetic-only"):
            raise AssertionError("Synthetic image was not queued")
        worker.start()
        timer = QTimer()
        timer.setSingleShot(True)

        def timed_out():
            report["error"] = "Synthetic local OCR timed out"
            worker.stopping.set()
            app.quit()

        timer.timeout.connect(timed_out)
        timer.start(45_000)
        app.exec()
        worker.stopping.set()
        if not worker.wait(15_000):
            raise AssertionError("Local OCR worker did not exit")
        report["temporary_files_cleaned"] = bool(observed) and all(
            not path.exists() and not path.parent.exists() for path in observed)
        report["source_unchanged"] = hashlib.sha256(bytes(image.constBits())).hexdigest() == before
        report["workers_exited"] = True
        report["ok"] = (report.get("recognized_fixture", False) and
            report["temporary_files_cleaned"] and report["source_unchanged"] and
            not report["real_vault_accessed"] and not report.get("error"))
        if report["ok"]:
            report["checks"] = ["real local OCR recognizes synthetic text",
                "transient job never accesses a vault", "final image pixels remain unchanged",
                "temporary image and directory removed", "worker and OCR process exit cleanly"]
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if timer:
            timer.stop()
        if worker:
            worker.stopping.set()
            worker.wait(15_000)
        workers.ocr.OcrSession = original_session
        (root / "screenshot-privacy-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
