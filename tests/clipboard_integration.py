"""Native clipboard integration. Run explicitly on Windows, outside pytest."""
from pathlib import Path
import json
import subprocess
import sys
import time
import traceback
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QMimeData, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from snapvault.storage import Vault
from snapvault.ui import MainWindow, STYLE


def main():
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    if len(sys.argv) > 1 and sys.argv[1] == "--writer":
        if sys.argv[2] == "text":
            app.clipboard().setText("Synthetic clipboard test: not an image")
        else:
            image = QImage(190, 110, QImage.Format.Format_ARGB32)
            image.fill(int(sys.argv[2]))
            app.clipboard().setImage(image)
        QTimer.singleShot(600, app.quit)
        return app.exec()
    app.setStyleSheet(STYLE)
    root = Path(__file__).resolve().parents[1] / ".tmp" / ("clipboard-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    vault = Vault(root / "data")
    backup = QMimeData()
    original = app.clipboard().mimeData()
    if original:
        for fmt in original.formats():
            backup.setData(fmt, original.data(fmt))
    window = MainWindow(vault, listen=True, use_tray=True, browser_tools=False)
    window.show()
    state = 0
    process = None
    started = time.monotonic()
    launched = 0
    result = {"ok": False}
    known_sequence = window.sequence()

    def write(value):
        nonlocal process, launched
        process = subprocess.Popen([sys.executable, __file__, "--writer", str(value)],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        launched = time.monotonic()

    def tick():
        nonlocal state, known_sequence
        try:
            assert time.monotonic()-started < 35, "Native clipboard event timeout"
            count = vault.stats()["total"]
            if state == 0:
                write(0xffbbddff)
                state = 1
                return
            if process and (process.poll() is None or time.monotonic()-launched < 1.1):
                return
            known_sequence = window.sequence()
            if state == 1:
                if count != 1:
                    return
                write(0xffbbddff)
                state = 2
            elif state == 2:
                assert count == 1, "Repeated clipboard image was not deduplicated"
                window.toggle_pause()
                write(0xffaabbcc)
                state = 3
            elif state == 3:
                assert count == 1, "Paused listener captured an image"
                window.toggle_pause()
                write(0xffaabbcc)
                state = 4
            elif state == 4:
                if count != 2:
                    return
                write("text")
                state = 5
            elif state == 5:
                assert count == 2, "Text clipboard incorrectly added a screenshot"
                if window.tray:
                    window.close()
                    assert not window.isVisible() and window.listening, "Close-to-tray did not keep listening"
                    window.show_window()
                window.active_id = vault.query()[0]["id"]
                window.copy_image()
                known_sequence = window.sequence()
                result.update(ok=True, total=count, tray_available=bool(window.tray), checks=[
                    "external-process native clipboard image", "deduplication", "pause", "resume",
                    "ignore plain text", "close to tray", "copy original"])
                timer.stop()
        except Exception:
            timer.stop()
            result["error"] = traceback.format_exc()
        finally:
            if not timer.isActive():
                window.listening = False
                window.clip_generation += 1
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
                # Never archive the restored user clipboard into the test vault.
                if window.sequence() == known_sequence:
                    app.clipboard().setMimeData(backup)
                    app.processEvents()
                (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                print(json.dumps(result, indent=2), flush=True)
                window.quit_app()

    timer = QTimer(window)
    timer.timeout.connect(tick)
    timer.start(100)
    app.exec()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
