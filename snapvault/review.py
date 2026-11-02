"""Keyboard review of automatic topics: one item at a time, number keys tag it."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QStackedWidget, QTextEdit, QVBoxLayout)

from .classification import ALL_TOPIC_LABELS, TOPIC_LABELS
from .glass import configure_window_glass, refresh_window_glass
from .theme import native_titlebar, review_style, theme

SCOPES = (("unknown", "待整理（没有主题的内容）"), ("auto", "全部自动标签（逐条确认）"))


def _label(text="", name=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if name:
        widget.setObjectName(name)
    return widget


def _button(text, callback, name=None):
    widget = QPushButton(text)
    widget.setAutoDefault(False)
    widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setAccessibleName(text.replace("← ", "").replace(" →", ""))
    widget.clicked.connect(callback)
    if name:
        widget.setObjectName(name)
    return widget


class ReviewDialog(QDialog):
    """Saving a decision makes it a manual tag, exactly like the tag editor.

    Confirming unchanged automatic tags is also recorded, so the agreement
    figure is not biased toward the items that were wrong.
    """

    def __init__(self, host, scope="unknown"):
        super().__init__(host)
        configure_window_glass(self)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.host = host
        self.vault = host.vault
        self.queue, self.index, self.saved = [], 0, 0
        self.row = None
        self.setObjectName("reviewDialog")
        self.setWindowTitle("整理主题标签 · 捕风捉影")
        self.resize(980, 660)
        self.setMinimumSize(780, 520)
        self._build()
        for number, key in enumerate(TOPIC_LABELS, start=1):
            QShortcut(QKeySequence(str(number)), self, activated=lambda k=key: self.toggle(k))
        QShortcut(QKeySequence("0"), self, activated=self.clear_and_save)
        QShortcut(QKeySequence("Return"), self, activated=self.save_and_next)
        QShortcut(QKeySequence("Enter"), self, activated=self.save_and_next)
        QShortcut(QKeySequence("Right"), self, activated=lambda: self.step(1))
        QShortcut(QKeySequence("Space"), self, activated=lambda: self.step(1))
        QShortcut(QKeySequence("Left"), self, activated=lambda: self.step(-1))
        self.scope.setCurrentIndex(max(0, self.scope.findData(scope)))
        self.load_queue()
        self.refresh_theme()

    def refresh_theme(self):
        palette = theme()
        self.setStyleSheet(review_style())
        native_titlebar(self)
        refresh_window_glass(self, palette["dark"], enabled=palette["glass"])

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_theme()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(_label("整理主题标签", "title"))
        header.addStretch()
        self.scope = QComboBox()
        self.scope.setObjectName("reviewScope")
        for key, title in SCOPES:
            self.scope.addItem(title, key)
        self.scope.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.scope.currentIndexChanged.connect(lambda *_: self.load_queue())
        header.addWidget(self.scope)
        self.progress = _label("", "reviewProgress")
        header.addWidget(self.progress)
        layout.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(14)
        preview = QFrame()
        preview.setObjectName("reviewPreview")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(10, 10, 10, 10)
        self.title = _label("", "selectionTitle", True)
        preview_layout.addWidget(self.title)
        self.pages = QStackedWidget()
        self.pages.setObjectName("reviewPages")
        self.image = _label("", "reviewImage")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(320, 240)
        self.text = QTextEdit()
        self.text.setObjectName("reviewText")
        self.text.setReadOnly(True)
        # Keep keyboard focus on the dialog so arrows and digits stay shortcuts.
        self.text.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pages.addWidget(self.image)
        self.pages.addWidget(self.text)
        preview_layout.addWidget(self.pages, 1)
        self.ocr = QTextEdit()
        self.ocr.setObjectName("reviewText")
        self.ocr.setReadOnly(True)
        self.ocr.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ocr.setMaximumHeight(130)
        preview_layout.addWidget(self.ocr)
        body.addWidget(preview, 1)
        side = QFrame()
        side.setObjectName("reviewTags")
        side.setFixedWidth(260)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(16, 16, 16, 14)
        side_layout.setSpacing(6)
        side_layout.addWidget(_label("主题（数字键切换，最多两个）", "eyebrow"))
        self.checks = {}
        for number, (key, title) in enumerate(TOPIC_LABELS.items(), start=1):
            check = QCheckBox(f"{number}  {title}")
            check.setObjectName("reviewTag")
            check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            check.toggled.connect(lambda checked, k=key: self._checked(k, checked))
            side_layout.addWidget(check)
            self.checks[key] = check
        side_layout.addSpacing(8)
        self.auto = _label("", "reviewAuto", True)
        side_layout.addWidget(self.auto)
        self.notice = _label("", "reviewHint", True)
        side_layout.addWidget(self.notice)
        side_layout.addStretch()
        body.addWidget(side)
        layout.addLayout(body, 1)
        footer = QHBoxLayout()
        footer.addWidget(_button("← 上一条", lambda: self.step(-1)))
        footer.addWidget(_button("跳过 →", lambda: self.step(1)))
        footer.addStretch()
        self.hint = _label("Enter 保存并下一条 · 0 清空并留在待整理 · →/空格 跳过 · Esc 关闭", "reviewHint")
        footer.addWidget(self.hint)
        footer.addStretch()
        footer.addWidget(_button("清空并下一条", self.clear_and_save))
        self.save_button = _button("保存并下一条", self.save_and_next, "primary")
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

    def load_queue(self):
        self.queue = self.vault.review_queue(self.scope.currentData())
        self.index = 0
        self.show_current()

    def _selected(self):
        return [key for key, check in self.checks.items() if check.isChecked()]

    def _checked(self, key, checked):
        if checked and len(self._selected()) > 2:
            self.checks[key].blockSignals(True)
            self.checks[key].setChecked(False)
            self.checks[key].blockSignals(False)
            self.notice.setText("最多两个主题，请先取消一个。")

    def toggle(self, key):
        if self.row is not None:
            self.notice.clear()
            self.checks[key].setChecked(not self.checks[key].isChecked())

    def show_current(self):
        # Skip items deleted or decided manually since the queue was built.
        while self.index < len(self.queue):
            row = self.vault.get(self.queue[self.index])
            if row is not None and not row["tags_manual"]:
                break
            self.queue.pop(self.index)
        self.row = self.vault.get(self.queue[self.index]) if self.index < len(self.queue) else None
        self.notice.clear()
        self.progress.setText(f"{min(self.index + 1, len(self.queue))} / {len(self.queue)} · 本次已保存 {self.saved} 条")
        enabled = self.row is not None
        for check in self.checks.values():
            check.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        if not enabled:
            if self.queue:
                self.title.setText(f"已到末尾 · 还有 {len(self.queue)} 条跳过的内容，按 ← 返回查看")
            else:
                self.title.setText("这一范围已经整理完了" if self.saved else "这一范围没有需要整理的内容")
            self.pages.setCurrentWidget(self.text)
            self.text.setPlainText("")
            self.ocr.hide()
            self.auto.setText("")
            for check in self.checks.values():
                check.setChecked(False)
            return
        row = self.row
        tags = [tag for tag in row["tags"] if tag in TOPIC_LABELS]
        for key, check in self.checks.items():
            check.blockSignals(True)
            check.setChecked(key in tags)
            check.blockSignals(False)
        names = " · ".join(ALL_TOPIC_LABELS.get(tag, tag) for tag in row["tags"])
        status = row.get("classification_status")
        self.auto.setText("自动结果：" + (names or "没有合适主题") if status == "done" else "自动分类尚未完成")
        stamp = datetime.fromisoformat(row["captured_at"]).astimezone().strftime("%Y/%m/%d %H:%M")
        if row["item_type"] == "image":
            self.title.setText(f"{row['title']}  ·  {stamp}")
            pixmap = QPixmap(str(self.vault.images / row["filename"]))
            if pixmap.isNull():
                self.image.setPixmap(QPixmap())
                self.image.setText("原图不存在或无法读取")
            else:
                self.image.setPixmap(pixmap.scaled(max(200, self.image.width()), max(160, self.image.height()),
                                                   Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.pages.setCurrentWidget(self.image)
            self.ocr.setPlainText(row["ocr_text"] or "（没有识别出文字）")
            self.ocr.show()
        else:
            self.title.setText(("链接" if row["item_type"] == "link" else "文字") + f"  ·  {stamp}")
            self.text.setPlainText(row["content"])
            self.pages.setCurrentWidget(self.text)
            self.ocr.hide()

    def step(self, direction):
        if not self.queue:
            return
        self.index = max(0, min(len(self.queue), self.index + direction))
        self.show_current()

    def save_and_next(self):
        if self.row is None:
            return
        self.vault.set_tags(self.row["id"], self._selected(), source="review")
        self.saved += 1
        self.queue.pop(self.index)
        self.show_current()

    def clear_and_save(self):
        if self.row is None:
            return
        for check in self.checks.values():
            check.setChecked(False)
        self.save_and_next()

    def done(self, result):
        if self.saved and hasattr(self.host, "refresh"):
            self.host.refresh()
        super().done(result)
