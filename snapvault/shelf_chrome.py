"""Quiet glass-shelf chrome and file rows, independent of the library theme.

Painting consumes only the item's metadata and an already-loaded QIcon. No file
contents or thumbnails are read here, so a repaint cannot block on a document.
"""
from __future__ import annotations
from .i18n import tr

from datetime import datetime
from pathlib import PureWindowsPath
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QPainter,
                          QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QStyle,
                              QStyledItemDelegate, QWidget)

from .branding import APP_NAME, app_icon, asset_path


def shelf_colors(dark=True):
    """A fresh semantic palette; the shelf deliberately owns its appearance."""
    if dark:
        return dict(dark=True, tint="#202020", surface="#2B2B2B", input="#252525",
                    text="#FFFFFF", secondary="#C5C5C5", muted="#B5B5B5",
                    accent="#4CC2FF", border="#484848", hover="#383838",
                    selected="#2D3B45", selected_border="#4CC2FF",
                    pressed="#3C3C3C", status="#C5C5C5", danger="#F1B9B9",
                    scroll="#666666", tile_border="#474747")
    return dict(dark=False, tint="#F3F3F3", surface="#FFFFFF", input="#F9F9F9",
                text="#1B1B1B", secondary="#5D5D5D", muted="#626262",
                accent="#0067C0", border="#D2D2D2", hover="#F0F0F0",
                selected="#E5EFF8", selected_border="#0067C0",
                pressed="#E8E8E8", status="#3D3D3D", danger="#A14444",
                scroll="#9A9A9A", tile_border="#D2D2D2")


def shelf_style(dark=True, native_glass=False):
    """Style only the shelf; translucent material never fades its foreground."""
    p = shelf_colors(dark)
    rgb = "32, 32, 32" if dark else "243, 243, 243"
    alpha = (80 if dark else 128) if native_glass else 255
    root_background = "transparent" if native_glass else p["tint"]
    chevron = asset_path("chevron-dark.png" if dark else "chevron-light.png").as_posix()
    return f'''\nQWidget#quickPaste, QWidget#quickPaste QWidget {{\n    font-family: "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "微软雅黑";\n    color: {p['text']}; font-size: 14px;\n}}\nQWidget#quickPaste {{ background: {root_background}; }}\nQFrame#quickFrame {{\n    background: rgba({rgb}, {alpha}); border: 1px solid {p['border']}; border-radius: 16px;\n}}\nQWidget#shelfHeader, QWidget#quickHeader {{ background: transparent; border: none; }}\nQLabel#quickTitle {{ color: {p['text']}; font-size: 15px; font-weight: 600; background: transparent; }}\nQLabel#quickSummary, QLabel#shelfSummary {{ color: {p['secondary']}; font-size: 12px; background: transparent; }}\nQLabel#quickHeaderIcon {{ background: transparent; }}\nQLineEdit#quickSearch {{\n    color: {p['text']}; background: {p['input']}; border: 1px solid {p['border']};\n    border-radius: 9px; padding: 10px 12px; font-size: 14px; min-height: 20px;\n    selection-background-color: {p['selected']}; selection-color: {p['text']};\n}}\nQLineEdit#quickSearch:focus {{ border: 1px solid {p['accent']}; }}\nQListWidget#quickResults, QListWidget#recentResults {{\n    color: {p['text']}; background: transparent; border: none; outline: none; padding: 2px 0;\n}}\nQListWidget#quickResults::item {{\n    color: {p['text']}; background: transparent; border: none; border-radius: 9px;\n    padding: 6px 9px; margin: 1px 0;\n}}\nQListWidget#quickResults::item:hover:!selected {{ background: {p['hover']}; }}\nQListWidget#quickResults::item:selected {{ background: {p['selected']}; color: {p['text']}; }}\nQListWidget#recentResults::item {{ border: none; padding: 0; margin: 0; background: transparent; }}\nQListWidget#recentResults::item:hover, QListWidget#recentResults::item:selected {{ background: transparent; }}\nQLabel#quickHint {{ color: {p['muted']}; font-size: 12px; background: transparent; padding-top: 2px; }}\nQLabel#quickStatus {{ color: {p['status']}; font-size: 12px; background: transparent; }}\nQLabel#quickEmpty {{ color: {p['secondary']}; font-size: 14px; background: transparent; padding: 24px 14px; }}\nQWidget#recentLockCard {{ background: transparent; }}\nQWidget#recentLockCard QLabel#recentLockTitle {{ font-size: 20px; font-weight: 600; background: transparent; }}\nQWidget#recentLockCard QLabel#recentLockMessage, QWidget#recentLockCard QLabel#recentLockBoundary {{ color: {p['secondary']}; font-size: 12px; background: transparent; }}\nQWidget#recentLockCard QPushButton#quickTool {{ min-height: 30px; }}\nQPushButton#quickTool {{\n    color: {p['secondary']}; background: transparent; border: 1px solid {p['border']};\n    border-radius: 7px; padding: 6px 9px; min-height: 19px; font-size: 12px;\n}}\nQPushButton#quickTool:hover {{ background: {p['hover']}; color: {p['text']}; }}\nQPushButton#quickTool:pressed {{ background: {p['pressed']}; }}\nQPushButton#quickTool:checked {{ background: {p['selected']}; color: {p['text']}; border-color: {p['selected_border']}; }}\nQPushButton#quickTool:disabled {{ color: {p['muted']}; border-color: transparent; background: transparent; }}\nQPushButton#quickTool:focus {{ border-color: {p['accent']}; }}\nQPushButton#quickHeaderButton, QPushButton#quickClose {{\n    color: {p['secondary']}; background: transparent; border: 1px solid transparent;\n    border-radius: 7px; padding: 0; min-width: 30px; min-height: 30px;\n}}\nQPushButton#quickHeaderButton:hover, QPushButton#quickClose:hover {{ background: {p['hover']}; }}\nQPushButton#quickHeaderButton:pressed, QPushButton#quickClose:pressed {{ background: {p['pressed']}; }}\nQPushButton#quickHeaderButton:checked {{ background: {p['selected']}; border-color: {p['selected_border']}; }}\nQPushButton#quickHeaderButton:focus, QPushButton#quickClose:focus {{ border-color: {p['accent']}; }}\nQTabBar#quickTabs {{ background: transparent; border: none; }}\nQTabBar#quickTabs::tab {{\n    color: {p['secondary']}; background: transparent; border: none;\n    border-bottom: 2px solid transparent; padding: 7px 14px; min-height: 20px; font-size: 14px;\n}}\nQTabBar#quickTabs::tab:hover {{ color: {p['text']}; background: {p['hover']}; border-radius: 6px; }}\nQTabBar#quickTabs::tab:selected {{ color: {p['text']}; border-bottom-color: {p['accent']}; }}\nQComboBox#sourceFilter {{\n    color: {p['secondary']}; background: transparent; border: 1px solid {p['border']};\n    border-radius: 6px; padding: 4px 25px 4px 9px; min-height: 18px; font-size: 12px;\n}}\nQComboBox#sourceFilter:hover, QComboBox#sourceFilter:focus {{ border-color: {p['accent']}; }}\nQComboBox#sourceFilter::drop-down {{ border: none; width: 23px; }}\nQComboBox#sourceFilter::down-arrow {{ image: url("{chevron}"); width: 12px; height: 8px; }}\nQComboBox#sourceFilter QAbstractItemView {{ background: {p['surface']}; color: {p['text']}; selection-background-color: {p['selected']}; border: 1px solid {p['border']}; }}\nQScrollBar:vertical {{ background: transparent; width: 6px; margin: 3px 0; border: none; }}\nQScrollBar::handle:vertical {{ background: {p['scroll']}; border-radius: 3px; min-height: 28px; }}\nQScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}\nQScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}\nQToolTip {{ color: {p['text']}; background: {p['surface']}; border: 1px solid {p['border']}; padding: 5px 8px; }}\nQMenu {{ color: {p['text']}; background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 8px; padding: 5px; }}\nQMenu::item {{ padding: 7px 22px 7px 12px; border-radius: 5px; }}\nQMenu::item:selected {{ color: {p['text']}; background: {p['selected']}; }}\nQMenu::item:disabled {{ color: {p['muted']}; }}\nQMenu::separator {{ background: {p['border']}; height: 1px; margin: 4px 8px; }}\n'''


def _paint_pin(painter, rect, color):
    painter.save()
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 18, rect.height() / 18)
    painter.setPen(QPen(color, 1.45, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    pin = QPainterPath(QPointF(5.5, 3.0))
    pin.lineTo(12.5, 3.0)
    pin.lineTo(11.5, 8.0)
    pin.lineTo(14, 11)
    pin.lineTo(4, 11)
    pin.lineTo(6.5, 8.0)
    pin.closeSubpath()
    painter.drawPath(pin)
    painter.drawLine(QPointF(9, 11), QPointF(9, 16))
    painter.restore()


def _control_icon(kind, color, size=18):
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    ink = QColor(color)
    if kind == "pin":
        _paint_pin(painter, QRectF(0, 0, size, size), ink)
    else:
        painter.setPen(QPen(ink, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if kind == "collapse":
            painter.drawLine(QPointF(4, 5), QPointF(9, 10))
            painter.drawLine(QPointF(9, 10), QPointF(14, 5))
            painter.drawLine(QPointF(5, 14), QPointF(13, 14))
        elif kind == "lock":
            painter.drawRoundedRect(QRectF(4, 8, 10, 8), 1.6, 1.6)
            painter.drawArc(QRectF(6, 2, 6, 10), 0, 180 * 16)
            painter.drawLine(QPointF(9, 11), QPointF(9, 13))
        elif kind == "more":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
            for x in (4, 9, 14):
                painter.drawEllipse(QPointF(x, 9), 1.2, 1.2)
    painter.end()
    return QIcon(pixmap)


class DragHeader(QWidget):
    """Window-drag surface; its native buttons keep their own input handling."""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._pressed = False
        self.setObjectName("shelfHeader")
        self.setMinimumHeight(40)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.brand_icon = QLabel()
        self.brand_icon.setObjectName("quickHeaderIcon")
        self.brand_icon.setFixedSize(24, 24)
        self.brand_icon.setPixmap(app_icon().pixmap(24, 24))
        self.brand_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.brand_icon)
        self.title = QLabel(APP_NAME)
        self.title.setObjectName("quickTitle")
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.title)
        layout.addStretch(1)
        self.keep_open = self._button(tr('保持展开'), "quickHeaderButton")
        self.keep_open.setCheckable(True)
        self.keep_open.toggled.connect(self._keep_changed)
        layout.addWidget(self.keep_open)
        self.lock_button = self._button(tr('锁定最近文件（Ctrl+L）'), "quickHeaderButton")
        self.lock_button.setCheckable(True)
        self.lock_button.clicked.connect(lambda _checked=False: panel.toggle_files_lock())
        layout.addWidget(self.lock_button)
        self.more_button = self._button(tr('更多操作'), "quickHeaderButton")
        self.more_button.clicked.connect(lambda _checked=False: panel.source_menu())
        layout.addWidget(self.more_button)
        self.collapse_button = self._button(tr('收成悬浮球'), "quickClose")
        self.collapse_button.clicked.connect(lambda _checked=False: panel.collapse())
        layout.addWidget(self.collapse_button)
        self.set_dark(getattr(panel, "shelf_dark", True))

    def _button(self, label, object_name):
        button = QPushButton()
        button.setObjectName(object_name)
        button.setFixedSize(32, 32)
        button.setIconSize(QSize(18, 18))
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return button

    def _keep_changed(self, checked):
        self.keep_open.setToolTip(tr('取消保持展开') if checked else tr('保持展开'))

    def set_dark(self, dark=True):
        p = shelf_colors(dark)
        self.keep_open.setIcon(_control_icon("pin", p["text"]))
        self.lock_button.setIcon(_control_icon("lock", p["text"]))
        self.more_button.setIcon(_control_icon("more", p["text"]))
        self.collapse_button.setIcon(_control_icon("collapse", p["text"]))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.panel.begin_window_drag(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed and event.buttons() & Qt.MouseButton.LeftButton:
            self.panel.move_window_drag(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pressed and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.panel.end_window_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _relative_time(value, now=None):
    try:
        stamp = float(value)
        elapsed = max(0, (time.time() if now is None else now) - stamp)
        when = datetime.fromtimestamp(stamp)
        today = datetime.fromtimestamp(time.time() if now is None else now)
    except (ValueError, TypeError, OverflowError, OSError):
        return ""
    if elapsed < 60:
        return tr('刚刚')
    if elapsed < 3600:
        return tr('{v0}分钟前', v0=int(elapsed // 60))
    if when.date() == today.date():
        return when.strftime(tr('今天 %H:%M'))
    return when.strftime("%m-%d" if when.year == today.year else "%Y-%m-%d")


_IMAGE_TYPES = {"PNG", "JPG", "JPEG", "WEBP", "GIF", "BMP", "TIF", "TIFF", "ICO"}
_TYPE_GROUPS = {
    "PDF": ("PDF", "red"),
    "ZIP": ("ZIP", "gold"), "RAR": ("RAR", "gold"), "7Z": ("7Z", "gold"),
    "TAR": ("TAR", "gold"), "GZ": ("GZ", "gold"),
    "DOC": ("DOC", "blue"), "DOCX": ("DOC", "blue"), "ODT": ("DOC", "blue"),
    "XLS": ("XLS", "green"), "XLSX": ("XLS", "green"), "CSV": ("CSV", "green"),
    "PPT": ("PPT", "orange"), "PPTX": ("PPT", "orange"),
    "TXT": ("TXT", "gray"), "MD": ("MD", "gray"),
    "PY": ("PY", "violet"), "JS": ("JS", "violet"), "JSON": ("JSON", "violet"),
    "MP4": ("MP4", "violet"), "MOV": ("MOV", "violet"), "MP3": ("MP3", "violet"),
}
_TILES = {
    True: {
        "red": ("#45313C", "#F1B8C0"), "gold": ("#433B2C", "#E8CF9C"),
        "blue": ("#294055", "#A5D2F5"), "green": ("#2A423B", "#ACD9C5"),
        "orange": ("#493A31", "#EDC3A8"), "violet": ("#3A3651", "#C9BEEA"),
        "gray": ("#354152", "#C4D0DF"),
    },
    False: {
        "red": ("#F8E9ED", "#964C60"), "gold": ("#F4EEDB", "#7C642C"),
        "blue": ("#E1EDF8", "#35678F"), "green": ("#E2EFE8", "#3B6D58"),
        "orange": ("#F6E9E0", "#8C5B3B"), "violet": ("#ECE8F7", "#64558E"),
        "gray": ("#E3EAF2", "#52667D"),
    },
}


class RecentFileDelegate(QStyledItemDelegate):
    """Continuous file rows with original previews and quiet file-type tiles."""

    def __init__(self, parent=None, dark=True):
        super().__init__(parent)
        self.dark = bool(dark)

    def set_dark(self, dark=True):
        self.dark = bool(dark)
        parent = self.parent()
        if hasattr(parent, "viewport"):
            parent.viewport().update()

    def sizeHint(self, option, index):
        return QSize(0, 64)

    def paint(self, painter, option, index):
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return super().paint(painter, option, index)
        p = shelf_colors(self.dark)
        bounds = QRectF(option.rect).adjusted(1, 1, -1, -1)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected or hovered:
            painter.setBrush(QColor(p["selected"] if selected else p["hover"]))
            painter.drawRoundedRect(bounds, 9, 9)
        if focused:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(p["accent"]), 1))
            painter.drawRoundedRect(bounds.adjusted(.5, .5, -.5, -.5), 9, 9)
        icon_rect = QRectF(bounds.left() + 9, bounds.center().y() - 24, 48, 48)
        suffix = PureWindowsPath(str(entry.get("path", ""))).suffix.lstrip(".").upper()
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QPixmap):
            icon = QIcon(icon)
        if entry.get("vault_id") and entry.get("thumbnail") and isinstance(icon, QIcon) and not icon.isNull():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p["surface"]))
            painter.drawRoundedRect(icon_rect, 7, 7)
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(icon_rect, 7, 7)
            painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            icon.paint(painter, icon_rect.toRect(), Qt.AlignmentFlag.AlignCenter)
            painter.restore()
        else:
            self._file_tile(painter, icon_rect, suffix)

        title_font = QFont(option.font)
        title_font.setPixelSize(14)
        title_font.setWeight(QFont.Weight.Medium)
        meta_font = QFont(option.font)
        meta_font.setPixelSize(12)
        meta_font.setWeight(QFont.Weight.Normal)
        title_metrics, meta_metrics = QFontMetrics(title_font), QFontMetrics(meta_font)
        left, right = icon_rect.right() + 12, bounds.right() - 11
        top = bounds.center().y() - 21
        stamp = _relative_time(entry.get("seen_at"))
        stamp_width = meta_metrics.horizontalAdvance(stamp) if stamp else 0
        pin_width = 20 if entry.get("pinned") else 0
        title_right = right - (stamp_width + 10 if stamp_width else 0) - pin_width
        title_rect = QRectF(left, top, max(1, title_right - left), 23)
        name = str(entry.get("name") or tr('未命名文件')).replace("\n", " ").replace("\r", " ")
        painter.setFont(title_font)
        painter.setPen(QColor(p["text"] if entry.get("exists", True) else p["secondary"]))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         title_metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, int(title_rect.width())))
        painter.setFont(meta_font)
        painter.setPen(QColor(p["secondary"]))
        if stamp:
            painter.drawText(QRectF(right - stamp_width, top + 1, stamp_width + 1, 21),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, stamp)
        if entry.get("pinned"):
            _paint_pin(painter, QRectF(title_right + 1, top + 4, 15, 15), QColor(p["accent"]))
        if not entry.get("exists", True):
            detail = tr('已移动或删除')
        else:
            detail = str(PureWindowsPath(str(entry.get("path", ""))).parent)
        meta_rect = QRectF(left, top + 24, max(1, right - left), 18)
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         meta_metrics.elidedText(detail, Qt.TextElideMode.ElideMiddle, int(meta_rect.width())))
        painter.restore()

    def _file_tile(self, painter, bounds, suffix):
        label, group = _TYPE_GROUPS.get(suffix, (("IMG" if suffix in _IMAGE_TYPES else suffix[:4] or "FILE"), "gray"))
        background, ink = _TILES[self.dark][group]
        rect = bounds.adjusted(2, 2, -2, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(rect, 8, 8)
        painter.setPen(QPen(QColor(ink), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        fold = QPainterPath(QPointF(rect.right() - 12, rect.top() + 5))
        fold.lineTo(rect.right() - 12, rect.top() + 12)
        fold.lineTo(rect.right() - 5, rect.top() + 12)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(fold)
        font = QFont("Segoe UI")
        font.setPixelSize(11 if len(label) > 3 else 12)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(ink))
        painter.drawText(rect.adjusted(2, 8, -2, 0), Qt.AlignmentFlag.AlignCenter, label)
