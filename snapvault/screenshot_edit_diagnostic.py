"""Isolated checks for editable screenshot marks and the matching image editor.

Only locally drawn fixtures are used. Output signals go to fixture files;
there is no clipboard, vault, real screen grab, hotkey or authentication path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import traceback
import uuid

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QPushButton
from shiboken6 import isValid

from .annotation import Annotation, AnnotationDialog
from .annotation_document import annotation_bounds
from .capture_diagnostic import _fixture_image, _pixels, _rectangle
from .screenshot import ScreenshotOverlay
from .theme import apply_theme


def run_screenshot_edit_diagnostic(app, destination, *, preview=False):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    fixture = root / ("screenshot-edit-run-" + token)
    fixture.mkdir()
    report_path = root / "screenshot-edit-result.json"
    report = dict(ok=False, synthetic_only=True, preview=bool(preview),
        system_clipboard_accessed=False, real_screen_captured=False,
        real_vault_accessed=False, global_hotkeys_registered=False,
        system_recent_read=False, authentication_prompted=False,
        windows_closed_cleanly=False, object_movement_observed=False,
        text_edit_observed=False, source_unchanged=False, outputs={}, checks=[])
    old_theme = app.property("snapvaultTheme") or "light"
    old_font = app.font()
    original_picker = QFileDialog.getSaveFileName
    started = time.monotonic()
    fonts, widgets = [], []
    overlay = gallery = timer = timeout = None
    gesture = None

    if app.platformName() == "offscreen":
        font_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("segoeui.ttf", "segoeuib.ttf", "msyh.ttc", "msyhbd.ttc"):
            path = font_root / name
            if path.is_file():
                ident = QFontDatabase.addApplicationFont(str(path))
                if ident >= 0:
                    fonts.append(ident)
        app.setFont(QFont("Microsoft YaHei UI", 9))
    source = _fixture_image()
    source_hash = _pixels(source)
    marks = [Annotation("text", (QPointF(500, 550),), "#e81123", 5, "双击修改这段合成文字"),
             Annotation("arrow", (QPointF(1300, 880), QPointF(1400, 700)), "#e81123", 8)]

    def persist():
        report["source_unchanged"] = _pixels(source) == source_hash
        report["seconds"] = round(time.monotonic() - started, 3)
        if overlay is not None and isValid(overlay):
            report["current"] = dict(selection=_rectangle(overlay.selection),
                marks=len(overlay.annotations), selected_index=overlay.selected_index,
                toolbar=_rectangle(overlay.toolbar.geometry()))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(value, message):
        if not value:
            raise AssertionError(message)
        report["checks"].append(message)
        persist()

    def output(image, action):
        target = fixture / ("output-" + uuid.uuid4().hex[:6] + "-" + action + ".png")
        assert image.save(str(target), "PNG")
        report["outputs"][action] = report["outputs"].get(action, 0) + 1
        report["last_output"] = dict(action=action, width=image.width(), height=image.height(), file=str(target))
        if preview:
            report["ok"] = report["object_movement_observed"] and report["source_unchanged"]
        persist()

    def drag(widget, first, last):
        first, last = QPoint(first), QPoint(last)
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, first)
        QTest.mouseMove(widget, (first + last) / 2)
        QTest.mouseMove(widget, last)
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, last)
        app.processEvents()

    def screen_point(point):
        return QPoint(round(point.x() / overlay.scale_x), round(point.y() / overlay.scale_y))

    def close_preview():
        for widget in widgets:
            if isValid(widget):
                widget.close()
        report["windows_closed_cleanly"] = True
        persist()
        app.quit()

    class ProbeOverlay(ScreenshotOverlay):
        def mousePressEvent(self, event):
            nonlocal gesture
            gesture = self.document.snapshot()
            super().mousePressEvent(event)

        def mouseReleaseEvent(self, event):
            super().mouseReleaseEvent(event)
            if gesture and len(gesture) == len(self.annotations):
                report["object_movement_observed"] |= any(
                    first.points != last.points for first, last in zip(gesture, self.annotations))
            if preview:
                persist()

        def commit_text(self):
            before = tuple(mark.text for mark in self.annotations) if hasattr(self, "document") else ()
            super().commit_text()
            if hasattr(self, "document"):
                report["text_edit_observed"] |= before != tuple(mark.text for mark in self.annotations)

    try:
        # A preview's export action is also restricted to the fixture directory.
        QFileDialog.getSaveFileName = lambda *_args, **_kwargs: (str(fixture / "synthetic-export.png"), "PNG")
        apply_theme(app, "glass_day")
        overlay = ProbeOverlay(QRect(0, 0, 1200, 800), QPixmap.fromImage(source))
        overlay.setWindowTitle("截图编辑验证 · " + token)
        widgets.append(overlay)
        if preview:
            overlay.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
            area = overlay.screen().availableGeometry()
            overlay.move(area.center() - overlay.rect().center())
        else:
            overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        overlay.set_selection(QRect(100, 165, 960, 455))
        for mark in marks:
            overlay.add_annotation(mark)
        overlay.set_tool("select")
        overlay.finished.connect(output)
        overlay.show()

        gallery = AnnotationDialog(source)
        gallery.setWindowTitle("图库编辑验证 · " + token)
        widgets.append(gallery)
        if not preview:
            gallery.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        for mark in marks:
            gallery.canvas.add_annotation(mark)
        gallery.canvas.set_tool("select")
        gallery.copyRequested.connect(lambda image: output(image, "gallery-copy"))
        gallery.saveToLibraryRequested.connect(lambda image: output(image, "gallery-library"))
        app.processEvents()

        if preview:
            open_gallery = QPushButton("打开合成图库编辑器", overlay.toolbar)
            open_gallery.setAutoDefault(False)
            open_gallery.clicked.connect(lambda: (gallery.show(), gallery.raise_(), gallery.activateWindow()))
            overlay.toolbar.layout().addWidget(open_gallery)
            overlay._place_toolbar()
            overlay.cancelled.connect(close_preview)
            timer = QTimer()
            timer.timeout.connect(persist)
            timer.start(200)
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(close_preview)
            timeout.start(180_000)
            persist()
            app.exec()
        else:
            before = overlay.document.snapshot()
            start = screen_point((before[1].points[0] + before[1].points[-1]) / 2)
            drag(overlay, start, start + QPoint(35, 20))
            moved = overlay.document.snapshot()
            check(overlay.selected_index == 1 and moved[1].points != before[1].points,
                  "screenshot selects and drags a single arrow object")
            check(moved[0] == before[0], "moving an arrow leaves other annotations unchanged")
            overlay.undo()
            check(overlay.document.snapshot() == before, "one undo restores the entire object drag")
            overlay.redo()
            check(overlay.document.snapshot() == moved, "redo restores object position")
            midpoint = screen_point((moved[1].points[0] + moved[1].points[-1]) / 2)
            QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, midpoint)
            overlay.color_combo.setCurrentIndex(overlay.color_combo.findData("#0078d4"))
            check(overlay.annotations[1].color == "#0078d4", "selected screenshot object changes color")
            overlay.undo()
            check(overlay.annotations[1].color == moved[1].color, "color edit is independently undoable")
            overlay.redo()
            overlay.select_annotation(1)
            QTest.keyClick(overlay, Qt.Key.Key_Delete)
            check(len(overlay.annotations) == 1, "Delete removes only the selected object")
            overlay.undo()
            check(len(overlay.annotations) == 2, "deleted object is recoverable with undo")
            text_center = screen_point(annotation_bounds(overlay.annotations[0]).center())
            QTest.mouseDClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, text_center)
            check(overlay.text_editor.isVisible(), "screenshot text opens inline for a second edit")
            overlay.text_editor.setText("文字再次修改验证")
            QTest.keyClick(overlay.text_editor, Qt.Key.Key_Return)
            check(overlay.annotations[0].text == "文字再次修改验证" and not report["outputs"],
                  "Enter commits edited text without completing capture")
            edited_hash = _pixels(overlay.crop())
            overlay.undo()
            check(overlay.annotations[0].text == marks[0].text, "text edit can be undone")
            overlay.redo()
            check(_pixels(overlay.crop()) == edited_hash, "text redo restores identical output pixels")
            saved_marks, selection = overlay.document.snapshot(), QRect(overlay.selection)
            QFileDialog.getSaveFileName = lambda *_args, **_kwargs: ("", "")
            overlay.save_as()
            check(overlay.document.snapshot() == saved_marks and overlay.selection == selection
                  and _pixels(overlay.crop()) == edited_hash and not report["outputs"],
                  "cancelled save keeps this screenshot selection and editable marks")
            QFileDialog.getSaveFileName = lambda *_args, **_kwargs: (str(fixture / "synthetic-export.png"), "PNG")

            gallery.show()
            app.processEvents()
            canvas = gallery.canvas
            initial = canvas.document.snapshot()
            center = (initial[1].points[0] + initial[1].points[-1]) / 2
            first = canvas._widget_point(center).toPoint()
            drag(canvas, first, first + QPoint(28, 18))
            check(canvas.selected_index == 1 and canvas.annotations[1].points != initial[1].points,
                  "gallery editor selects and moves the same annotation object")
            canvas.set_color("#0078d4")
            check(canvas.annotations[1].color == "#0078d4", "gallery object color is editable")
            canvas.delete_selected()
            check(len(canvas.annotations) == 1 and canvas.document.undo(), "gallery object deletion is undoable")
            canvas._rendered = canvas.rendered_image() if False else canvas._rendered
            canvas.select_annotation(0)
            canvas.edit_selected_text()
            check(canvas.text_editor.isVisible(), "gallery text editing is inline, matching screenshot field")
            canvas.text_editor.setText("图库文字修改验证")
            QTest.keyClick(canvas.text_editor, Qt.Key.Key_Return)
            check(canvas.annotations[0].text == "图库文字修改验证", "gallery inline text commits the second edit")

            for theme_id, label in (("glass_day", "day"), ("glass_night", "night")):
                apply_theme(app, theme_id)
                overlay.refresh_theme()
                gallery.refresh_theme()
                app.processEvents()
                check(overlay.grab().save(str(root / f"screenshot-edit-{label}.png")), f"screenshot {label} interface renders")
                check(gallery.grab().save(str(root / f"gallery-edit-{label}.png")), f"gallery {label} interface renders")
            check(_pixels(source) == source_hash and _pixels(overlay.source) == source_hash
                  and _pixels(canvas.source) == source_hash, "both editors preserve original source pixels")
            report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        QFileDialog.getSaveFileName = original_picker
        for clock in (timer, timeout):
            if clock is not None:
                clock.stop()
        for widget in widgets:
            if isValid(widget):
                widget.close()
        app.processEvents()
        report["windows_closed_cleanly"] = True
        persist()
        apply_theme(app, old_theme)
        app.setFont(old_font)
        for ident in fonts:
            QFontDatabase.removeApplicationFont(ident)
    return 0 if report["ok"] else 1
