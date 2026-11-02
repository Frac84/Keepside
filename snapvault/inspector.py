"""Original-image inspector, sharing the gallery's selection and local OCR."""
from __future__ import annotations
from .i18n import tr, language
from datetime import datetime
from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import QDialog, QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSplitter, QTextEdit, QVBoxLayout, QWidget
from .branding import app_name
from .gallery import glyph
from .glass import configure_window_glass, refresh_window_glass
from .ocr import clean_text
from .ocr_runtime import arrange
from .storage import normalized
from .theme import inspector_style, native_titlebar, theme
INSPECTOR_STYLE = inspector_style('light')

def _label(text='', name=None):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        label.setObjectName(name)
    return label

def _button(text, callback, tooltip='', name=None):
    button = QPushButton(text)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip if text in ('×', '←', '→', '−', '+') and tooltip else text)
    button.setAccessibleDescription(tooltip)
    if name:
        button.setObjectName(name)
    button.clicked.connect(callback)
    return button

class _ElidedLabel(QLabel):

    def __init__(self):
        super().__init__()
        self._full_text = ''
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)

    def set_full_text(self, text):
        self._full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._update_text()

    def _update_text(self):
        self.setText(self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideMiddle, max(10, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text()

def _box(line):
    return QRectF(line['x'], line['y'], line['w'], line['h'])

def lines_in(lines, rect):
    """OCR lines picked by a scene rectangle; a click picks the line under it."""
    if rect.width() < 4 and rect.height() < 4:
        return [line for line in lines if _box(line).contains(rect.center())]
    picked = []
    for line in lines:
        box = _box(line)
        overlap = rect.intersected(box)
        if rect.contains(box.center()) or overlap.width() * overlap.height() >= 0.5 * box.width() * box.height():
            picked.append(line)
    return picked

def selection_text(lines):
    """Reading-order text of picked lines, joined like the full OCR result."""
    rows = [{'x': line['x'], 'y': line['y'] + line['h'] / 2, 'h': line['h'], 'text': line['text']} for line in lines]
    return clean_text(arrange(rows))

class _ImageView(QGraphicsView):
    zoom_changed = Signal(float)
    image_changed = Signal(bool)
    selection_made = Signal(QRectF)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QColor(theme()['viewer_canvas']))
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(280, 220)
        self.setMouseTracking(True)
        self.item = None
        self.lines, self._line_items, self._terms = ([], [], ())
        self._select_mode, self._press = (False, None)
        self._fit_mode = True
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.fit_image)

    @property
    def has_image(self):
        return self.item is not None and (not self.item.pixmap().isNull())

    @property
    def zoom_percent(self):
        return self.transform().m11() * self.devicePixelRatioF() * 100

    def set_lines(self, lines, terms=()):
        """Overlay OCR line boxes; only search hits or selection mode show them."""
        for item in self._line_items:
            self.scene().removeItem(item)
        self._line_items = []
        self.lines = list(lines) if self.has_image else []
        for line in self.lines:
            item = self.scene().addRect(_box(line))
            item.setZValue(1)
            item.setData(0, line['text'])
            self._line_items.append(item)
        self.update_overlay(terms)

    def update_overlay(self, terms=None):
        if terms is not None:
            self._terms = tuple((term for term in terms if term))
        colors = theme()
        hit_color = QColor(colors['highlight'])
        hit_color.setAlpha(120)
        hit_pen = QPen(QColor(colors['warning']), 0)
        outline = QColor(colors['accent'])
        outline.setAlpha(170)
        select_pen = QPen(outline, 0, Qt.PenStyle.DashLine)
        for pen in (hit_pen, select_pen):
            pen.setCosmetic(True)
        for item in self._line_items:
            text = normalized(item.data(0))
            hit = any((term in text for term in self._terms))
            item.setBrush(QBrush(hit_color) if hit else QBrush(Qt.BrushStyle.NoBrush))
            item.setPen(hit_pen if hit else select_pen)
            item.setVisible(hit or self._select_mode)

    @property
    def select_mode(self):
        return self._select_mode

    def set_select_mode(self, enabled):
        self._select_mode = bool(enabled) and bool(self.lines)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag if self._select_mode else QGraphicsView.DragMode.ScrollHandDrag)
        if self._select_mode:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.viewport().unsetCursor()
        self.update_overlay()

    def mousePressEvent(self, event):
        if self._select_mode and event.button() == Qt.MouseButton.LeftButton:
            self._press = self.mapToScene(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._select_mode and self._press is not None and (event.button() == Qt.MouseButton.LeftButton):
            end = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._press, end).normalized()
            self._press = None
            self.selection_made.emit(rect)

    def set_image(self, path):
        self._fit_timer.stop()
        self.scene().clear()
        self._line_items, self.lines = ([], [])
        self.item = None
        self.resetTransform()
        pixmap = QPixmap(str(path)) if path else QPixmap()
        if not pixmap.isNull():
            pixmap.setDevicePixelRatio(1.0)
            self.item = self.scene().addPixmap(pixmap)
            self.scene().setSceneRect(self.item.boundingRect())
            self._fit_mode = True
            self.fit_image()
            self._fit_timer.start(0)
        else:
            self.scene().setSceneRect(0, 0, 1, 1)
        self.image_changed.emit(self.has_image)
        self.zoom_changed.emit(self.zoom_percent)

    def fit_image(self):
        if not self.has_image:
            return
        self._fit_mode = True
        self.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed.emit(self.zoom_percent)

    def actual_size(self):
        self._fit_mode = False
        self._set_ratio(1 / self.devicePixelRatioF())

    def zoom_by(self, factor, position=None):
        if not self.has_image or factor <= 0:
            return
        self._fit_mode = False
        ratio = self.transform().m11() * factor
        density = self.devicePixelRatioF()
        self._set_ratio(min(32 / density, max(0.01 / density, ratio)), position)

    def _set_ratio(self, ratio, position=None):
        if not self.has_image:
            return
        position = position or self.viewport().rect().center()
        before = self.mapToScene(position)
        current = self.transform().m11()
        self.scale(ratio / current, ratio / current)
        after = self.mapToScene(position)
        shift = after - before
        self.translate(shift.x(), shift.y())
        self.zoom_changed.emit(self.zoom_percent)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if not self.has_image or not delta:
            event.ignore()
            return
        self.zoom_by(1.2 ** max(-4, min(4, delta / 120)), event.position().toPoint())
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.has_image and self._fit_mode:
            self._fit_timer.start(0)
        else:
            self.zoom_changed.emit(self.zoom_percent)

    def showEvent(self, event):
        super().showEvent(event)
        if self.has_image and self._fit_mode:
            self._fit_timer.start(0)

class ImageInspector(QDialog):
    """Independent image viewer; the library stays available while it is open."""

    def __init__(self, host):
        super().__init__(host)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        configure_window_glass(self)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.host = host
        self._row = None
        self._image_id = None
        self._retry_id = None
        self._ocr_snapshot = None
        self.setObjectName('inspector')
        self.setWindowTitle(tr('{APP_NAME} · 图片预览', APP_NAME=app_name()))
        self.setWindowIcon(host.windowIcon())
        self.resize(1180, 800)
        self.setMinimumSize(920, 580)
        self.setStyleSheet(inspector_style())
        self._build_ui()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(600)
        self._refresh_timer.timeout.connect(self.refresh)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(self._reset_hint)
        host.gallery.currentRowChanged.connect(self.refresh)
        ocr = getattr(host, 'ocr', None)
        if ocr is not None and hasattr(ocr, 'changed'):
            ocr.changed.connect(self._ocr_changed)
        QShortcut(QKeySequence('Alt+Left'), self, activated=lambda: self.navigate(-1))
        QShortcut(QKeySequence('Alt+Right'), self, activated=lambda: self.navigate(1))
        QShortcut(QKeySequence('Ctrl++'), self, activated=lambda: self.view.zoom_by(1.2))
        QShortcut(QKeySequence('Ctrl+='), self, activated=lambda: self.view.zoom_by(1.2))
        QShortcut(QKeySequence('Ctrl+-'), self, activated=lambda: self.view.zoom_by(1 / 1.2))
        QShortcut(QKeySequence('Ctrl+0'), self, activated=self.view.actual_size)
        QShortcut(QKeySequence('Ctrl+P'), self, activated=self.pin_image)
        self.refresh()
        native_titlebar(self)

    def _update_caption(self):
        prefix = tr('{APP_NAME} · 预览', APP_NAME=app_name())
        row = self._row
        if row:
            name = ' '.join(str(row.get('filename', '')).splitlines())
            budget = min(420, max(100, self.width() - self.fontMetrics().horizontalAdvance(prefix) - 260))
            short = self.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, budget)
            caption = prefix + ' · ' + short
        else:
            caption = tr('{APP_NAME} · 图片预览', APP_NAME=app_name())
        if self.windowTitle() != caption:
            self.setWindowTitle(caption)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_row'):
            self._update_caption()

    def refresh_theme(self):
        palette = theme()
        self.setStyleSheet(inspector_style())
        self.view.setBackgroundBrush(QColor(palette['viewer_canvas']))
        self.header_mark.setPixmap(glyph('image', palette['accent'], 22).pixmap(22, 22))
        native_titlebar(self)
        refresh_window_glass(self, palette['dark'], enabled=palette['glass'])
        self.view.update_overlay()
        self.update()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QWidget()
        header.setObjectName('inspectorHeader')
        top = QHBoxLayout(header)
        top.setContentsMargins(22, 16, 18, 16)
        top.setSpacing(14)
        self.header_mark = _label('')
        self.header_mark.setObjectName('inspectorMark')
        self.header_mark.setFixedSize(26, 26)
        self.header_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.header_mark.setPixmap(glyph('image', theme()['accent'], 22).pixmap(22, 22))
        top.addWidget(self.header_mark)
        information = QVBoxLayout()
        information.setSpacing(5)
        self.filename = _ElidedLabel()
        self.filename.setObjectName('detailtitle')
        self.filename.setStyleSheet('font-size: 15px; font-weight: 600;')
        self.metadata = _label('', 'muted')
        information.addWidget(self.filename)
        information.addWidget(self.metadata)
        top.addLayout(information, 1)
        self.ocr_toggle = _button(tr('识别文字'), self._toggle_ocr, tr('显示或隐藏本地 OCR 面板'))
        self.ocr_toggle.setCheckable(True)
        self.ocr_toggle.setChecked(True)
        top.addWidget(self.ocr_toggle)
        close = _button('×', self.reject, tr('关闭大图查看器 · Esc'))
        close.setFixedWidth(38)
        close.setStyleSheet('font-size: 21px; padding: 3px;')
        top.addWidget(close)
        layout.addWidget(header)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName('inspectorSplit')
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        canvas = QWidget()
        canvas.setObjectName('inspectorCanvas')
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)
        self.view = _ImageView()
        self.view.setObjectName('inspectorCanvas')
        canvas_layout.addWidget(self.view, 1)
        self.empty_message = _label(tr('原图不存在或无法读取'))
        self.empty_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_message.setObjectName('inspectorEmpty')
        canvas_layout.addWidget(self.empty_message)
        self.splitter.addWidget(canvas)
        self.ocr_panel = QFrame()
        self.ocr_panel.setObjectName('inspectorOcr')
        self.ocr_panel.setMinimumWidth(270)
        panel = QVBoxLayout(self.ocr_panel)
        panel.setContentsMargins(22, 24, 22, 18)
        panel.setSpacing(14)
        panel.addWidget(_label(tr('本地 OCR 提取'), 'detailtitle'))
        panel.addWidget(_label(tr('离线小模型 · 自动中英'), 'muted'))
        self.ocr_status = _label('', 'muted')
        self.ocr_status.setWordWrap(True)
        panel.addWidget(self.ocr_status)
        self.ocr_text = QTextEdit()
        self.ocr_text.setReadOnly(True)
        self.ocr_text.setPlaceholderText(tr('识别出的文字会显示在这里。'))
        panel.addWidget(self.ocr_text, 1)
        ocr_actions = QHBoxLayout()
        self.copy_text_button = _button(tr('复制全文'), self.copy_text, tr('复制本地 OCR 识别出的文字'))
        self.retry_button = _button(tr('重新识别'), self.retry_ocr, tr('使用离线小模型重新识别；失败时保留原有文字'))
        ocr_actions.addWidget(self.copy_text_button)
        ocr_actions.addWidget(self.retry_button)
        panel.addLayout(ocr_actions)
        note = _label(tr('本地识别，队列空闲后释放模型；图片不上传。搜索词会在图上高亮。'), 'muted')
        note.setWordWrap(True)
        panel.addWidget(note)
        self.splitter.addWidget(self.ocr_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([830, 350])
        layout.addWidget(self.splitter, 1)
        footer = QWidget()
        footer.setObjectName('inspectorFooter')
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(18, 14, 18, 12)
        footer_layout.setSpacing(9)
        tools = QFrame()
        tools.setObjectName('inspectorTools')
        tools.setMaximumWidth(1100)
        actions = QHBoxLayout(tools)
        actions.setContentsMargins(12, 7, 12, 7)
        actions.setSpacing(5)
        self.previous_button = _button('←', lambda: self.navigate(-1), tr('上一张 · Alt+←'))
        self.next_button = _button('→', lambda: self.navigate(1), tr('下一张 · Alt+→'))
        for control in (self.previous_button, self.next_button):
            control.setFixedWidth(34)
        self.page = _label('')
        self.page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page.setMinimumWidth(48)
        actions.addWidget(self.previous_button)
        actions.addWidget(self.page)
        actions.addWidget(self.next_button)
        self._separator(actions)
        zoom_out = _button('−', lambda: self.view.zoom_by(1 / 1.2), tr('缩小 · Ctrl+−'))
        zoom_out.setFixedWidth(32)
        self.zoom_label = _label('100%', 'inspectorZoom')
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setMinimumWidth(54)
        zoom_in = _button('+', lambda: self.view.zoom_by(1.2), tr('放大 · Ctrl++'))
        zoom_in.setFixedWidth(32)
        actions.addWidget(zoom_out)
        actions.addWidget(self.zoom_label)
        actions.addWidget(zoom_in)
        self.select_button = _button(tr('框选文字'), self._toggle_select, tr('在图上拖出范围或单击一行，复制其中的识别文字'))
        self.select_button.setCheckable(True)
        actions.addWidget(self.select_button)
        self._separator(actions)
        fit = _button(tr('适应窗口'), self.view.fit_image, tr('让整张图片完整显示；调整窗口时自动适应'))
        actual = _button('100%', self.view.actual_size, tr('1 个图像像素对应 1 个屏幕像素 · Ctrl+0'))
        actions.addWidget(fit)
        actions.addWidget(actual)
        self._separator(actions)
        copy = _button(tr('复制原图'), self.copy_image, tr('复制完整原始像素，可粘贴到微信'), 'primary')
        export = _button(tr('导出'), self.export_image, tr('将原始 PNG 导出到指定位置'))
        open_image = _button(tr('打开原图'), self.open_original, tr('使用系统默认图片程序打开'))
        pin = _button(tr('贴图'), self.pin_image, tr('将这张截图贴到屏幕并置顶 · Ctrl+P'))
        pin.setObjectName('inspectorPin')
        actions.addWidget(copy)
        actions.addWidget(pin)
        actions.addWidget(export)
        actions.addWidget(open_image)
        self._image_buttons = [zoom_out, zoom_in, fit, actual, copy, pin, export, open_image]
        footer_layout.addWidget(tools, 0, Qt.AlignmentFlag.AlignHCenter)
        self.hint = _label('')
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_layout.addWidget(self.hint)
        self._reset_hint()
        layout.addWidget(footer)
        self.view.zoom_changed.connect(self._update_zoom)
        self.view.image_changed.connect(self._image_available)
        self.view.selection_made.connect(self._copy_selection)

    @staticmethod
    def _separator(layout):
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedHeight(22)
        separator.setObjectName('inspectorSeparator')
        layout.addSpacing(5)
        layout.addWidget(separator)
        layout.addSpacing(5)

    def _toggle_ocr(self, checked):
        self.ocr_panel.setVisible(checked)

    def _toggle_select(self, checked):
        self.view.set_select_mode(checked)
        if checked and (not self.view.select_mode):
            self.select_button.setChecked(False)
        self._feedback(tr('拖出范围或单击一行即可复制文字；再次点击「框选文字」退出') if self.view.select_mode else tr('已退出框选文字'))

    def _update_select_button(self):
        available = bool(self.view.lines)
        self.select_button.setEnabled(available)
        self.select_button.setToolTip(tr('在图上拖出范围或单击一行，复制其中的识别文字') if available else tr('这张图还没有文字位置信息；点击「重新识别」后即可框选'))
        if not available and self.select_button.isChecked():
            self.select_button.setChecked(False)
            self.view.set_select_mode(False)

    def _copy_selection(self, rect):
        picked = lines_in(self.view.lines, rect)
        if not picked:
            self._feedback(tr('这个范围里没有识别到文字'))
            return
        text = selection_text(picked)
        self.host.copy_plain_text(text)
        self._feedback(tr('已复制 {value_0} 段文字', value_0=len(picked)))

    def _update_zoom(self, percent):
        self.zoom_label.setText((f'{percent:.1f}%' if percent < 10 else f'{percent:.0f}%') if self.view.has_image else '—')

    def _image_available(self, available):
        self.empty_message.setVisible(not available)
        for button in self._image_buttons:
            button.setEnabled(available)

    def _reset_hint(self):
        self.hint.setText(tr('滚轮缩放 · 按住鼠标拖动 · Alt+← / → 切换截图'))

    def _feedback(self, text):
        self.hint.setText(text)
        self._feedback_timer.start(4000)

    def refresh(self, *_):
        key = self.host.active_id
        row = self.host.vault.get(key) if key else None
        if row and row.get('item_type', 'image') != 'image':
            row = None
        self._row = row
        indices = self._image_indices()
        index = self.host.gallery.currentRow()
        ordinal = indices.index(index) if index in indices else -1
        self.previous_button.setEnabled(bool(row) and ordinal > 0)
        self.next_button.setEnabled(bool(row) and 0 <= ordinal < len(indices) - 1)
        self.page.setText(f'{ordinal + 1} / {len(indices)}' if row and ordinal >= 0 else f'— / {len(indices)}')
        if not row:
            self._update_caption()
            self.select_button.setChecked(False)
            self.view.set_select_mode(False)
            self.select_button.setEnabled(False)
            self.filename.set_full_text(tr('这张截图已不在当前图库中'))
            self.metadata.setText(tr('关闭查看器后可重新选择截图'))
            self._image_id = None
            self._ocr_snapshot = None
            self.view.set_image(None)
            self.ocr_text.clear()
            self.ocr_status.setText(tr('没有可显示的识别结果'))
            self.copy_text_button.setEnabled(False)
            self.retry_button.setEnabled(False)
            return
        self.filename.set_full_text(row['filename'])
        try:
            captured = datetime.fromisoformat(row['captured_at']).astimezone().strftime('%Y/%m/%d %H:%M')
        except (ValueError, TypeError):
            captured = ''
        self.metadata.setText(f"{row['width']:,} × {row['height']:,} px  ·  {row['size'] / 1024:,.0f} KB  ·  {captured}")
        self._update_caption()
        reloaded = False
        if self._image_id != key or not self.view.has_image:
            self._image_id = key
            self._retry_id = None
            self.view.set_image(self.host.vault.images / row['filename'])
            reloaded = True
        text = row.get('ocr_text', '')
        snapshot = (key, text)
        if snapshot != self._ocr_snapshot or reloaded:
            self.ocr_text.setPlainText(text)
            self._ocr_snapshot = snapshot
            self.view.set_lines(self.host.vault.ocr_lines(key), normalized(self.host.search.text()).split())
            self._update_select_button()
        self.copy_text_button.setEnabled(bool(text))
        self.retry_button.setEnabled(self._retry_id != key)
        status = row.get('ocr_status', 'pending')
        if self._retry_id == key:
            self.ocr_status.setText(tr('已加入本地识别队列…'))
        elif status == 'error':
            prefix = tr('重新识别失败，已保留原有文字：') if text else tr('识别失败：')
            self.ocr_status.setText(prefix + row.get('ocr_error', tr('未知错误'))[:260])
        elif status == 'pending':
            self.ocr_status.setText(tr('正在等待本地识别队列…'))
        else:
            self.ocr_status.setText(tr('识别完成 · {value_0:,} 个字符', value_0=len(text)) if text else tr('识别完成，未发现可读文字'))

    def _ocr_changed(self, key):
        if key == self._retry_id:
            self._retry_id = None
        if key == self.host.active_id:
            self.refresh()

    def _image_indices(self):
        return [i for i in range(self.host.gallery.count()) if self.host.gallery.item(i).data(Qt.ItemDataRole.UserRole).get('item_type', 'image') == 'image']

    def navigate(self, direction):
        index = self.host.gallery.currentRow()
        candidates = [i for i in self._image_indices() if (i > index if direction > 0 else i < index)]
        if candidates and direction:
            self.host.gallery.setCurrentRow(candidates[0] if direction > 0 else candidates[-1])
            self.refresh()

    def copy_image(self):
        self.refresh()
        if self._row and self.view.has_image:
            self.host.copy_image()
            self._feedback(tr('已复制完整原图，可以粘贴到微信'))

    def export_image(self):
        self.refresh()
        if self._row and self.view.has_image:
            self.host.export_image()

    def pin_image(self):
        self.refresh()
        if self._row and self.view.has_image:
            pin = self.host.pin_image()
            if pin is not None:
                self.accept()

    def open_original(self):
        self.refresh()
        if self._row and self.view.has_image:
            path = self.host.vault.images / self._row['filename']
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self._feedback(tr('无法打开系统图片程序，请使用“导出”保存原图'))

    def copy_text(self):
        self.refresh()
        if self._row and self._row.get('ocr_text'):
            self.host.update_detail()
            self.host.copy_text()
            self._feedback(tr('已复制本地 OCR 全文'))

    def retry_ocr(self):
        self.refresh()
        if self._row:
            self._retry_id = self.host.active_id
            self.host.retry_ocr()
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_theme()
        self.refresh()
        self._refresh_timer.start()

    def hideEvent(self, event):
        self._refresh_timer.stop()
        self._feedback_timer.stop()
        super().hideEvent(event)
