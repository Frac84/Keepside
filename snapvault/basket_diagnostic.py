"""Synthetic end-to-end file-basket checks for source and frozen releases.

All files belong to a fresh fixture directory. Clipboard, external viewers,
Explorer, mouse input, browser tools and Hello verification are isolated.
The optional Qt renders contain only synthetic content, never desktop pixels.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
from types import SimpleNamespace
import uuid

from PIL import Image
from PySide6.QtCore import QMimeData, Qt, QTimer, QUrl
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QWidget

from . import file_basket, hotkey, recent_panel, ui
from .file_basket import FileBasket
from .recent_panel import ShelfPanel
from .shelf_diagnostic import _Capture, _Clipboard, _Ocr
from .storage import Vault


def run_basket_preview(app, destination):
    """Owned native drag surface for manual/tool QA of the frozen executable."""
    from PySide6.QtCore import QObject, QEvent, QPoint
    from PySide6.QtGui import QCursor, QKeySequence, QShortcut
    from PySide6.QtWidgets import QLabel

    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fixture = root / ("native-run-" + uuid.uuid4().hex[:8])
    fixture.mkdir()
    report_path = root / "native-basket-result.json"
    report = dict(synthetic_only=True, system_clipboard_accessed=False,
                  closed_cleanly=False, drops=[], drag_invocations=0, drag_completed=0)
    clipboard = _Clipboard()

    class Application:
        clipboard = staticmethod(lambda: clipboard)
        instance = staticmethod(lambda: app)
        quit = staticmethod(app.quit)

    class DesktopRecorder:
        openUrl = staticmethod(lambda _url: True)

    replacements = [(ui, "QApplication", Application), (recent_panel, "QApplication", Application),
                    (ui, "CaptureWorker", _Capture), (ui, "OcrWorker", _Ocr),
                    (ui.MainWindow, "sequence", staticmethod(lambda: 0)),
                    (hotkey, "foreground_window", lambda: 0),
                    (recent_panel, "QDesktopServices", DesktopRecorder),
                    (recent_panel, "reveal_file", lambda _path: True)]
    with _replacements(replacements):
        vault = Vault(fixture / "synthetic-data")
        for key, value in (("retention_days", 0), ("shelf_ball_enabled", False),
                           ("shelf_keep_open", True), ("shelf_appearance", "light"),
                           ("recent_files_system_enabled", False)):
            vault.set_setting(key, value)
        window = ui.MainWindow(vault, listen=False, use_tray=False, browser_tools=False,
                               classify=False, hotkeys=False)
        panel = ShelfPanel(window)
        window.quick_panel = panel
        panel.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.CustomizeWindowHint |
                             Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        originals = []
        for folder, name in (("source-a", "合成笔记.txt"), ("source-b", "synthetic image.png"),
                             ("source-c", "synthetic video.mp4")):
            source = fixture / folder
            source.mkdir()
            path = source / name
            path.write_bytes(b"Synthetic native drag acceptance file.")
            originals.append(path)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in originals}
        window.file_basket.add_paths(originals)
        receiver = None

        def persist():
            report["original_files_unchanged"] = all(path.is_file() and
                hashlib.sha256(path.read_bytes()).hexdigest() == before[path] for path in originals)
            report["basket_count"] = window.file_basket.count
            report["dragging"] = panel._dragging
            if receiver is not None:
                report["cursor_in_receiver"] = receiver.rect().contains(receiver.mapFromGlobal(QCursor.pos()))
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        class Observer(QObject):
            def eventFilter(self, watched, event):
                if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                                    QEvent.Type.MouseButtonRelease):
                    report.setdefault("button_events", []).append(event.type().name)
                    persist()
                return False

        class Receiver(QLabel):
            def __init__(self):
                super().__init__("拖到这里验证文件接收\n仅合成数据，不复制文件")
                self.setAcceptDrops(True)
                self.setMinimumHeight(96)
                self.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.setStyleSheet("background:#E5EFF8;color:#1B1B1B;border:2px dashed #0067C0;border-radius:8px")

            def dragEnterEvent(self, event):
                report["receiver_enter_count"] = report.get("receiver_enter_count", 0) + 1
                if event.mimeData().hasUrls() and event.possibleActions() & Qt.DropAction.CopyAction:
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.accept()
                else:
                    event.ignore()
                persist()

            dragMoveEvent = dragEnterEvent

            def dropEvent(self, event):
                urls = event.mimeData().urls()
                paths = [Path(url.toLocalFile()) for url in urls if url.isLocalFile()]
                if len(paths) != len(urls) or not paths or any(path not in originals for path in paths):
                    event.ignore()
                    return
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                report["drops"].append(dict(count=len(paths), copy_only=True,
                    all_expected=True, all_existing=all(path.is_file() for path in paths)))
                self.setText(f"已接收 {len(paths)} 个合成文件入口\nCopyAction · 原文件保持原样")
                persist()

        receiver = Receiver()
        panel.findChild(QWidget, "quickFrame").layout().addWidget(receiver)
        observer = Observer(panel)
        panel.basket_drag_all_button.installEventFilter(observer)
        original_drag = panel.start_basket_drag

        def observed_drag(*args, **kwargs):
            report["drag_invocations"] += 1
            persist()
            value = original_drag(*args, **kwargs)
            report["drag_completed"] += 1
            persist()
            return value

        panel.start_basket_drag = observed_drag
        panel.setWindowTitle("中转篮原生拖放验证 · " + fixture.name)
        panel.resize(540, 730)
        panel.open_basket()
        panel.move(120, 80)
        shortcuts = [QShortcut(QKeySequence("Ctrl+Q"), panel, activated=window.quit_app)]
        heartbeat = QTimer(panel)
        heartbeat.setInterval(500)
        heartbeat.timeout.connect(persist)
        heartbeat.start()

        def stop():
            heartbeat.stop()
            report["closed_cleanly"] = True
            persist()
            panel.shutdown()
            window.recent_files.stop()

        app.aboutToQuit.connect(stop)
        QTimer.singleShot(180000, window.quit_app)
        persist()
        try:
            return app.exec()
        finally:
            app.aboutToQuit.disconnect(stop)
            stop()


@contextmanager
def _replacements(values):
    originals = [(owner, name, owner.__dict__[name] if isinstance(owner, type)
                  and name in owner.__dict__ else getattr(owner, name)) for owner, name, _ in values]
    try:
        for owner, name, value in values:
            setattr(owner, name, value)
        yield
    finally:
        for owner, name, value in reversed(originals):
            setattr(owner, name, value)


def run_basket_diagnostic(app, destination):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fixture = root / ("basket-run-" + uuid.uuid4().hex[:8])
    fixture.mkdir()
    report_path = root / "basket-result.json"
    result = dict(ok=False, synthetic_only=True, system_clipboard_accessed=False,
                  external_file_viewer_started=False, browser_input_started=False,
                  authentication_prompted=False, checks=[])
    clipboard = _Clipboard()
    opened, revealed, dragged = [], [], []
    window = panel = restarted = timer = video = None
    connected = False
    original_font, loaded_fonts = app.font(), []
    started = time.monotonic()
    original_metadata = file_basket._metadata

    def metadata(path):
        info = original_metadata(path)
        if info is not None and video is not None and Path(path) == video:
            # Exercise a multi-gigabyte stat result with a tiny real file. The
            # basket still uses its real path validation and standard MIME URLs.
            return SimpleNamespace(st_size=8 * 1024 ** 3, st_mtime_ns=info.st_mtime_ns)
        return info

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

    class DesktopRecorder:
        @staticmethod
        def openUrl(url):
            opened.append(Path(url.toLocalFile()))
            return True

    class DragRecorder:
        def __init__(self, _source):
            self.mime = None

        def setMimeData(self, mime):
            self.mime = mime

        def setPixmap(self, _pixmap):
            pass

        def exec(self, actions, default_action):
            assert actions == default_action == Qt.DropAction.CopyAction
            assert self.mime and all(url.isLocalFile() for url in self.mime.urls())
            dragged.append([Path(url.toLocalFile()) for url in self.mime.urls()])
            return Qt.DropAction.CopyAction

        def deleteLater(self):
            pass

        @staticmethod
        def cancel():
            pass

    class DropEvent:
        def __init__(self, paths):
            self.mime = QMimeData()
            self.mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
            self.accepted = self.ignored = False
            self.action = None

        def mimeData(self):
            return self.mime

        def source(self):
            return None

        def possibleActions(self):
            return Qt.DropAction.CopyAction

        def acceptProposedAction(self):
            self.accepted = True

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

        def setDropAction(self, action):
            self.action = action

    def forbid_authentication(*_args, **_kwargs):
        raise AssertionError("Basket use and lock must not start authentication")

    def persist():
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def stop():
        if timer is not None:
            timer.stop()
        if restarted is not None:
            restarted.shutdown()
        if panel is not None:
            panel.shutdown()
        if window is not None:
            window.recent_files.stop()
            window.hide()

    def selected(keys):
        panel.basket_results.clearSelection()
        for index in range(panel.basket_results.count()):
            item = panel.basket_results.item(index)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole)["key"] in keys)

    replacements = [
        (ui, "QApplication", DiagnosticApplication),
        (recent_panel, "QApplication", DiagnosticApplication),
        (ui, "CaptureWorker", _Capture), (ui, "OcrWorker", _Ocr),
        (ui.MainWindow, "sequence", staticmethod(lambda: 0)),
        (ui, "should_exclude_clipboard", lambda _mime=None: False),
        (ui, "clipboard_source_app", lambda **_kwargs: ""),
        (hotkey, "foreground_window", lambda: 0),
        (hotkey, "focus_window", lambda _target: False),
        (hotkey, "send_paste", lambda: None),
        (recent_panel, "QDesktopServices", DesktopRecorder),
        (recent_panel, "reveal_file", lambda path: revealed.append(Path(path)) or True),
        (recent_panel, "QDrag", DragRecorder),
        (recent_panel.windows_hello, "availability", lambda: recent_panel.windows_hello.Availability(True, "available", "Synthetic availability")),
        (recent_panel.windows_hello, "verify", forbid_authentication),
        (file_basket, "_metadata", metadata),
    ]
    try:
        with _replacements(replacements):
            if app.platformName() == "offscreen":
                font_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
                for name in ("segoeui.ttf", "segoeuib.ttf", "msyh.ttc", "msyhbd.ttc"):
                    path = font_root / name
                    if path.is_file():
                        identifier = QFontDatabase.addApplicationFont(str(path))
                        if identifier >= 0:
                            loaded_fonts.append(identifier)
                app.setFont(QFont("Microsoft YaHei", 9))
            source_a, source_b = fixture / "source-a", fixture / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            note = source_a / "课程笔记.txt"
            note.write_text("Synthetic basket note.\n", encoding="utf-8")
            missing = source_b / "待处理清单.txt"
            missing.write_text("Synthetic basket item to become missing.", encoding="utf-8")
            extra = source_b / "补充资料.txt"
            extra.write_text("Synthetic additional drop.", encoding="utf-8")
            picture = source_b / "合成图片.png"
            image = Image.new("RGB", (96, 64), "#a3b7c5")
            image.save(picture)
            image.close()
            video = source_a / "合成大视频.mp4"
            video.write_bytes(b"Synthetic small video fixture; only metadata is simulated.")
            logical = 8 * 1024 ** 3
            result.update(large_file_logical_bytes=logical, large_file_metadata_simulated=True,
                          large_file_physical_bytes=video.stat().st_size)
            originals = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (note, picture, extra, video)}
            vault = Vault(fixture / "test-data")
            for key, value in (("retention_days", 0), ("quick_paste_autopaste", False),
                               ("shelf_ball_enabled", False), ("recent_files_system_enabled", False)):
                vault.set_setting(key, value)
            image = Image.new("RGB", (64, 48), "#eeeeee")
            key, _ = vault.add(image)
            image.close()
            vault.finish_ocr(key, "Synthetic retained image")
            vault.add_text("Synthetic retained text")
            before_rows = vault.query(limit=30)
            before_images = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in vault.images.iterdir()}
            window = ui.MainWindow(vault, listen=False, use_tray=False, browser_tools=False,
                                   classify=False, hotkeys=False)
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            window.show()
            window.cleanup_timer.stop()
            panel = ShelfPanel(window)
            window.quick_panel = panel
            panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            panel.keep_open.setChecked(True)
            panel.open_basket()
            assert window.tab_scroller is None and not window.listening

            def inspect():
                nonlocal restarted
                timer.stop()
                try:
                    model = window.file_basket
                    panel.add_basket_paths([note, picture, missing, video])
                    panel.refresh()
                    assert model.count == 4 and len(panel.basket_entries()) == 4
                    assert any(row["size"] == logical for row in model.entries())
                    assert vault.query(limit=30) == before_rows
                    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in vault.images.iterdir()} == before_images
                    result["checks"].append("multi-folder files and simulated 8 GiB metadata store references without copying into the image library")
                    panel.add_basket_paths([note, video])
                    assert model.count == 4
                    reloaded = FileBasket(vault)
                    assert [row["path"] for row in reloaded.entries()] == [row["path"] for row in model.entries()]
                    reloaded.deleteLater()
                    result["checks"].append("deduplication and restart persistence preserve basket order")
                    entries = panel.basket_entries()
                    keys = {entries[0]["key"], entries[1]["key"]}
                    selected(keys)
                    expected = {Path(row["path"]) for row in entries if row["key"] in keys}
                    panel.copy_basket()
                    assert {Path(url.toLocalFile()) for url in clipboard.last_mime.urls()} == expected
                    panel.start_basket_drag()
                    assert set(dragged[-1]) == expected
                    panel.search.setText("课程")
                    panel.refresh()
                    panel.start_basket_drag(all_items=True)
                    assert set(dragged[-1]) == {note, picture, missing, video}
                    panel.copy_basket(all_items=True)
                    assert {Path(url.toLocalFile()) for url in clipboard.last_mime.urls()} == {note, picture, missing, video}
                    result["checks"].append("selected and all-file export use exact local URL payloads and CopyAction only, independent of search filtering")
                    panel.search.clear()
                    missing.unlink()
                    panel.refresh()
                    assert model.count == 4 and any(not row["exists"] for row in model.entries())
                    panel.start_basket_drag(all_items=True)
                    assert set(dragged[-1]) == {note, picture, video}
                    assert model.count == 4
                    result["checks"].append("missing originals are marked and skipped during export without deleting saved references")
                    entries = panel.basket_entries()
                    selected({next(row["key"] for row in entries if Path(row["path"]) == note)})
                    panel.open_current()
                    panel.reveal_current()
                    assert opened[-1] == note and revealed[-1] == note
                    result["checks"].append("open and reveal actions dispatch through isolated local recorders")
                    panel.remove_basket_entries()
                    assert model.count == 3 and note.is_file()
                    panel.clear_basket()
                    assert model.count == 0 and note.is_file() and picture.is_file() and video.is_file()
                    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in originals.items())
                    result["checks"].append("remove and clear modify only basket references and leave original bytes intact")
                    panel.add_basket_paths([note, picture, video])
                    ball = panel.ensure_ball()
                    ball.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                    enter = DropEvent([extra])
                    ball.dragEnterEvent(enter)
                    assert enter.accepted
                    drop = DropEvent([extra])
                    ball.dropEvent(drop)
                    panel.refresh()
                    assert drop.accepted and drop.action == Qt.DropAction.CopyAction
                    assert model.count == 4 and panel.tabs.currentIndex() == 2 and panel.basket_count == 4
                    result["checks"].append("floating-ball file drops route to the basket and its privacy-aware count")
                    for dark, name in ((False, "basket-preview-light.png"), (True, "basket-preview-dark.png")):
                        panel.set_appearance(dark)
                        panel.refresh()
                        app.processEvents()
                        assert panel.grab().save(str(root / name))
                    result["screenshots_show_compositor_blur"] = False
                    result["screenshot_material"] = "Qt synthetic rendering; compositor checked separately"
                    before_count, before_drag, before_clipboard = model.count, len(dragged), clipboard.last_mime
                    before_open, before_reveal = len(opened), len(revealed)
                    assert panel.lock_files()
                    panel.refresh()
                    assert panel.basket_count == 0 and panel.basket_results.count() == 0
                    for widget in [panel, *panel.findChildren(QWidget)]:
                        assert all(path.name not in widget.toolTip() for path in (note, picture, video, extra))
                    panel.add_basket_paths([missing])
                    panel.copy_basket(all_items=True)
                    panel.start_basket_drag(all_items=True)
                    panel.open_current()
                    panel.reveal_current()
                    panel.remove_basket_entries()
                    panel.clear_basket()
                    rejected = DropEvent([note])
                    ball.dropEvent(rejected)
                    assert rejected.ignored and not rejected.accepted
                    assert model.count == before_count and len(dragged) == before_drag
                    assert clipboard.last_mime is before_clipboard
                    assert len(opened) == before_open and len(revealed) == before_reveal
                    restarted = ShelfPanel(window)
                    restarted.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                    assert restarted.files_locked and restarted.basket_count == 0
                    result["checks"].append("manual lock hides basket metadata and blocks all input/export actions, including after restart, without authentication")
                    assert vault.query(limit=30) == before_rows
                    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in vault.images.iterdir()} == before_images
                    result.update(ok=True, checks_completed=len(result["checks"]),
                                  retained_library_items=len(before_rows), basket_references=model.count,
                                  clipboard_export_count=len(clipboard.last_mime.urls()),
                                  native_drag_count=len(dragged))
                except Exception:
                    result["error"] = traceback.format_exc()
                finally:
                    result["seconds"] = round(time.monotonic() - started, 3)
                    persist()
                    window.quit_app()

            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(inspect)
            timer.start(50)
            app.aboutToQuit.connect(stop)
            connected = True
            app.exec()
            stop()
            assert panel._orb is None or not panel._orb.isVisible()
            result["windows_closed_cleanly"] = True
            persist()
    except Exception:
        result["error"] = traceback.format_exc()
        result["ok"] = False
        persist()
    finally:
        stop()
        if connected:
            try:
                app.aboutToQuit.disconnect(stop)
            except (RuntimeError, TypeError):
                pass
        if video is not None:
            video.unlink(missing_ok=True)
        app.setFont(original_font)
        for identifier in loaded_fonts:
            QFontDatabase.removeApplicationFont(identifier)
    return 0 if result["ok"] else 1
