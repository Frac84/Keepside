"""Isolated screenshot-field checks and a synthetic, mouse-testable preview.

The image is drawn here. This module never grabs a screen, reads a clipboard,
registers a hotkey, opens a real vault, or dispatches an output to another app.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import uuid

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPen, QPixmap
from PySide6.QtTest import QTest
from shiboken6 import isValid

from .screenshot import ScreenshotOverlay
from .theme import apply_theme


def _pixels(image):
    return hashlib.sha256(memoryview(image.constBits())[:image.sizeInBytes()]).hexdigest()


def _rectangle(rect):
    return dict(x=rect.x(), y=rect.y(), width=rect.width(), height=rect.height())


def _fixture_image(width=1200, height=800, scale=2):
    """Readable fictional office page, at twice the logical screen resolution."""
    image = QImage(width * scale, height * scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#f3f3f3"))
    painter = QPainter(image)
    painter.scale(scale, scale)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    def text(x, y, value, size=15, color="#242424", bold=False):
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(QPoint(x, y), value)

    painter.fillRect(QRect(0, 0, width, 58), QColor("#ffffff"))
    text(28, 35, "捕风捉影 · 截图操作验证", 20, bold=True)
    text(29, 88, "仅合成画面：框选 → 调整选区 → 标注 → 复制 / 保存 / 贴图 / 识字", 16)
    text(29, 116, "所有输出只保存到独立测试目录，不操作系统剪贴板；Esc 退出，180 秒自动关闭。", 13, "#555555")
    painter.setPen(QPen(QColor("#dddddd"), 1))
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(QRectF(28, 145, width - 56, height - 180), 12, 12)
    text(57, 187, "项目周报 / DEMO ONLY", 24, bold=True)
    text(57, 219, "这是供截图标注的虚构内容。可以圈选重点、加序号、画箭头或遮挡示例字段。", 15, "#555555")
    text(57, 265, "任务", 15, bold=True)
    text(472, 265, "负责人", 15, bold=True)
    text(680, 265, "状态", 15, bold=True)
    for index, (name, person, status) in enumerate((
        ("整理用户反馈", "示例成员 A", "已完成"),
        ("检查截图流程", "示例成员 B", "进行中"),
        ("发布桌面新版本", "示例成员 C", "待验收"),
    )):
        top = 284 + index * 70
        painter.fillRect(QRect(48, top, width - 96, 58), QColor("#f5f7f9" if index % 2 == 0 else "#fafafa"))
        text(65, top + 35, name, 17)
        text(472, top + 35, person, 16)
        text(680, top + 35, status, 16, "#0067c0")
    text(57, 550, "示例编号：DEMO-2026-001", 17)
    text(57, 584, "虚构邮箱：demo@example.invalid", 17)
    text(57, 630, "试试用实色遮挡覆盖上面的示例邮箱，再复制最终图。", 15, "#555555")
    text(57, height - 65, "合成测试页 · 不含真实账号、文件或桌面内容", 13, "#666666")
    painter.end()
    image.setDevicePixelRatio(1.)
    return image


def run_capture_diagnostic(app, destination, *, preview=False):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    fixture = root / ("capture-run-" + token)
    fixture.mkdir()
    report_path = root / "screenshot-result.json"
    report = dict(ok=False, synthetic_only=True, preview=bool(preview),
                  system_input_injected=False, system_clipboard_accessed=False,
                  real_screen_captured=False, global_hotkeys_registered=False,
                  real_vault_accessed=False, authentication_prompted=False,
                  system_recent_read=False, windows_closed_cleanly=False,
                  input_source="external_mouse" if preview else "QTest to owned widget",
                  movement_observed=False, resize_observed=False,
                  annotation_observed=False, source_unchanged=False,
                  output_actions={}, checks=[])
    started = time.monotonic()
    old_theme = app.property("snapvaultTheme") or "light"
    old_font = app.font()
    registered_fonts = []
    if app.platformName() == "offscreen":
        # The Windows offscreen plugin does not enumerate system fonts. Load
        # installed font files explicitly so diagnostic PNGs contain real text.
        font_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("segoeui.ttf", "segoeuib.ttf", "msyh.ttc", "msyhbd.ttc"):
            font_path = font_root / filename
            if font_path.is_file():
                font_id = QFontDatabase.addApplicationFont(str(font_path))
                if font_id >= 0:
                    registered_fonts.append(font_id)
        app.setFont(QFont("Microsoft YaHei UI", 9))
    source = _fixture_image()
    source_hash = _pixels(source)
    report["source_sha256"] = source_hash
    report["source_pixels"] = dict(width=source.width(), height=source.height())
    widgets, outputs = [], []
    live = timer = timeout = None
    gesture_start = None

    def persist():
        report["source_unchanged"] = _pixels(source) == source_hash
        report["seconds"] = round(time.monotonic() - started, 3)
        if live is not None and isValid(live):
            report["current"] = dict(selection=_rectangle(live.selection),
                                     geometry=_rectangle(live.geometry()),
                                     marks=len(getattr(live, "annotations", [])),
                                     toolbar=_rectangle(live.toolbar.geometry()))
        text = json.dumps(report, ensure_ascii=False, indent=2)
        report_path.write_text(text, encoding="utf-8")
        (fixture / "screenshot-result.json").write_text(text, encoding="utf-8")

    def output(image, action):
        assert not image.isNull()
        path = fixture / (f"output-{len(outputs) + 1}-{action}.png")
        assert image.save(str(path), "PNG"), "Synthetic export failed"
        outputs.append((image.copy(), action))
        counts = report["output_actions"]
        counts[action] = counts.get(action, 0) + 1
        report["last_output"] = dict(action=action, file=str(path),
                                      width=image.width(), height=image.height(),
                                      sha256=_pixels(image))
        if preview:
            report["ok"] = (report["movement_observed"] and report["resize_observed"]
                            and report["annotation_observed"] and report["source_unchanged"])
        persist()

    class ProbeOverlay(ScreenshotOverlay):
        def mousePressEvent(self, event):
            nonlocal gesture_start
            gesture_start = (QRect(self.selection), len(getattr(self, "annotations", [])))
            super().mousePressEvent(event)

        def mouseReleaseEvent(self, event):
            super().mouseReleaseEvent(event)
            if gesture_start is not None:
                before, marks = gesture_start
                after = self.selection
                if not before.isEmpty() and not after.isEmpty():
                    report["movement_observed"] |= before.topLeft() != after.topLeft() and before.size() == after.size()
                    report["resize_observed"] |= before.size() != after.size()
                report["annotation_observed"] |= len(getattr(self, "annotations", [])) > marks
            if preview:
                persist()

    def make_overlay(theme_id, *, hidden=True):
        apply_theme(app, theme_id)
        widget = ProbeOverlay(QRect(0, 0, 1200, 800), QPixmap.fromImage(source.copy()))
        widget.setWindowTitle("截图操作验证 · " + token)
        if hidden:
            widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        else:
            # A normal top-level surface makes this synthetic fixture selectable
            # in the desktop automation inventory. Production flags are untouched.
            widget.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
            area = widget.screen().availableGeometry()
            widget.move(area.center() - widget.rect().center())
        widget.finished.connect(output)
        widgets.append(widget)
        widget.show()
        app.processEvents()
        return widget

    def drag(widget, first, last):
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, first)
        QTest.mouseMove(widget, (first + last) / 2)
        QTest.mouseMove(widget, last)
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, last)
        app.processEvents()

    def check(value, description):
        if not value:
            raise AssertionError(description)
        report["checks"].append(description)
        persist()

    def close_preview():
        if live is not None and isValid(live):
            live.close()
        report["windows_closed_cleanly"] = True
        persist()
        app.quit()

    try:
        if preview:
            live = make_overlay("glass_day", hidden=False)
            live.cancelled.connect(close_preview)
            live.destroyed.connect(app.quit)
            timer = QTimer()
            timer.setInterval(200)
            timer.timeout.connect(persist)
            timer.start()
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(close_preview)
            timeout.start(180_000)
            persist()
            app.exec()
        else:
            live = make_overlay("glass_day")
            drag(live, QPoint(150, 175), QPoint(1000, 640))
            check(live.selection.width() >= 849 and live.selection.height() >= 464,
                  "drag creates selection at frozen-image coordinates")
            check(live.rect().contains(live.toolbar.geometry()), "toolbar remains inside screen")
            check(len(live._handles()) == 8 and all(live.rect().contains(point)
                  for point in live._handles().values()), "all eight resize handles remain inside screen")
            selected = QRect(live.selection)
            live.set_tool("select")
            drag(live, QPoint(550, 350), QPoint(580, 370))
            check(live.selection.topLeft() == selected.topLeft() + QPoint(30, 20)
                  and live.selection.size() == selected.size(), "inside drag moves existing selection")
            selected = QRect(live.selection)
            drag(live, selected.bottomRight(), selected.bottomRight() - QPoint(40, 35))
            check(live.selection.width() < selected.width() and live.selection.height() < selected.height(),
                  "corner handle resizes selection")
            live.set_tool("redact")
            live.color = "#202020"
            drag(live, QPoint(250, 300), QPoint(390, 360))
            check(len(live.annotations) == 1, "inline opaque redaction becomes an annotation")
            rendered = live.crop()
            # The selection was moved by (30,20); our mark centre is (320,330).
            x = round((320 - live.selection.x()) * source.width() / live.width())
            y = round((330 - live.selection.y()) * source.height() / live.height())
            check(rendered.pixelColor(x, y) == QColor("#202020"),
                  "export contains flattened opaque redaction")
            marked_hash = _pixels(rendered)
            live.undo()
            check(not live.annotations and _pixels(live.crop()) != marked_hash, "undo removes exported redaction")
            live.redo()
            check(len(live.annotations) == 1 and _pixels(live.crop()) == marked_hash, "redo restores identical final pixels")
            for action in ("copy", "save", "pin", "ocr"):
                before = len(outputs)
                live.finish(action)
                check(len(outputs) == before + 1 and outputs[-1][1] == action
                      and _pixels(outputs[-1][0]) == marked_hash,
                      f"{action} action emits flattened final image only")
            for theme_id, name in (("glass_day", "day"), ("glass_night", "night")):
                apply_theme(app, theme_id)
                if hasattr(live, "refresh_theme"):
                    live.refresh_theme()
                else:
                    from .theme import screenshot_style
                    live.toolbar.setStyleSheet(screenshot_style())
                app.processEvents()
                check(live.grab().save(str(root / f"screenshot-{name}.png")), f"{name} screenshot UI rendered")
            before = len(outputs)
            cancelled = []
            live.cancelled.connect(lambda: cancelled.append(True))
            QTest.keyClick(live, Qt.Key.Key_Escape)
            check(cancelled and len(outputs) == before, "cancel emits no image or output action")
            check(_pixels(source) == source_hash and _pixels(live.pixmap.toImage()) == source_hash,
                  "source image and frozen pixels remain unchanged")
            check(report["movement_observed"] and report["resize_observed"]
                  and report["annotation_observed"], "owned-widget gestures exercised move resize and annotation")
            report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        for clock in (timer, timeout):
            if clock is not None:
                clock.stop()
        for widget in widgets:
            if isValid(widget):
                widget.close()
        app.processEvents()
        report["windows_closed_cleanly"] = True
        apply_theme(app, old_theme)
        persist()
        app.setFont(old_font)
        for font_id in registered_fonts:
            QFontDatabase.removeApplicationFont(font_id)
    return 0 if report["ok"] else 1
