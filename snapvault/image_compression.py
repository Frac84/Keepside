"""Local, bounded-size image export. Source images are never overwritten."""
from __future__ import annotations

from .i18n import tr

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable
import math

from PIL import Image, ImageOps
from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QStyle, QStyleOption, QVBoxLayout, QWidget,
)

from .glass import configure_window_glass, refresh_window_glass
from .theme import native_titlebar, theme


class CompressionCancelled(Exception):
    """Cooperative stop requested between encoder calls."""


@dataclass(frozen=True)
class CompressionResult:
    data: bytes
    format: str
    width: int
    height: int
    quality: int | None
    original_width: int
    original_height: int
    flattened_alpha: bool = False

    @property
    def extension(self):
        return {"JPEG": ".jpg", "WEBP": ".webp", "PNG": ".png"}[self.format]

    @property
    def resized(self):
        return (self.width, self.height) != (self.original_width, self.original_height)


def compress_image(image: Image.Image, target_bytes: int, output_format="AUTO",
                   cancelled: Callable[[], bool] | None = None) -> CompressionResult:
    """Find a verified encoded result under the limit, shrinking only if needed.

    The highest quality found by bounded search is retained at each size. Every
    returned byte string is checked against the exact limit, so non-monotonic
    encoder sizes cannot accidentally produce an oversized export.
    """
    if isinstance(target_bytes, bool) or not isinstance(target_bytes, int) or target_bytes <= 0:
        raise ValueError(tr('目标大小必须大于 0。'))
    if not isinstance(image, Image.Image) or image.width <= 0 or image.height <= 0:
        raise ValueError(tr('没有可压缩的图片。'))
    fmt = str(output_format).upper()
    if fmt not in {"AUTO", "JPEG", "WEBP", "PNG"}:
        raise ValueError(tr('不支持的输出格式。'))

    def check_cancel():
        if cancelled is not None and cancelled():
            raise CompressionCancelled()

    check_cancel()
    source = ImageOps.exif_transpose(image)
    source.load()
    width, height = source.size
    alpha = source.mode in {"RGBA", "LA"} or (source.mode == "P" and "transparency" in source.info)
    if alpha:
        with source.convert("RGBA") as alpha_source:
            with alpha_source.getchannel("A") as channel:
                alpha = channel.getextrema()[0] < 255
    if fmt == "AUTO":
        fmt = "WEBP" if alpha else "JPEG"
    flattened = bool(alpha and fmt == "JPEG")
    if flattened:
        rgba = source.convert("RGBA")
        prepared = Image.new("RGB", source.size, "white")
        prepared.paste(rgba, mask=rgba.getchannel("A"))
        rgba.close()
    elif alpha and fmt in {"WEBP", "PNG"}:
        prepared = source.convert("RGBA")
    else:
        prepared = source.convert("RGB")
    # No metadata is forwarded: EXIF orientation has already been applied and
    # unrelated camera/location fields do not belong to a smaller export.
    prepared.info.clear()

    def encode(candidate, quality=None):
        check_cancel()
        output = BytesIO()
        if fmt == "JPEG":
            candidate.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
        elif fmt == "WEBP":
            candidate.save(output, "WEBP", quality=quality, method=4)
        else:
            candidate.save(output, "PNG", optimize=True, compress_level=9)
        return output.getvalue()

    current = prepared
    try:
        while True:
            check_cancel()
            if fmt == "PNG":
                smallest = encode(current)
                found, quality = (smallest, None) if len(smallest) <= target_bytes else (None, None)
            else:
                highest, lowest = 95, 20
                full = encode(current, highest)
                found, quality = (full, highest) if len(full) <= target_bytes else (None, None)
                smallest = full
                if found is None:
                    smallest = encode(current, lowest)
                    if len(smallest) <= target_bytes:
                        found, quality = smallest, lowest
                        lo, hi = lowest + 1, highest - 1
                        while lo <= hi:
                            mid = (lo + hi) // 2
                            data = encode(current, mid)
                            if len(data) <= target_bytes:
                                found, quality, lo = data, mid, mid + 1
                            else:
                                hi = mid - 1
            if found is not None:
                check_cancel()
                assert len(found) <= target_bytes
                return CompressionResult(found, fmt, current.width, current.height, quality,
                                         width, height, flattened)
            if current.size == (1, 1):
                raise ValueError(tr('目标太小，连最小图片的文件头也无法容纳。请增大目标大小。'))
            # Area roughly follows encoded size. Cap each step so the loop
            # always advances, including flat PNGs dominated by file headers.
            scale = max(.1, min(.85, math.sqrt(target_bytes / max(1, len(smallest))) * .9))
            new_width = max(1, int(current.width * scale))
            new_height = max(1, int(current.height * scale))
            # Resize from the original to avoid accumulating repeated blur.
            reduced = prepared.resize((new_width, new_height), Image.Resampling.LANCZOS)
            if current is not prepared:
                current.close()
            current = reduced
    finally:
        if current is not prepared:
            current.close()
        prepared.close()
        if source is not image:
            source.close()


def pil_from_qimage(image: QImage):
    """Detach a Qt image into a Pillow RGBA image for the encoder thread."""
    if image.isNull():
        raise ValueError(tr('没有可压缩的图片。'))
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA", (rgba.width(), rgba.height()), rgba.bits().tobytes(),
                           "raw", "RGBA", rgba.bytesPerLine())


class _CompressionWorker(QThread):
    resultReady = Signal(object)
    failed = Signal(str)

    def __init__(self, image, target, fmt, parent):
        super().__init__(parent)
        self.image, self.target, self.fmt = image.copy(), target, fmt

    def run(self):
        try:
            with pil_from_qimage(self.image) as source:
                result = compress_image(source, self.target, self.fmt, self.isInterruptionRequested)
            self.resultReady.emit(result)
        except CompressionCancelled:
            pass
        except Exception as exc:
            self.failed.emit(tr('压缩未完成：{value0}', value0=exc))


class _Preview(QLabel):
    def __init__(self, image=None):
        super().__init__()
        self.image = QImage() if image is None else image.copy()
        self.setMinimumSize(180, 160)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setObjectName("compressionPreview")

    def set_image(self, image):
        self.image = image.copy()
        self._fit()

    def _fit(self):
        if not self.image.isNull():
            self.setPixmap(QPixmap.fromImage(self.image).scaled(
                max(1, self.width()-20), max(1, self.height()-20),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()


def _size_text(size):
    return f"{size / 1024 / 1024:.2f} MB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KB"


class CompressionDialog(QDialog):
    """A directly usable target-size export window, with a cancellable worker."""
    copyRequested = Signal(QImage)
    fileCopyRequested = Signal(object)

    def __init__(self, image: QImage, parent=None, *, source_name=None, source_size=None,
                 source_bytes=None, source_path=None):
        super().__init__(parent)
        if image.isNull():
            raise ValueError(tr('没有可压缩的图片。'))
        configure_window_glass(self)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.image = image.copy()
        self.source_name = Path(source_name).stem if source_name else tr("图片")
        self.source_path = Path(source_path).resolve() if source_path is not None else None
        self.original_size = (int(source_size) if isinstance(source_size, int) and source_size >= 0 else
                              len(source_bytes) if isinstance(source_bytes, (bytes, bytearray)) else None)
        self.result = None
        self.worker = None
        self._closing = False
        self.setWindowTitle(tr('图片压缩到指定大小'))
        self.setObjectName("compressionDialog")
        self.resize(860, 630)
        self.setMinimumSize(660, 500)
        self._build_ui()
        self.refresh_theme()
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.start_compression)
        QTimer.singleShot(0, self.start_compression)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(14)
        title = QLabel(tr('压缩到多大？'))
        title.setObjectName("compressionTitle")
        layout.addWidget(title)
        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addWidget(QLabel(tr('目标上限')))
        self.target = QDoubleSpinBox()
        self.target.setObjectName("compressionTarget")
        self.target.setAccessibleName(tr('压缩后文件大小上限'))
        self.target.setRange(1, 102400)
        self.target.setDecimals(0)
        self.target.setValue(500)
        self.target.setSingleStep(100)
        self.target.setMinimumWidth(120)
        controls.addWidget(self.target)
        self.unit = QComboBox()
        self.unit.setAccessibleName(tr('目标大小单位'))
        self.unit.addItems(["KB", "MB"])
        controls.addWidget(self.unit)
        controls.addSpacing(14)
        controls.addWidget(QLabel(tr('格式')))
        self.format = QComboBox()
        self.format.setAccessibleName(tr('输出图片格式'))
        for title, value in ((tr('自动选择'), "AUTO"), ("JPEG", "JPEG"), (tr('WebP（保留透明）'), "WEBP"), (tr('PNG（保留透明）'), "PNG")):
            self.format.addItem(title, value)
        controls.addWidget(self.format)
        controls.addStretch()
        self.compress_button = QPushButton(tr('开始压缩'))
        self.compress_button.setObjectName("compressionPrimary")
        self.compress_button.clicked.connect(self.start_compression)
        controls.addWidget(self.compress_button)
        layout.addLayout(controls)
        self.format_hint = QLabel(tr('自动选择：普通图片保存为 JPEG，透明图片保存为 WebP。超出上限时会适当缩小尺寸。'))
        self.format_hint.setWordWrap(True)
        self.format_hint.setObjectName("compressionMuted")
        layout.addWidget(self.format_hint)
        previews = QHBoxLayout()
        previews.setSpacing(16)
        left, right = QVBoxLayout(), QVBoxLayout()
        original = tr('原图 · {value0} × {value1}', value0=self.image.width(), value1=self.image.height())
        if self.original_size is not None:
            original += f" · {_size_text(self.original_size)}"
        left.addWidget(QLabel(original))
        self.original_preview = _Preview(self.image)
        left.addWidget(self.original_preview, 1)
        self.result_info = QLabel(tr('压缩结果'))
        right.addWidget(self.result_info)
        self.result_preview = _Preview()
        right.addWidget(self.result_preview, 1)
        previews.addLayout(left, 1)
        previews.addLayout(right, 1)
        layout.addLayout(previews, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel(tr('只处理本机图片。原图保持不变。'))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName("compressionStatus")
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch()
        self.copy_button = QPushButton(tr('复制压缩文件'))
        self.copy_button.setToolTip(tr('以文件形式复制，粘贴发送时保留当前文件大小'))
        self.copy_button.clicked.connect(self.copy_file)
        self.copy_button.setEnabled(False)
        footer.addWidget(self.copy_button)
        self.save_button = QPushButton(tr('另存为…'))
        self.save_button.setObjectName("compressionPrimary")
        self.save_button.clicked.connect(self.save_file)
        self.save_button.setEnabled(False)
        footer.addWidget(self.save_button)
        close = QPushButton(tr('关闭'))
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.target.valueChanged.connect(self._invalidate)
        self.unit.currentIndexChanged.connect(self._unit_changed)
        self.format.currentIndexChanged.connect(self._invalidate)

    def refresh_theme(self):
        p = theme()
        self.setStyleSheet(f"""
            QDialog#compressionDialog {{ background: {p['canvas']}; color:{p['text']}; }}
            QDialog#compressionDialog[nativeGlass="true"] {{ background:{p['material_canvas']}; }}
            QLabel {{ color:{p['text']}; background:transparent; }}
            QLabel#compressionTitle {{ font-size:20px; font-weight:600; }}
            QLabel#compressionMuted {{ color:{p['secondary']}; }}
            QLabel#compressionPreview {{ background:{p['preview']}; border:1px solid {p['border']}; border-radius:8px; }}
            QPushButton {{ color:{p['text']}; background:{p['card']}; border:1px solid {p['border']}; border-radius:5px; padding:8px 14px; min-height:20px; }}
            QPushButton:hover {{ background:{p['hover']}; border-color:{p['border_hover']}; }}
            QPushButton:focus {{ border:2px solid {p['accent']}; }}
            QPushButton#compressionPrimary {{ color:{p['on_accent']}; background:{p['accent']}; border-color:{p['accent']}; }}
            QPushButton#compressionPrimary:hover {{ background:{p['accent_hover']}; }}
            QPushButton:disabled {{ color:{p['disabled_fg']}; background:{p['disabled_bg']}; border-color:{p['border']}; }}
            QDoubleSpinBox, QComboBox {{ color:{p['text']}; background:{p['input']}; border:1px solid {p['border']}; border-radius:5px; padding:7px; }}
            QProgressBar {{ background:{p['border']}; border:0; }}
            QProgressBar::chunk {{ background:{p['accent']}; }}
        """)
        native_titlebar(self)
        refresh_window_glass(self, p["dark"], enabled=p["glass"])

    def paintEvent(self, event):
        # An alpha-backed QDialog does not fill its own stylesheet background
        # on every Qt platform. Paint the style primitive so opaque fallback
        # themes still have a readable window when native Acrylic is absent.
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)
        painter.end()

    def _unit_changed(self):
        # Keep the same byte limit when switching units rather than silently
        # turning a 500 KB request into a 500 MB request.
        self.target.blockSignals(True)
        value = self.target.value()
        if self.unit.currentText() == "MB":
            self.target.setDecimals(3)
            self.target.setRange(.001, 100)
            self.target.setSingleStep(.1)
            self.target.setValue(value / 1024)
        else:
            self.target.setDecimals(0)
            self.target.setRange(1, 102400)
            self.target.setSingleStep(100)
            self.target.setValue(value * 1024)
        self.target.blockSignals(False)
        self._invalidate()

    def _invalidate(self):
        self.result = None
        self.copy_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.result_preview.set_image(QImage())
        self.result_info.setText(tr('压缩结果'))
        self.status.setText(tr('目标已改变，点击“开始压缩”生成新结果。'))
        if self.format.currentData() == "JPEG":
            self.format_hint.setText(tr('JPEG 不保留透明区域，透明区域将使用白色背景。超出上限时会适当缩小尺寸。'))
        elif self.format.currentData() == "PNG":
            self.format_hint.setText(tr('PNG 保留透明与清晰边缘；为满足很小的上限，可能需要明显缩小尺寸。'))
        elif self.format.currentData() == "WEBP":
            self.format_hint.setText(tr('WebP 保留透明区域；请确认接收图片的平台支持 WebP。'))
        else:
            self.format_hint.setText(tr('自动选择：普通图片保存为 JPEG，透明图片保存为 WebP。超出上限时会适当缩小尺寸。'))

    def _set_busy(self, busy):
        for control in (self.target, self.unit, self.format):
            control.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.compress_button.setText(tr('取消压缩') if busy else tr('开始压缩'))
        self.compress_button.setEnabled(True)
        self.copy_button.setEnabled(not busy and self.result is not None)
        self.save_button.setEnabled(not busy and self.result is not None)

    def start_compression(self):
        if self._closing:
            return
        if self.worker is not None:
            self.worker.requestInterruption()
            self.status.setText(tr('正在取消压缩…'))
            self.compress_button.setEnabled(False)
            return
        self._invalidate()
        target = int(self.target.value() * (1024 * 1024 if self.unit.currentText() == "MB" else 1024))
        self.status.setText(tr('正在压缩，可继续操作其他窗口。'))
        self._set_busy(True)
        worker = _CompressionWorker(self.image, target, self.format.currentData(), self)
        self.worker = worker
        worker.resultReady.connect(self._result_ready)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._worker_finished)
        worker.start()

    def _result_ready(self, result):
        if self._closing or self.worker is None or self.worker.isInterruptionRequested():
            return
        preview = QImage.fromData(result.data)
        if preview.isNull():
            self._failed(tr('压缩结果无法解码，请换一种格式。'))
            return
        self.result = result
        self.result_preview.set_image(preview)
        self.result_info.setText(f"{result.format} · {_size_text(len(result.data))} · {result.width} × {result.height}")
        message = tr('已达到目标大小。')
        if result.resized:
            message += tr('尺寸已缩小到 {value0} × {value1}。', value0=result.width, value1=result.height)
        if result.flattened_alpha:
            message += tr('透明区域已填为白色。')
        self.status.setText(message + tr('原图保持不变。'))

    def _failed(self, message):
        self.status.setText(message)

    def _worker_finished(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            interrupted = worker.isInterruptionRequested()
            worker.deleteLater()
            if interrupted and not self._closing:
                self.result = None
                self.status.setText(tr('已取消。原图保持不变。'))
        self._set_busy(False)
        if self._closing:
            super().reject()

    def copy_file(self):
        if self.result is not None and self.worker is None:
            self.fileCopyRequested.emit(self.result)

    def save_file(self):
        if self.result is None or self.worker is not None:
            return
        result = self.result
        filters = {"JPEG": tr('JPEG 图片 (*.jpg)'), "WEBP": tr('WebP 图片 (*.webp)'), "PNG": tr('PNG 图片 (*.png)')}
        suggested = self.source_name + tr('-已压缩') + result.extension
        name, _ = QFileDialog.getSaveFileName(self, tr('另存压缩后的图片'), suggested, filters[result.format])
        if not name:
            return
        path = Path(name)
        if path.suffix.lower() not in ({".jpg", ".jpeg"} if result.format == "JPEG" else {result.extension}):
            self.status.setText(tr('请选择 {value0} 扩展名，确保文件格式与内容一致。', value0=result.extension))
            return
        try:
            if self.source_path is not None and (path.resolve() == self.source_path or
                    (path.exists() and self.source_path.exists() and path.samefile(self.source_path))):
                self.status.setText(tr('请选择新的文件名，原图需要保留。'))
                return
            path.write_bytes(result.data)
            self.status.setText(tr('已保存 {value0} 的 {value1} 图片。原图保持不变。', value0=_size_text(len(result.data)), value1=result.format))
        except OSError as exc:
            self.status.setText(tr('保存失败：{value0}', value0=exc))

    def reject(self):
        self._closing = True
        if self.worker is not None:
            self.worker.requestInterruption()
            self.status.setText(tr('正在结束压缩…'))
            self.compress_button.setEnabled(False)
            return
        super().reject()

    def shutdown(self):
        """Join the encoder during application exit, never destroy a live QThread."""
        self._closing = True
        if self.worker is not None:
            self.worker.requestInterruption()
            self.worker.wait()

    def closeEvent(self, event):
        if self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
