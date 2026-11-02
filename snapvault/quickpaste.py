"""Shared hotkey palette for saved content and recent local files."""
from __future__ import annotations
from .i18n import tr

from datetime import datetime

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QCursor, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QVBoxLayout, QWidget)

from . import hotkey
from .branding import app_name
from .theme import quick_style

TYPE_NAMES = {"image": "图片", "text": "文字", "link": "链接"}


def _summary(row):
    kind = row.get("item_type", "image")
    if kind == "link":
        url = row.get("content", "").strip()
        return QUrl(url).host() or tr('链接'), url
    if kind == "text":
        lines = [line.strip() for line in row.get("content", "").splitlines() if line.strip()]
        return (lines[0] if lines else tr('文本片段')), " ".join(lines[1:3])
    text = " ".join(row.get("ocr_text", "").split())
    status = tr({"pending": "等待本地识别…", "error": "识别失败"}.get(row.get("ocr_status"), '未发现可读文字'))
    return row.get("title") or tr('截图'), text or status


class QuickPastePanel(QWidget):
    """A small top-most palette. Enter pastes into the window that was active."""

    LIMIT = 40

    def __init__(self, host):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.host = host
        self.target = 0
        self.setObjectName("quickPaste")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(tr('{v0} · 随手面板', v0=app_name()))
        self.resize(600, 470)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setObjectName("quickFrame")
        outer.addWidget(frame)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("quickSearch")
        self.search.setPlaceholderText(tr('搜索图片文字、原文或链接…'))
        self.search.setClearButtonEnabled(True)
        self.search.installEventFilter(self)
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.setObjectName("quickResults")
        self.results.setIconSize(QSize(44, 44))
        self.results.setUniformItemSizes(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.itemActivated.connect(lambda _item: self.accept_current())
        self.results.installEventFilter(self)
        layout.addWidget(self.results, 1)
        self.empty = QLabel("")
        self.empty.setObjectName("quickEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty, 1)
        self.hint = QLabel("")
        self.hint.setObjectName("quickHint")
        layout.addWidget(self.hint)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.refresh_theme()

    def refresh_theme(self):
        self.setStyleSheet(quick_style())

    def _update_hint(self):
        action = tr('Enter 粘贴到原窗口') if self.host.quick_autopaste else tr('Enter 复制')
        self.hint.setText(tr('↑↓ 选择 · {v0} · Ctrl+Enter 仅复制 · Ctrl+P 贴图 · Esc 关闭', v0=action))

    def open(self):
        """Remember the active window, then show the palette on the cursor's screen."""
        own = int(self.winId())
        current = hotkey.foreground_window()
        self.target = current if current and current != own else 0
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center().x() - self.width() // 2, area.top() + max(40, area.height() // 6))
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._update_hint()
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def refresh(self):
        text = self.search.text().strip()
        rows = self.host.vault.query(text, limit=self.LIMIT, sort="relevance" if text else "newest")
        self.results.clear()
        for row in rows:
            title, detail = _summary(row)
            stamp = datetime.fromisoformat(row["captured_at"]).astimezone().strftime("%m/%d %H:%M")
            kind = tr(TYPE_NAMES.get(row.get("item_type", "image"), '内容'))
            item = QListWidgetItem(f"{title}\n{kind} · {stamp}" + (f" · {detail}" if detail else ""))
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            item.setToolTip(title)
            item.setSizeHint(QSize(0, 56))
            if row.get("item_type", "image") == "image":
                pixmap = QPixmap(str(self.host.vault.thumbs / f"{row['id']}.png"))
                if not pixmap.isNull():
                    item.setIcon(QIcon(pixmap.scaled(88, 88, Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation)))
            self.results.addItem(item)
        if rows:
            self.results.setCurrentRow(0)
        self.results.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        self.empty.setText(tr('没有匹配的内容。图片文字需要等本地识别完成后才能搜索。') if text else tr('内容库还是空的。'))

    def current_id(self):
        item = self.results.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def accept_current(self, paste=True):
        key = self.current_id()
        row = self.host.vault.get(key) if key else None
        if row is None:
            return
        if not self.host.copy_row(row):
            return
        target = self.target
        self.hide()
        if paste and self.host.quick_autopaste and target:
            # Give the target window a moment to take focus before Ctrl+V.
            if hotkey.focus_window(target):
                QTimer.singleShot(90, hotkey.send_paste)

    def pin_current(self):
        key = self.current_id()
        row = self.host.vault.get(key) if key else None
        if row and row.get("item_type", "image") == "image":
            self.hide()
            self.host.pin_row(row)

    def _move(self, step):
        count = self.results.count()
        if count:
            self.results.setCurrentRow(max(0, min(count - 1, self.results.currentRow() + step)))

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            key, modifiers = event.key(), event.modifiers()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._move(1 if key == Qt.Key.Key_Down else -1)
                return True
            if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self._move(6 if key == Qt.Key.Key_PageDown else -6)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.accept_current(paste=not modifiers & Qt.KeyboardModifier.ControlModifier)
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
            if key == Qt.Key.Key_P and modifiers & Qt.KeyboardModifier.ControlModifier:
                self.pin_current()
                return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        # Clicking anywhere else dismisses the palette, like a menu.
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            QTimer.singleShot(0, self._hide_if_inactive)
        super().changeEvent(event)

    def _hide_if_inactive(self):
        if not self.isActiveWindow():
            self.hide()
