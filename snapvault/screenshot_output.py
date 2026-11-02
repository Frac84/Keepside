from .i18n import tr
from .branding import app_name
"""Local screenshot outputs: atomic PNG export and a modeless OCR result."""
from pathlib import Path

from PySide6.QtCore import QSaveFile, Qt, Signal
from PySide6.QtGui import QImageWriter, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QTextEdit, QVBoxLayout)

from .theme import native_titlebar, theme


def write_screenshot_png(image, filename):
    """Write only flattened pixels, replacing a file only after a full write."""
    if image.isNull():
        raise ValueError(tr('截图为空'))
    path = Path(filename)
    if path.suffix.lower() != ".png":
        path = path.with_name(path.name + ".png")
    output = QSaveFile(str(path))
    if not output.open(QSaveFile.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    writer = QImageWriter(output, b"png")
    if not writer.write(image):
        output.cancelWriting()
        raise OSError(writer.errorString())
    if not output.commit():
        raise OSError(output.errorString())
    return path


class ScreenshotTextDialog(QDialog):
    """Follow an existing local OCR job without adding a worker or changing focus."""
    copyRequested = Signal(str)
    retryRequested = Signal(str)
    transientRetryRequested = Signal()

    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.setObjectName("screenshotText")
        self.setWindowTitle(tr('截图识字 · {app}', app=app_name()))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint)
        self.resize(640, 600)
        self.setMinimumSize(420, 360)
        self.item_id = None
        self._transient = False
        self._preview = QPixmap.fromImage(image)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QLabel(tr('截图识字'))
        heading.setStyleSheet("font-size:20px;font-weight:600")
        layout.addWidget(heading)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(132)
        layout.addWidget(self.preview)
        self.status = QLabel(tr('正在保存截图，随后在本机识别文字…'))
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlaceholderText(tr('识别结果会显示在这里，可选择部分文字或复制全文。'))
        self.text.setAccessibleName(tr('截图识别文字'))
        layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.retry_button = QPushButton(tr('重新识别'))
        self.retry_button.clicked.connect(self.retry)
        self.retry_button.setEnabled(False)
        row.addWidget(self.retry_button)
        row.addStretch()
        close = QPushButton(tr('关闭'))
        close.clicked.connect(self.close)
        row.addWidget(close)
        self.copy_button = QPushButton(tr('复制全文'))
        self.copy_button.setObjectName("primary")
        self.copy_button.clicked.connect(self.copy_text)
        self.copy_button.setEnabled(False)
        row.addWidget(self.copy_button)
        for button in (self.retry_button, close, self.copy_button):
            button.setAutoDefault(False)
            button.setMinimumHeight(34)
        layout.addLayout(row)
        self.shortcut = QShortcut(QKeySequence("Ctrl+Shift+C"), self,
                                  activated=self.copy_text)
        self.refresh_theme()

    def set_item(self, key):
        self.item_id = key

    def set_transient(self):
        self._transient = True
        self.status.setText(tr('正在本机识别文字，不存入内容库…'))

    def update_transient(self, result, error=""):
        self.update_result(dict(ocr_text=result.get("text", ""),
                                ocr_status="error" if error else "done", ocr_error=error))
        if not error:
            self.status.setText(self.status.text() + tr(' · 未入库'))

    def update_result(self, row):
        if not row:
            self.fail(tr('截图记录不可用，请重新截图。'))
            return
        value = row.get("ocr_text", "")
        if self.text.toPlainText() != value:
            self.text.setPlainText(value)
        status = row.get("ocr_status", "pending")
        self.copy_button.setEnabled(bool(value.strip()))
        self.retry_button.setEnabled(bool(self.item_id or self._transient) and status != "pending")
        if status == "error":
            self.status.setText(tr('本地识别失败：') + row.get("ocr_error", tr('请重试'))[:240])
        elif status == "done":
            self.status.setText(tr('本地识别完成 · {value0} 个字符', value0=len(value)) if value.strip()
                                else tr('识别完成，未发现可读文字。'))
        else:
            self.status.setText(tr('正在本机识别文字…'))

    def fail(self, message):
        self.status.setText(message)
        self.retry_button.setEnabled(bool(self.item_id or self._transient))

    def copy_text(self):
        value = self.text.toPlainText()
        if value.strip():
            self.copyRequested.emit(value)
            self.status.setText(tr('已复制全文。'))

    def retry(self):
        if self._transient:
            self.retry_button.setEnabled(False)
            self.status.setText(tr('正在本机重新识别，不存入内容库…'))
            self.transientRetryRequested.emit()
            return
        if self.item_id:
            self.retry_button.setEnabled(False)
            self.status.setText(tr('正在本机重新识别…'))
            self.retryRequested.emit(self.item_id)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.preview.setPixmap(self._preview.scaled(max(1, self.width() - 40), 132,
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def refresh_theme(self):
        p = theme()
        self.setStyleSheet(f"""
QDialog#screenshotText {{ background:{p['surface']}; color:{p['text']}; }}
QDialog#screenshotText QLabel {{ color:{p['text']}; background:transparent; }}
QDialog#screenshotText QTextEdit {{ background:{p['card']}; color:{p['text']};
 border:1px solid {p['border']}; border-radius:8px; padding:10px; }}
QDialog#screenshotText QPushButton {{ background:{p['card']}; color:{p['text']};
 border:1px solid {p['border']}; border-radius:6px; padding:4px 14px; }}
QDialog#screenshotText QPushButton:hover {{ background:{p['hover']}; }}
QDialog#screenshotText QPushButton#primary {{ background:{p['accent']}; color:{p['on_accent']}; }}
QDialog#screenshotText QPushButton:disabled {{ color:{p['disabled_fg']}; background:{p['disabled_bg']}; }}
QDialog#screenshotText QPushButton:focus {{ border:2px solid {p['accent']}; }}
""")
        native_titlebar(self)
