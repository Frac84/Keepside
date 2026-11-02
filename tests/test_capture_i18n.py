"""Capture/editor translations leave image pixels and user text untouched."""
import ast
import json
from pathlib import Path
import re

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from snapvault import i18n
from snapvault.annotation import AnnotationDialog
from snapvault.image_compression import CompressionDialog
from snapvault.pinboard import PinnedImageWindow
from snapvault.screenshot import ScreenshotOverlay
from snapvault.screenshot_output import ScreenshotTextDialog


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def english(app):
    previous = i18n.language()
    i18n.set_language("en_US")
    yield
    i18n.set_language(previous)


def source():
    image = QImage(600, 400, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    return image


def interface_texts(widget):
    values = []
    for child in (widget, *widget.findChildren(QWidget)):
        for attribute in ("text", "windowTitle", "toolTip", "placeholderText", "accessibleName", "accessibleDescription"):
            getter = getattr(child, attribute, None)
            if callable(getter):
                value = getter()
                if isinstance(value, str):
                    values.append(value)
    return values


def assert_english(widget):
    chinese = [value for value in interface_texts(widget) if re.search("[\u3400-\u9fff]", value)]
    assert chinese == []


def test_capture_and_annotation_tools_are_english(english):
    image = source()
    overlay = ScreenshotOverlay(QRect(0, 0, 1000, 800), QPixmap.fromImage(image))
    dialog = AnnotationDialog(image)
    try:
        overlay.set_selection(QRect(50, 50, 800, 500))
        assert overlay.tool_buttons["rectangle"].text() == "Rectangle"
        assert overlay.action_buttons["copy_only"].text() == "Copy only"
        assert overlay.action_buttons["copy"].text() == "Copy and save"
        assert "Keepside" in dialog.windowTitle()
        assert_english(overlay)
        assert_english(dialog)
        assert dialog.canvas.source == image
    finally:
        overlay.close()
        dialog.close()


def test_ocr_chrome_translates_but_user_text_does_not(english):
    dialog = ScreenshotTextDialog(source())
    try:
        dialog.set_item("synthetic")
        dialog.update_result(dict(ocr_text="用户自己的中文内容", ocr_status="done"))
        assert dialog.text.toPlainText() == "用户自己的中文内容"
        assert "characters" in dialog.status.text()
        assert dialog.copy_button.text() == "Copy all text"
        assert "Keepside" in dialog.windowTitle()
        assert_english(dialog)
    finally:
        dialog.close()


def test_compression_and_pin_controls_are_english(english, monkeypatch, app):
    monkeypatch.setattr(CompressionDialog, "start_compression", lambda *_: None)
    dialog = CompressionDialog(source(), source_name="用户自己的文件名.png")
    pin = PinnedImageWindow(pixmap=source())
    try:
        assert dialog.source_name == "用户自己的文件名"
        assert dialog.compress_button.text() == "Compress"
        dialog._invalidate()
        assert_english(dialog)
        assert_english(pin)
        assert "Keepside" in pin.windowTitle()
        pin.set_opacity(.5)
        assert pin.opacity_slider.toolTip() == "Opacity: 50%"
    finally:
        dialog.close()
        pin.close()
        app.processEvents()


def test_capture_catalog_covers_literal_translation_keys():
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "snapvault/locales/en_capture.json").read_text(encoding="utf-8"))
    needed = set()
    for name in ("screenshot.py", "annotation.py", "screenshot_output.py", "image_compression.py", "pinboard.py"):
        tree = ast.parse((root / "snapvault" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
                    node.func.id == "tr" and node.args and isinstance(node.args[0], ast.Constant)):
                needed.add(node.args[0].value)
    assert needed <= catalog.keys()
    assert all(not re.search("[\u3400-\u9fff]", value) for value in catalog.values())
