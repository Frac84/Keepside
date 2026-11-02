"""Responsive local image, text and link cards; no browser engine involvement."""
from .i18n import tr, language
from collections import OrderedDict
from datetime import datetime, timezone
import math
from PySide6.QtCore import Qt, QSize, QRectF, QPointF, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QListWidget, QStyledItemDelegate, QStyle
from .classification import ALL_TOPIC_LABELS, is_code_content
from .theme import theme

def glyph(name, color=None, size=18):
    """Small local vector icons, independent of web fonts or downloaded assets."""
    colors = theme()
    color = color or colors['accent']
    pix = QPixmap(size * 2, size * 2)
    pix.setDevicePixelRatio(2)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 24, size / 24)
    pen = QPen(QColor(color), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    def line(x1, y1, x2, y2):
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    if name in ('image', 'grid'):
        p.drawRoundedRect(QRectF(5, 4, 16, 15), 1.5, 1.5)
        line(2, 7, 2, 22)
        line(2, 22, 18, 22)
        p.drawEllipse(QRectF(8, 7, 3, 3))
        path = QPainterPath(QPointF(7, 16))
        path.lineTo(12, 11)
        path.lineTo(15, 14)
        path.lineTo(18, 11)
        p.drawPath(path)
    elif name in ('today', 'yesterday', 'week'):
        p.drawRoundedRect(QRectF(4, 5, 16, 16), 2, 2)
        line(4, 10, 20, 10)
        line(8, 2, 8, 7)
        line(16, 2, 16, 7)
        if name == 'today':
            p.setBrush(QColor(color))
            p.drawEllipse(QRectF(10, 13, 4, 4))
        else:
            for x in (8, 12, 16):
                p.drawPoint(QPointF(x, 14))
    elif name == 'star':
        path = QPainterPath()
        for i in range(10):
            r = 10 if i % 2 == 0 else 4.6
            a = math.pi * i / 5 - math.pi / 2
            pt = QPointF(12 + math.cos(a) * r, 12 + math.sin(a) * r)
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        path.closeSubpath()
        p.drawPath(path)
    elif name == 'settings':
        for y, x in ((6, 8), (12, 16), (18, 10)):
            line(3, y, 21, y)
            p.setBrush(QColor(colors['canvas']))
            p.drawRoundedRect(QRectF(x - 2, y - 3, 4, 6), 1, 1)
    elif name == 'layers':
        for dy in (0, 5, 10):
            path = QPainterPath(QPointF(3, 7 + dy))
            path.lineTo(12, 2 + dy)
            path.lineTo(21, 7 + dy)
            if not dy:
                path.lineTo(12, 12)
                path.closeSubpath()
            else:
                path = QPainterPath(QPointF(3, 7 + dy))
                path.lineTo(12, 12 + dy)
                path.lineTo(21, 7 + dy)
            p.drawPath(path)
    elif name == 'folder':
        path = QPainterPath(QPointF(3, 20))
        path.lineTo(3, 5)
        path.lineTo(10, 5)
        path.lineTo(12, 8)
        path.lineTo(21, 8)
        path.lineTo(18, 20)
        path.closeSubpath()
        p.drawPath(path)
        line(3, 20, 7, 11)
        line(7, 11, 20, 11)
    elif name in ('clipboard', 'copy'):
        p.drawRoundedRect(QRectF(6, 4, 15, 17), 1.5, 1.5)
        if name == 'clipboard':
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(10, 2, 7, 4), 1, 1)
        else:
            line(3, 17, 3, 1)
            line(3, 1, 16, 1)
    elif name == 'import':
        line(3, 16, 3, 21)
        line(3, 21, 21, 21)
        line(21, 21, 21, 16)
        line(12, 3, 12, 16)
        line(7, 8, 12, 3)
        line(12, 3, 17, 8)
    elif name == 'refresh':
        p.drawArc(QRectF(4, 4, 16, 16), 35 * 16, 290 * 16)
        line(20, 3, 20, 9)
        line(20, 9, 14, 9)
    elif name == 'tabs':
        p.drawRoundedRect(QRectF(3, 5, 18, 15), 1, 1)
        line(3, 9, 21, 9)
    elif name == 'search':
        p.drawEllipse(QRectF(3, 3, 12, 12))
        line(13, 13, 21, 21)
    elif name == 'trash':
        line(4, 6, 20, 6)
        line(9, 3, 15, 3)
        p.drawRoundedRect(QRectF(6, 6, 12, 15), 1, 1)
        line(10, 10, 10, 17)
        line(14, 10, 14, 17)
    elif name == 'text':
        for y, w in ((5, 17), (10, 17), (15, 17), (20, 11)):
            line(4, y, w + 3, y)
    elif name == 'link':
        p.drawArc(QRectF(3, 9, 13, 10), 45 * 16, 270 * 16)
        p.drawArc(QRectF(9, 3, 13, 10), 225 * 16, 270 * 16)
        line(8, 16, 16, 8)
    elif name in ('previous', 'next'):
        x, sign = (14, -1) if name == 'previous' else (10, 1)
        line(x, 5, x + sign * 7, 12)
        line(x + sign * 7, 12, x, 19)
    elif name == 'close':
        line(6, 6, 18, 18)
        line(18, 6, 6, 18)
    elif name == 'expand':
        for x, y, sx, sy in ((3, 3, 1, 1), (21, 3, -1, 1), (3, 21, 1, -1), (21, 21, -1, -1)):
            line(x, y, x + sx * 6, y)
            line(x, y, x, y + sy * 6)
    elif name == 'pin':
        path = QPainterPath(QPointF(9, 3))
        path.lineTo(20, 14)
        path.lineTo(17, 14)
        path.lineTo(14, 17)
        path.lineTo(7, 10)
        path.lineTo(10, 7)
        path.closeSubpath()
        p.drawPath(path)
        line(10, 14, 3, 21)
        line(6, 9, 15, 18)
    elif name == 'dot':
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(7, 7, 10, 10))
    else:
        p.drawRoundedRect(QRectF(4, 4, 16, 16), 3, 3)
    p.end()
    return QIcon(pix)

class ThumbnailCache:
    """Only decode visible cards, retaining at most 24 thumbnails (~15 MiB)."""

    def __init__(self, directory, capacity=24):
        self.directory, self.capacity = (directory, capacity)
        self.items = OrderedDict()

    def get(self, key):
        pix = self.items.pop(key, None)
        if pix is None:
            pix = QPixmap(str(self.directory / f'{key}.png'))
        self.items[key] = pix
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
        return pix

    def clear(self):
        self.items.clear()

def retention_badge(row, days, now=None):
    colors = theme()
    if row['favorite']:
        return (tr('永久收藏'), colors['accent_soft'], colors['accent_text'])
    if not days:
        return (tr('永久保留'), colors['badge_bg'], colors['badge_text'])
    captured = datetime.fromisoformat(row['captured_at'])
    remaining = math.ceil(days - ((now or datetime.now(timezone.utc)) - captured).total_seconds() / 86400)
    return (tr('剩余 {remaining} 天', remaining=remaining) if remaining > 0 else tr('即将清理'), colors['warning_bg'], colors['warning'])

def card_topics(row):
    """Keep topic badges independent of capture type and OCR status."""
    tags = row.get('tags', [])[:2]
    titles = [tr(ALL_TOPIC_LABELS.get(key, key)) for key in tags]
    status = row.get('classification_status', 'pending')
    if status == 'pending':
        titles.append(tr('待分类'))
    elif status == 'error':
        titles.append(tr('分类失败'))
    return titles or [tr('待整理')]

class CardDelegate(QStyledItemDelegate):

    def __init__(self, host):
        super().__init__(host.gallery)
        self.host = host
        self.cache = ThumbnailCache(host.vault.thumbs)

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        if not row:
            return
        colors = theme()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(2, 2, -10, -10)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setPen(QPen(QColor(colors['accent'] if selected else colors['border_hover'] if hover else colors['border']), 1.6 if selected else 1))
        painter.setBrush(QColor(colors['card']))
        painter.drawRoundedRect(rect, 11, 11)
        image_rect = rect.adjusted(8, 8, -8, -102)
        painter.setPen(Qt.PenStyle.NoPen)
        item_type = row.get('item_type', 'image')
        is_image = item_type == 'image'
        surface = colors['image_preview'] if is_image else colors['link_preview'] if item_type == 'link' else colors['preview']
        painter.setBrush(QColor(surface))
        painter.drawRoundedRect(image_rect, 7, 7)
        clip = QPainterPath()
        clip.addRoundedRect(image_rect, 7, 7)
        painter.save()
        painter.setClipPath(clip)
        if is_image:
            pix = self.cache.get(row['id'])
            if not pix.isNull():
                scaled = pix.scaled(image_rect.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(QPointF(image_rect.center().x() - scaled.width() / 2, image_rect.center().y() - scaled.height() / 2), scaled)
            else:
                glyph('image', colors['muted'], 32).paint(painter, int(image_rect.center().x() - 16), int(image_rect.center().y() - 16), 32, 32)
        else:
            content = row.get('content', '')
            code = item_type == 'text' and is_code_content(content)
            font = QFont('Consolas' if code else 'Microsoft YaHei UI')
            font.setPixelSize(12 if code else 13)
            painter.setFont(font)
            painter.setPen(QColor(colors['secondary']))
            content_rect = image_rect.adjusted(14, 42, -14, -12)
            if item_type == 'link':
                host = QUrl(content.strip()).host()
                font.setPixelSize(14)
                font.setWeight(QFont.Weight.DemiBold)
                painter.setFont(font)
                painter.setPen(QColor(colors['link_text']))
                painter.drawText(QRectF(content_rect.x(), content_rect.y(), content_rect.width(), 25), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, painter.fontMetrics().elidedText(host, Qt.TextElideMode.ElideRight, int(content_rect.width())))
                content_rect.adjust(0, 31, 0, 0)
                font.setPixelSize(12)
                font.setWeight(QFont.Weight.Normal)
                painter.setFont(font)
                painter.setPen(QColor(colors['secondary']))
            painter.drawText(content_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, content[:2000])
            fade_rect = QRectF(image_rect.x(), image_rect.bottom() - 24, image_rect.width(), 24)
            fade = QLinearGradient(fade_rect.topLeft(), fade_rect.bottomLeft())
            transparent = QColor(surface)
            transparent.setAlpha(0)
            fade.setColorAt(0, transparent)
            fade.setColorAt(1, QColor(surface))
            painter.fillRect(fade_rect, fade)
        painter.restore()
        font = QFont('Microsoft YaHei UI')
        font.setPixelSize(10)
        painter.setFont(font)
        type_width = self._badge(painter, image_rect.x() + 7, image_rect.y() + 7, tr('图片') if is_image else tr('链接') if item_type == 'link' else tr('文字'), colors['badge_bg'], colors['badge_text'])
        similar = getattr(self.host, 'similar_sizes', {}).get(row['id'])
        if similar:
            self._badge(painter, image_rect.x() + 12 + type_width, image_rect.y() + 7, tr('相似 ×{similar}', similar=similar), colors['accent_soft'], colors['accent_text'])
        if selected:
            painter.setBrush(QColor(colors['accent']))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(image_rect.right() - 14, image_rect.y() + 16), 9, 9)
            painter.setPen(QPen(QColor(colors['on_accent']), 1.5))
            x, y = (image_rect.right() - 14, image_rect.y() + 16)
            painter.drawLine(QPointF(x - 4, y), QPointF(x - 1, y + 3))
            painter.drawLine(QPointF(x - 1, y + 3), QPointF(x + 5, y - 3))
        if is_image and row['ocr_status'] in ('pending', 'error'):
            failed = row['ocr_status'] == 'error'
            self._badge(painter, image_rect.x() + 7, image_rect.bottom() - 25, tr('识别失败') if failed else tr('识别中'), colors['danger_bg'] if failed else colors['accent_soft'], colors['danger'] if failed else colors['accent_text'])
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(colors['text']))
        title_rect = QRectF(rect.x() + 13, rect.bottom() - 92, rect.width() - 26 - (22 if row['favorite'] else 0), 21)
        display_title = QUrl(row.get('content', '').strip()).host() or tr('网页链接') if item_type == 'link' else row['title']
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(display_title, Qt.TextElideMode.ElideRight, int(title_rect.width())))
        if row['favorite']:
            glyph('star', colors['favorite'], 15).paint(painter, int(rect.right() - 28), int(title_rect.y() + 3), 15, 15)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor(colors['secondary']))
        snippet = ' '.join(row['ocr_text'].split()) or {'pending': tr('等待本地文字识别…'), 'error': tr('识别失败，可重新识别'), 'done': tr('未发现可读文字')}[row['ocr_status']] if is_image else ' '.join(row.get('content', '').split())
        snippet_rect = QRectF(rect.x() + 13, rect.bottom() - 69, rect.width() - 26, 18)
        painter.drawText(snippet_rect, Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(snippet, Qt.TextElideMode.ElideRight, int(snippet_rect.width())))
        font.setPixelSize(10)
        painter.setFont(font)
        x = rect.x() + 13
        for title in card_topics(row):
            width = painter.fontMetrics().horizontalAdvance(title) + 12
            if x + width > rect.right() - 10:
                break
            failed = title == tr('分类失败')
            x += self._badge(painter, x, rect.bottom() - 46, title, colors['danger_bg'] if failed else colors['accent_soft'], colors['danger'] if failed else colors['accent_text']) + 5
        stamp = datetime.fromisoformat(row['captured_at']).astimezone().strftime('%m/%d %H:%M')
        painter.setPen(QColor(colors['muted']))
        painter.drawText(QRectF(rect.x() + 13, rect.bottom() - 27, 88, 17), Qt.AlignmentFlag.AlignVCenter, stamp)
        if row.get('tags_manual'):
            painter.drawText(QRectF(rect.right() - 60, rect.bottom() - 27, 47, 17), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, tr('手动'))
        days = getattr(self.host, 'retention_days', 7)
        if days and (not row['favorite']):
            captured = datetime.fromisoformat(row['captured_at'])
            remaining = math.ceil(days - (datetime.now(timezone.utc) - captured).total_seconds() / 86400)
            if remaining <= 2:
                self._badge(painter, image_rect.right() - 69, image_rect.bottom() - 25, tr('即将清理') if remaining <= 0 else tr('剩余 {remaining} 天', remaining=remaining), colors['warning_bg'], colors['warning'])
        painter.restore()

    @staticmethod
    def _badge(p, x, y, text, bg, fg):
        width = p.fontMetrics().horizontalAdvance(text) + 12
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(QRectF(x, y, width, 18), 4, 4)
        p.setPen(QColor(fg))
        p.drawText(QRectF(x, y, width, 18), Qt.AlignmentFlag.AlignCenter, text)
        return width

class GalleryView(QListWidget):
    geometry_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setSpacing(0)
        self.card_size = QSize(250, 270)
        self.setGridSize(self.card_size)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        available = max(216, self.viewport().width() - 20)
        columns = max(1, min(5, available // 238))
        width = min(352, available // columns)
        size = QSize(width, round((width - 28) * 0.64) + 114)
        if size != self.card_size:
            self.card_size = size
            self.setGridSize(size)
            for i in range(self.count()):
                self.item(i).setSizeHint(size)
            self.geometry_changed.emit()
