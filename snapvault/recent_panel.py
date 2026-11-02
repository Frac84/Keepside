"""Recent file shelf sharing the existing content-search palette."""
from datetime import datetime
from pathlib import Path
import os
import threading

from PySide6.QtCore import QEvent, QItemSelectionModel, QPoint, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices, QDrag, QGuiApplication, QIcon, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QHBoxLayout,
    QComboBox, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton, QTabBar, QToolTip, QVBoxLayout, QWidget)

from . import hotkey, windows_hello
from .branding import app_name
from .file_transfer import file_mime_data, library_file, reveal_file
from .gallery import glyph
from .quickpaste import QuickPastePanel
from .storage import normalized
from .theme import theme
from .floating_ball import FloatingBall, clamp_position
from .glass import apply_glass
from .shelf_chrome import DragHeader, RecentFileDelegate, shelf_colors, shelf_style

from .i18n import language, tr

LOCK_SETTING = "recent_files_locked_v1"


class _HelloWorker(QThread):
    completed = Signal(int, object)

    def __init__(self, generation, hwnd, parent):
        super().__init__(parent)
        self.generation, self.hwnd = generation, hwnd
        self.cancel_event = threading.Event()

    def run(self):
        try:
            result = windows_hello.verify(self.hwnd, tr('解锁{app}的最近文件与中转篮', app=app_name()), self.cancel_event)
        except Exception:
            result = windows_hello.Verification(False, "error", tr('Windows Hello 验证未能完成，请重试。'))
        self.completed.emit(self.generation, result)


class FileList(QListWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setObjectName("recentResults")
        self.setUniformItemSizes(True)
        self.setIconSize(QSize(48, 48))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(panel.file_menu)
        self.itemActivated.connect(lambda _item: panel.open_current())
        self.itemSelectionChanged.connect(panel.update_actions)

    def startDrag(self, _actions):
        self.panel.start_file_drag()

    def dragEnterEvent(self, event):
        self.panel.dragEnterEvent(event)

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        self.panel.dropEvent(event)


class BasketList(QListWidget):
    """A multi-file staging list; external file contents are never decoded."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setObjectName("recentResults")
        self.setAccessibleName(tr('文件中转篮'))
        self.setUniformItemSizes(True)
        self.setIconSize(QSize(48, 48))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(panel.basket_menu)
        self.itemActivated.connect(lambda _item: panel.open_basket_current())
        self.itemSelectionChanged.connect(panel.update_basket_actions)

    def startDrag(self, _actions):
        self.panel.start_basket_drag()

    def dragEnterEvent(self, event):
        if (not self.panel.files_locked and event.source() is not self
                and event.possibleActions() & Qt.DropAction.CopyAction and self.panel.drop_paths(event.mimeData())):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        paths = self.panel.drop_paths(event.mimeData())
        if (not self.panel.files_locked and event.source() is not self and paths
                and event.possibleActions() & Qt.DropAction.CopyAction):
            result = self.panel.add_basket_paths(paths)
            if result is None:
                event.ignore()
                return
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class BasketDragButton(QPushButton):
    """Begin the native drag after an actual held-mouse drag gesture."""

    def __init__(self, panel):
        super().__init__(tr('全部拖出'))
        self.panel = panel
        self._press_position = None
        self._drag_started = False
        self.setObjectName("quickTool")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._show_instruction)

    def _show_instruction(self):
        if not self.panel.files_locked:
            self.panel.status.setText(tr('按住“全部拖出”拖向目标窗口，或使用“复制全部”后粘贴。'))

    def mousePressEvent(self, event):
        self._press_position = None
        self._drag_started = False
        if self.isEnabled() and not self.panel.files_locked and event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.panel.files_locked:
            self._press_position = None
            self.setDown(False)
            event.ignore()
            return
        if self._drag_started:
            event.accept()
            return
        if (self.isEnabled() and self._press_position is not None
                and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._press_position).manhattanLength()
                    >= QGuiApplication.styleHints().startDragDistance()):
            self._press_position = None
            self._drag_started = True
            self.setDown(False)
            event.accept()
            self.panel.start_basket_drag(all_items=True)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_position = None
        if self._drag_started:
            self._drag_started = False
            self.setDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ShelfPanel(QuickPastePanel):
    """Keep content search intact and add a separate model for external files."""

    def __init__(self, host):
        super().__init__(host)
        if os.name == "nt" and hasattr(Qt.WindowType, "ExpandedClientAreaHint"):
            # Qt's supported native-frame client-area path preserves an alpha
            # backing store without the layered frameless-window route.
            self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.CustomizeWindowHint |
                Qt.WindowType.ExpandedClientAreaHint | Qt.WindowType.NoTitleBarBackgroundHint |
                Qt.WindowType.WindowStaysOnTopHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            if hasattr(Qt.WidgetAttribute, "WA_ContentsMarginsRespectsSafeArea"):
                self.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        self._dragging = False
        self._interaction_depth = 0
        self._shutting_down = False
        self._capture_suspended = False
        self._window_press = self._window_start = None
        self._window_moved = False
        self._orb = None
        self._opening_from_ball = False
        self.files_locked = bool(host.vault.setting(LOCK_SETTING, False))
        self._auth_generation = 0
        self._auth_worker = None
        self.last_lock_error = ""
        self.shelf_dark = host.vault.setting("shelf_appearance", "dark") != "light"
        self.glass_state = dict(enabled=False, method="fallback", reason="not shown")
        self._icons = {}
        self._hidden_shots = set(host.vault.setting("recent_hidden_screenshots", []))
        self._pinned_shots = set(host.vault.setting("recent_pinned_screenshots", []))
        if not hasattr(host, "file_basket"):
            from .file_basket import FileBasket
            host.file_basket = FileBasket(host.vault, host)
        self.basket = host.file_basket
        self.setWindowTitle(tr('{p0} · 随手面板', p0=app_name()))
        self.setWindowIcon(host.windowIcon())
        self.resize(520 if language() == "en_US" else 460, 570)
        self.setMinimumSize(480 if language() == "en_US" else 400, 440)
        self.setAutoFillBackground(False)
        self.setAcceptDrops(True)
        layout = self.findChild(QWidget, "quickFrame").layout()
        layout.setContentsMargins(16, 8, 16, 14)
        layout.setSpacing(10)
        self.header = DragHeader(self)
        self.lock_button = self.header.lock_button
        self.keep_open = self.header.keep_open
        self.keep_open.setChecked(bool(host.vault.setting("shelf_keep_open", False)))
        self.keep_open.toggled.connect(lambda value: host.vault.set_setting("shelf_keep_open", value))
        layout.insertWidget(0, self.header)
        self.tabs = QTabBar()
        self.tabs.setObjectName("quickTabs")
        self.tabs.setExpanding(True)
        self.tabs.addTab(tr('内容库'))
        self.tabs.addTab(tr('最近文件'))
        self.tabs.addTab(tr('中转篮'))
        layout.insertWidget(1, self.tabs)
        self.file_results = FileList(self)
        self.file_results.setItemDelegate(RecentFileDelegate(self.file_results))
        self.file_results.installEventFilter(self)
        layout.insertWidget(layout.indexOf(self.results) + 1, self.file_results, 1)
        self.basket_results = BasketList(self)
        self.basket_results.setItemDelegate(RecentFileDelegate(self.basket_results))
        self.basket_results.installEventFilter(self)
        layout.insertWidget(layout.indexOf(self.file_results) + 1, self.basket_results, 1)
        self.lock_card = QWidget()
        self.lock_card.setObjectName("recentLockCard")
        locked = QVBoxLayout(self.lock_card)
        locked.setContentsMargins(20, 24, 20, 24)
        locked.setSpacing(16)
        locked.addStretch()
        title = QLabel(tr('最近文件与中转篮已锁定'))
        title.setObjectName("recentLockTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        locked.addWidget(title)
        detail = QLabel(tr('由你手动锁定，解锁后继续使用。'))
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail.setWordWrap(True)
        locked.addWidget(detail)
        self.unlock_button = self.tool(tr('使用 Windows Hello 解锁'), self.unlock_files)
        self.unlock_button.setMinimumHeight(44)
        self.unlock_button.setAccessibleName(tr('使用 Windows Hello 解锁最近文件与中转篮'))
        locked.addWidget(self.unlock_button)
        self.lock_message = QLabel(tr('可在系统验证窗口选择 PIN 或指纹。'))
        self.lock_message.setObjectName("recentLockMessage")
        self.lock_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lock_message.setWordWrap(True)
        locked.addWidget(self.lock_message)
        self.cancel_unlock_button = self.tool(tr('取消验证'), self.cancel_unlock)
        self.cancel_unlock_button.hide()
        locked.addWidget(self.cancel_unlock_button)
        locked.addStretch()
        boundary = QLabel(tr('保护最近文件与中转篮入口。内容库和 Windows 自身的历史不受此锁保护；本地数据未加密。'))
        boundary.setObjectName("recentLockBoundary")
        boundary.setWordWrap(True)
        boundary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        locked.addWidget(boundary)
        self.lock_card.hide()
        layout.insertWidget(layout.indexOf(self.file_results), self.lock_card, 1)
        self.overview = QWidget()
        overview = QHBoxLayout(self.overview)
        overview.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel(tr('最近使用'))
        self.summary.setObjectName("shelfSummary")
        overview.addWidget(self.summary)
        overview.addStretch()
        self.source_filter = QComboBox()
        self.source_filter.setObjectName("sourceFilter")
        self.source_filter.setAccessibleName(tr('文件来源筛选'))
        for title, key in ((tr('最近打开'), "system"), (tr('已固定'), "pinned"), (tr('添加与监听'), "external"),
                           (tr('截图'), "screenshots"), (tr('全部来源'), "all")):
            self.source_filter.addItem(title, key)
        self.source_filter.currentIndexChanged.connect(lambda _index: self.refresh())
        overview.addWidget(self.source_filter)
        layout.insertWidget(layout.indexOf(self.file_results), self.overview)
        self.toolbar = QWidget()
        tools = QHBoxLayout(self.toolbar)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        self.open_button = self.tool(tr('打开'), self.open_current)
        self.open_button.setToolTip(tr('用系统默认应用打开文件（双击或 Enter）'))
        self.reveal_button = self.tool(tr('定位文件夹'), self.reveal_current)
        self.reveal_button.setToolTip(tr('打开所在文件夹并选中文件（Alt+Enter）'))
        self.copy_button = self.tool(tr('复制'), lambda: self.accept_current(paste=False))
        self.copy_button.setToolTip(tr('复制文件入口（Ctrl+Enter）；粘贴时由目标程序复制原文件'))
        self.pin_button = self.tool(tr('固定'), self.toggle_pin)
        tools.addWidget(self.open_button)
        tools.addWidget(self.reveal_button)
        tools.addWidget(self.copy_button)
        tools.addWidget(self.pin_button)
        tools.addStretch()
        self.sources_button = self.tool(tr('管理来源 ▾'), self.source_menu)
        tools.addWidget(self.sources_button)
        self.sources_button.hide()
        layout.insertWidget(layout.indexOf(self.hint), self.toolbar)
        self.basket_overview = QWidget()
        basket_overview = QHBoxLayout(self.basket_overview)
        basket_overview.setContentsMargins(0, 0, 0, 0)
        self.basket_summary = QLabel("")
        self.basket_summary.setObjectName("shelfSummary")
        basket_overview.addWidget(self.basket_summary)
        basket_overview.addStretch()
        layout.insertWidget(layout.indexOf(self.basket_results), self.basket_overview)
        self.basket_toolbar = QWidget()
        basket_tools = QVBoxLayout(self.basket_toolbar)
        basket_tools.setContentsMargins(0, 0, 0, 0)
        basket_tools.setSpacing(6)
        top_tools, bottom_tools = QHBoxLayout(), QHBoxLayout()
        top_tools.setSpacing(6)
        bottom_tools.setSpacing(6)
        self.basket_drag_all_button = BasketDragButton(self)
        self.basket_copy_all_button = self.tool(tr('复制全部'), lambda: self.copy_basket(all_items=True))
        self.basket_paste_button = self.tool(tr('粘贴文件'), self.paste_basket_files)
        self.basket_add_button = self.tool(tr('添加文件'), self.choose_basket_files)
        self.basket_open_button = self.tool(tr('打开'), self.open_basket_current)
        self.basket_reveal_button = self.tool(tr('定位文件夹'), self.reveal_basket_current)
        self.basket_remove_button = self.tool(tr('移除选中'), self.remove_basket_entries)
        self.basket_clear_button = self.tool(tr('清空篮子'), self.clear_basket)
        for control in (self.basket_drag_all_button, self.basket_copy_all_button, self.basket_paste_button, self.basket_add_button,
                        self.basket_open_button, self.basket_reveal_button, self.basket_remove_button,
                        self.basket_clear_button):
            control.setMinimumHeight(44)
            control.setAccessibleName(tr('中转篮：{action}', action=control.text()))
        self.basket_drag_all_button.setToolTip(tr('按住此按钮，把全部可用文件一起拖到支持文件拖放的窗口；原文件留在原处'))
        self.basket_drag_all_button.setAccessibleDescription(tr('按住鼠标拖动；也可用“复制全部”后到目标窗口粘贴'))
        self.basket_copy_all_button.setToolTip(tr('复制全部可用文件，可到目标窗口按 Ctrl+V'))
        self.basket_paste_button.setToolTip(tr('先在文件资源管理器中选中文件并按 Ctrl+C，再点击这里或按 Ctrl+V'))
        self.basket_remove_button.setToolTip(tr('仅从中转篮移除选中引用，不删除原文件'))
        self.basket_clear_button.setToolTip(tr('仅清空中转篮引用，不删除原文件'))
        for control in (self.basket_paste_button, self.basket_add_button,
                        self.basket_drag_all_button, self.basket_copy_all_button):
            top_tools.addWidget(control)
        for control in (self.basket_open_button, self.basket_reveal_button,
                        self.basket_remove_button, self.basket_clear_button):
            bottom_tools.addWidget(control)
        basket_tools.addLayout(top_tools)
        basket_tools.addLayout(bottom_tools)
        self.basket_note = QLabel(tr('只暂存文件路径，不复制大文件。移除或清空不会删除原文件。'))
        self.basket_note.setObjectName("quickHint")
        self.basket_note.setWordWrap(True)
        basket_tools.addWidget(self.basket_note)
        layout.insertWidget(layout.indexOf(self.hint), self.basket_toolbar)
        self.status = QLabel("")
        self.status.setObjectName("quickStatus")
        self.status.setWordWrap(True)
        layout.insertWidget(layout.indexOf(self.hint), self.status)
        self.tabs.currentChanged.connect(self.tab_changed)
        host.recent_files.changed.connect(self.request_refresh)
        self.basket.changed.connect(self._basket_changed)
        initial_tab = host.vault.setting("quick_panel_tab_v2", None)
        if not isinstance(initial_tab, int) or isinstance(initial_tab, bool) or initial_tab not in (0, 1, 2):
            initial_tab = int(bool(host.vault.setting("quick_panel_files_tab", True)))
        self.tabs.setCurrentIndex(initial_tab)
        self.tab_changed(self.tabs.currentIndex())
        self._lock_shortcut = QShortcut(QKeySequence("Ctrl+L"), self, activated=self.lock_files)
        self._sync_lock_controls()
        self.refresh_theme()

    @property
    def authentication_busy(self):
        return self._auth_worker is not None and self._auth_worker.isRunning()

    def _sync_lock_controls(self):
        self.lock_button.setChecked(self.files_locked)
        label = tr('解锁最近文件与中转篮') if self.files_locked else tr('锁定最近文件与中转篮（Ctrl+L）')
        self.lock_button.setToolTip(label)
        self.lock_button.setAccessibleName(label)
        self.unlock_button.setEnabled(not self.authentication_busy)
        self.cancel_unlock_button.setVisible(self.authentication_busy)

    def _clear_file_details(self):
        self.file_results.clear()
        self.file_results.setToolTip("")
        self._icons.clear()
        self.summary.clear()
        self.summary.setToolTip("")
        self.status.clear()
        self.status.setToolTip("")
        if hasattr(self, "basket_results"):
            self.basket_results.clear()
            self.basket_results.setToolTip("")
            self.basket_summary.clear()
            self.basket_summary.setToolTip("")

    def lock_files(self):
        """Only the user's explicit action locks; no idle/session timer is used."""
        self.last_lock_error = ""
        if not self.files_locked:
            try:
                available = windows_hello.availability()
                if not available.usable:
                    self.last_lock_error = available.message
                    self.status.setText(self.last_lock_error)
                    self._sync_lock_controls()
                    return False
                # Persist before claiming success, so restarting cannot bypass the lock.
                self.host.vault.set_setting(LOCK_SETTING, True)
            except Exception:
                self.last_lock_error = tr('无法保存锁定状态，请检查数据目录是否可写。')
                self.status.setText(self.last_lock_error)
                self._sync_lock_controls()
                return False
        self.files_locked = True
        self.cancel_unlock()
        self.timer.stop()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._clear_file_details()
        self.target = 0
        QToolTip.hideText()
        for menu in self.findChildren(QMenu):
            menu.close()
        for dialog in self.findChildren(QFileDialog):
            dialog.reject()
        if self._dragging:
            QDrag.cancel()
        self.lock_message.setText(tr('可在系统验证窗口选择 PIN 或指纹。'))
        self._sync_lock_controls()
        self.refresh()
        return True

    def toggle_files_lock(self):
        if self.files_locked:
            self.tabs.setCurrentIndex(1)
            self.unlock_files()
        else:
            self.lock_files()

    def unlock_files(self):
        """Verification is requested only by an explicit unlock click."""
        if not self.files_locked or self.authentication_busy or self._shutting_down:
            return
        self._auth_generation += 1
        worker = _HelloWorker(self._auth_generation, int(self.winId()), self)
        self._auth_worker = worker
        worker.completed.connect(self._unlock_result)
        worker.finished.connect(lambda current=worker: self._auth_finished(current))
        self.lock_message.setText(tr('正在等待 Windows Hello 验证…可在系统窗口选择其他验证方式。'))
        worker.start()
        self._sync_lock_controls()

    def cancel_unlock(self):
        self._auth_generation += 1
        if self._auth_worker is not None:
            self._auth_worker.cancel_event.set()
        if hasattr(self, "lock_message"):
            self.lock_message.setText(tr('验证已取消，最近文件仍保持锁定。'))

    def _unlock_result(self, generation, result):
        if (generation != self._auth_generation or self._auth_worker is None
                or self._auth_worker.cancel_event.is_set() or self._shutting_down):
            return
        if getattr(result, "success", False) is True and getattr(result, "status", "") == "verified":
            try:
                self.host.vault.set_setting(LOCK_SETTING, False)
            except Exception:
                self.lock_message.setText(tr('验证已通过，但无法保存解锁状态，请检查数据目录后重试。'))
                return
            self.files_locked = False
            self.lock_message.setText(tr('可在系统验证窗口选择 PIN 或指纹。'))
            self.refresh()
            self.search.setFocus()
        else:
            self.lock_message.setText(getattr(result, "message", "") or tr('验证未通过，最近文件仍保持锁定。'))

    def _auth_finished(self, worker):
        if worker is self._auth_worker:
            self._auth_worker = None
        worker.deleteLater()
        self._sync_lock_controls()

    def tool(self, text, callback):
        widget = QPushButton(text)
        widget.setObjectName("quickTool")
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
        widget.clicked.connect(callback)
        return widget

    @property
    def files_mode(self):
        return hasattr(self, "tabs") and self.tabs.currentIndex() == 1

    @property
    def basket_mode(self):
        return hasattr(self, "tabs") and self.tabs.currentIndex() == 2

    @property
    def protected_mode(self):
        return self.files_mode or self.basket_mode

    @property
    def basket_count(self):
        return 0 if self.files_locked else self.basket.count

    def tab_changed(self, _index):
        self.host.vault.set_setting("quick_panel_tab_v2", self.tabs.currentIndex())
        if not self.basket_mode:
            self.host.vault.set_setting("quick_panel_files_tab", self.files_mode)
        self.search.setPlaceholderText(tr('搜索中转篮文件名…') if self.basket_mode else
                                       tr('搜索文件名…') if self.files_mode else tr('搜索图片文字、原文或链接…'))
        self.status.clear()
        self._update_hint()
        if self.files_mode and not self.files_locked:
            self.host.recent_files.refresh()
        self.refresh()

    def _update_hint(self):
        if self.basket_mode:
            self.hint.setText(tr('由你决定何时锁定 · Esc 收起') if self.files_locked else
                              tr('Ctrl+V 加入文件 · 多选拖出 · Ctrl+Enter 复制选中'))
            self.hint.setToolTip("" if self.files_locked else tr('全部拖出不受搜索筛选影响；已丢失的文件会跳过，引用保留。'))
        elif self.files_mode:
            self.hint.setText(tr('由你决定何时锁定 · Esc 收起') if self.files_locked else tr('双击打开 · Alt+Enter 定位 · Ctrl+L 锁定'))
            self.hint.setToolTip("" if self.files_locked else tr('Ctrl+Enter 复制文件；可多选后复制或拖出。仅记录原路径，不导入文件。'))
        else:
            super()._update_hint()

    def open(self, files=None, from_ball=False):
        self._opening_from_ball = bool(from_ball)
        # Read the destination before hiding the no-activate floating ball.
        target = hotkey.foreground_window()
        if self._orb is not None:
            self._orb.hide()
        if files is not None:
            self.tabs.setCurrentIndex(int(files))
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._update_hint()
        self.refresh()
        self.status.clear()
        self.target = target if target and target not in (int(self.winId()), int(self.host.winId())) else 0
        saved = self.host.vault.setting("shelf_panel_position", None)
        if from_ball and self._orb is not None:
            anchor = self._orb.pos()
            screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
            left = anchor.x() - self.width() - 12
            if screen and left < screen.availableGeometry().left():
                left = anchor.x() + self._orb.width() + 12
            position = QPoint(left, anchor.y())
        elif isinstance(saved, list) and len(saved) == 2 and all(isinstance(v, int) for v in saved):
            position = QPoint(*saved)
        else:
            position = QCursor.pos() + QPoint(16, 16)
        self.move(clamp_position(position, self.size()))
        self.show()
        self.raise_()
        self.activateWindow()
        (self.unlock_button if self.protected_mode and self.files_locked else self.search).setFocus()
        if self.files_mode and not self.files_locked:
            self.host.recent_files.refresh()
        self._opening_from_ball = False

    def request_refresh(self):
        if self.isVisible():
            self.timer.start()

    def refresh_theme(self):
        if not hasattr(self, "shelf_dark"):
            return super().refresh_theme()
        self.setStyleSheet(shelf_style(self.shelf_dark, self.glass_state.get("enabled", False)))
        if hasattr(self, "header"):
            self.header.set_dark(self.shelf_dark)
        if hasattr(self, "file_results"):
            self.file_results.itemDelegate().set_dark(self.shelf_dark)
        if hasattr(self, "basket_results"):
            self.basket_results.itemDelegate().set_dark(self.shelf_dark)
        if hasattr(self, "_icons"):
            self._icons.clear()
            self.request_refresh()
        if self._orb is not None:
            self._orb.update()

    def showEvent(self, event):
        super().showEvent(event)
        if self.isWindow() and not self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.glass_state = apply_glass(self, self.shelf_dark)
            self.refresh_theme()

    def set_appearance(self, dark):
        self.shelf_dark = bool(dark)
        self.host.vault.set_setting("shelf_appearance", "dark" if dark else "light")
        if self.isVisible() and self.isWindow() and not self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.glass_state = apply_glass(self, self.shelf_dark)
        self.refresh_theme()

    def begin_window_drag(self, point):
        self._window_press, self._window_start = QPoint(point), self.pos()
        self._window_moved = False

    def move_window_drag(self, point):
        if self._window_press is None:
            return
        delta = point - self._window_press
        if self._window_moved or delta.manhattanLength() >= QGuiApplication.styleHints().startDragDistance():
            self._window_moved = True
            self.move(clamp_position(self._window_start + delta, self.size()))

    def end_window_drag(self):
        if self._window_moved:
            self.host.vault.set_setting("shelf_panel_position", [self.x(), self.y()])
        self._window_press = self._window_start = None
        self._window_moved = False

    def ensure_ball(self):
        if self._orb is None:
            self._orb = FloatingBall(self)
            self._orb.setAcceptDrops(True)
            saved = self.host.vault.setting("shelf_ball_position", None)
            if isinstance(saved, list) and len(saved) == 2 and all(isinstance(v, int) for v in saved):
                position = QPoint(*saved)
            else:
                screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
                area = screen.availableGeometry() if screen else self.geometry()
                position = QPoint(area.right() - 80, area.center().y() - 30)
            self._orb.restore_position(position, self.host.vault.setting("shelf_ball_dock_edge", ""))
        return self._orb

    def save_ball_position(self):
        if self._orb is not None:
            position = self._orb.persistent_position()
            self.host.vault.set_setting("shelf_ball_position", [position.x(), position.y()])
            self.host.vault.set_setting("shelf_ball_dock_edge", self._orb.dock_edge)

    def show_ball(self, enable=False):
        if enable:
            self.host.vault.set_setting("shelf_ball_enabled", True)
        if self._shutting_down or self._capture_suspended or not self.host.vault.setting("shelf_ball_enabled", True):
            return
        ball = self.ensure_ball()
        ball.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,
                          self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen))
        ball.show()
        ball.raise_()

    def hide_ball(self, permanent=False):
        if permanent:
            self.host.vault.set_setting("shelf_ball_enabled", False)
        if self._orb is not None:
            self._orb.hide()

    def open_from_ball(self):
        if self.basket_count:
            self.open_basket(from_ball=True)
        else:
            self.open(files=True, from_ball=True)

    def open_basket(self, from_ball=False):
        self.tabs.setCurrentIndex(2)
        self.open(from_ball=from_ball)

    def collapse(self):
        if self._shutting_down:
            self.hide()
            return
        if self.authentication_busy:
            self.cancel_unlock()
        if self.isVisible():
            self.ensure_ball()
            self.save_ball_position()
            self.host.vault.set_setting("shelf_panel_position", [self.x(), self.y()])
        self.hide()
        self._icons.clear()
        self.results.clear()
        self.file_results.clear()
        self.basket_results.clear()
        self.show_ball()

    def suspend_for_capture(self):
        state = (self.isVisible(), self._orb is not None and self._orb.isVisible())
        self._capture_suspended = True
        self.hide()
        self.hide_ball()
        QGuiApplication.processEvents()
        return state

    def resume_after_capture(self, state):
        self._capture_suspended = False
        if self._shutting_down:
            return
        if state and state[0]:
            self.show()
        elif state and state[1]:
            self.show_ball()

    def shutdown(self):
        self._shutting_down = True
        self.cancel_unlock()
        if self._auth_worker is not None and self._auth_worker.isRunning():
            self._auth_worker.wait(500)
        self.timer.stop()
        self.hide()
        self.hide_ball()
        if self._orb is not None:
            self._orb.deleteLater()
            self._orb = None

    def closeEvent(self, event):
        if self._shutting_down:
            event.accept()
        else:
            event.ignore()
            self.collapse()

    def file_entries(self):
        if self.files_locked:
            return []
        text = self.search.text().strip()
        source = self.source_filter.currentData() if hasattr(self, "source_filter") else "system"
        entries = self.host.recent_files.entries(text, limit=180)
        include_shots = source in ("all", "screenshots", "pinned")
        rows = self.host.vault.query(text, limit=80, item_type="image", sort="newest") if include_shots else []
        present = {row["id"] for row in rows}
        # Fixed screenshots stay available even after newer captures arrive.
        for row in self.host.vault.rows_by_ids(sorted(self._pinned_shots - present)) if include_shots else []:
            if row.get("item_type", "image") == "image" and all(
                    normalized(term) in normalized(row.get("search_text", "")) for term in text.split()):
                rows.append(row)
        for row in rows:
            if row["id"] in self._hidden_shots:
                continue
            path = self.host.vault.images / row["filename"]
            entries.append(dict(key="vault:" + row["id"], vault_id=row["id"], path=str(path),
                name=(row.get("title") or tr('截图')) + ".png", source=tr('截图'),
                seen_at=datetime.fromisoformat(row["captured_at"]).timestamp(),
                pinned=row["id"] in self._pinned_shots, exists=path.is_file(),
                thumbnail=str(self.host.vault.thumbs / f"{row['id']}.png")))
        if source == "system":
            entries = [entry for entry in entries if entry.get("origin") == "system_recent"]
        elif source == "external":
            entries = [entry for entry in entries if not entry.get("vault_id") and entry.get("origin") != "system_recent"]
        elif source == "screenshots":
            entries = [entry for entry in entries if entry.get("vault_id")]
        elif source == "pinned":
            entries = [entry for entry in entries if entry.get("pinned")]
        entries.sort(key=lambda e: (bool(e["pinned"]), e["seen_at"]), reverse=True)
        return entries[:80]

    def file_icon(self, entry):
        if self.files_locked:
            return QIcon()
        path = entry.get("thumbnail") or entry["path"]
        cache_key = (path, entry.get("mtime_ns"), entry["exists"])
        if cache_key in self._icons:
            return self._icons[cache_key]
        icon = None
        # External records are metadata-only, even for multi-GB videos or
        # large images. Only our precomputed screenshot thumbnail is decoded.
        if entry["exists"] and entry.get("vault_id") and entry.get("thumbnail"):
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid():
                reader.setScaledSize(size.scaled(QSize(96, 96), Qt.AspectRatioMode.KeepAspectRatio))
                image = reader.read()
                if not image.isNull():
                    icon = QIcon(QPixmap.fromImage(image))
        if icon is None:
            icon = glyph("image" if entry.get("vault_id") else "text", theme()["accent"], 36)
        if len(self._icons) >= 96:
            self._icons.clear()
        self._icons[cache_key] = icon
        return icon

    def source_label(self, entry):
        """Localize built-in sources without changing user folder names or records."""
        if entry.get("vault_id"):
            return tr('截图')
        if entry.get("origin") == "system_recent":
            return tr('最近使用')
        if entry.get("origin") == "manual" or entry.get("manual"):
            return tr('手动添加')
        source = entry.get("source", "")
        download = getattr(self.host.recent_files, "_download_folder", None)
        if source == "下载" and download and os.path.normcase(str(Path(entry["path"]).parent)) == os.path.normcase(str(download)):
            return tr('下载')
        return source

    def refresh(self):
        if not hasattr(self, "file_results"):
            return super().refresh()
        self._sync_lock_controls()
        self.lock_card.setVisible(self.protected_mode and self.files_locked)
        self.search.setVisible(not (self.protected_mode and self.files_locked))
        if self.files_locked:
            self._clear_file_details()
            self.basket_toolbar.hide()
            self.basket_overview.hide()
            self.basket_results.hide()
            self.toolbar.hide()
            self.overview.hide()
            self.file_results.hide()
            self._update_hint()
            if self.protected_mode:
                self.results.hide()
                self.empty.hide()
                self.update_actions()
                self.update_basket_actions()
                return
        if self._dragging or self._interaction_depth:
            return
        self.basket_toolbar.hide()
        self.basket_overview.hide()
        self.basket_results.hide()
        if self.basket_mode:
            self.toolbar.hide()
            self.overview.hide()
            self.file_results.hide()
            self.results.hide()
            return self.refresh_basket()
        self.toolbar.setVisible(self.files_mode)
        self.overview.setVisible(self.files_mode)
        self.file_results.setVisible(self.files_mode)
        self.results.setVisible(not self.files_mode)
        if not self.files_mode:
            return super().refresh()
        selected = {e["key"] for e in self.selected_entries()}
        current = self.file_results.currentItem()
        key = current.data(Qt.ItemDataRole.UserRole)["key"] if current else None
        scroll = self.file_results.verticalScrollBar().value()
        entries = self.file_entries()
        self.file_results.blockSignals(True)
        self.file_results.clear()
        for entry in entries:
            stamp = datetime.fromtimestamp(entry["seen_at"]).strftime("%m/%d %H:%M")
            kind = Path(entry["path"]).suffix.lstrip(".").upper() or tr('文件')
            detail = f"{self.source_label(entry)} · {kind} · {stamp}"
            if not entry["exists"]:
                detail += tr(' · 已移动或删除')
            item = QListWidgetItem(("★  " if entry["pinned"] else "") + entry["name"] + "\n" + detail)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(entry["name"] + "\n" + entry["path"])
            item.setIcon(self.file_icon(entry))
            item.setSizeHint(QSize(0, 64))
            self.file_results.addItem(item)
            if entry["key"] == key:
                self.file_results.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
            item.setSelected(entry["key"] in selected)
        if entries and not self.file_results.selectedItems():
            self.file_results.setCurrentRow(0)
        self.file_results.verticalScrollBar().setValue(scroll)
        self.file_results.blockSignals(False)
        self.file_results.setVisible(bool(entries))
        self.empty.setVisible(not entries)
        self.empty.setText(tr('没有匹配的文件名。') if self.search.text().strip() else
            tr('Windows 记录的最近打开文件会出现在这里。\n双击打开，选中后可定位所在文件夹。'))
        self.summary.setText(tr('{p0} 个文件', p0=len(entries)))
        self.summary.setToolTip(tr('默认显示 Windows 最近打开记录；部分应用不会登记。只保存路径和时间，不复制原文件，不加载视频。'))
        self.update_actions()

    def selected_entries(self):
        if self.files_locked:
            return []
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.file_results.selectedItems()]

    def update_actions(self):
        if not hasattr(self, "copy_button"):
            return
        entries = self.selected_entries()
        self.open_button.setEnabled(len(entries) == 1 and entries[0]["exists"])
        self.copy_button.setEnabled(bool(entries) and all(e["exists"] for e in entries))
        self.pin_button.setEnabled(bool(entries))
        self.pin_button.setText(tr('取消固定') if entries and all(e["pinned"] for e in entries) else tr('固定'))
        self.reveal_button.setEnabled(len(entries) == 1 and entries[0]["exists"])

    def prepared_paths(self):
        if self.files_locked:
            raise ValueError(tr('最近文件已锁定'))
        entries = self.selected_entries()
        if not entries:
            raise ValueError(tr('请先选择文件'))
        return [library_file(self.host.vault, e["vault_id"]) if e.get("vault_id") else Path(e["path"]) for e in entries]

    def current_file_path(self):
        if self.files_locked:
            raise ValueError(tr('最近文件已锁定'))
        entries = self.selected_entries()
        if len(entries) != 1:
            raise ValueError(tr('请选择一个文件'))
        entry = entries[0]
        path = Path(entry["path"])
        if entry.get("vault_id"):
            row = self.host.vault.get(entry["vault_id"])
            if row is None:
                raise FileNotFoundError(tr('这条截图已不存在'))
            path = self.host.vault.images / row["filename"]
        if not path.is_file():
            raise FileNotFoundError(tr('文件已移动、删除或暂时无法访问'))
        return path

    def open_current(self):
        if self.basket_mode:
            return self.open_basket_current()
        if self.files_locked:
            return
        try:
            path = self.current_file_path()
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise OSError(tr('系统未能打开文件，请检查默认应用'))
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法打开：{p0}', p0=exc))
            self.request_refresh()
            return
        self.status.setText(tr('已请求系统默认应用打开文件。'))
        if not self.keep_open.isChecked():
            self.collapse()

    def accept_current(self, paste=True):
        if self.basket_mode:
            self.copy_basket()
            return
        if self.files_mode and self.files_locked:
            return
        if not self.files_mode:
            super().accept_current(paste)
            if not self.isVisible():
                self.show_ball()
            return
        try:
            mime = file_mime_data(self.prepared_paths())
            self.host._copy_owned(lambda: QApplication.clipboard().setMimeData(mime))
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法复制：{p0}', p0=exc))
            self.request_refresh()
            return
        if not self.keep_open.isChecked():
            self.collapse()
        self.status.setText(tr('已复制文件，可在目标窗口按 Ctrl+V。'))
        if paste and self.host.quick_autopaste and self.target and hotkey.focus_window(self.target):
            generation = self._auth_generation
            QTimer.singleShot(90, lambda: self._send_file_paste(generation))

    def _send_file_paste(self, generation):
        if not self.files_locked and not self._shutting_down and generation == self._auth_generation:
            hotkey.send_paste()

    def start_file_drag(self):
        if self.files_locked:
            return
        try:
            mime = file_mime_data(self.prepared_paths())
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法拖出：{p0}', p0=exc))
            self.request_refresh()
            return
        drag = QDrag(self.file_results)
        drag.setMimeData(mime)
        current = self.file_results.currentItem()
        if current:
            drag.setPixmap(current.icon().pixmap(48, 48))
        self._dragging = True
        try:
            result = drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)
        finally:
            self._dragging = False
            drag.deleteLater()
        if result != Qt.DropAction.IgnoreAction and not self.keep_open.isChecked():
            self.collapse()
        else:
            self.request_refresh()

    def toggle_pin(self):
        if self.files_locked:
            return
        entries = self.selected_entries()
        value = not all(e["pinned"] for e in entries)
        for entry in entries:
            if entry.get("vault_id"):
                (self._pinned_shots.add if value else self._pinned_shots.discard)(entry["vault_id"])
                if value:
                    row = self.host.vault.get(entry["vault_id"])
                    if row and not row["favorite"]:
                        self.host.vault.toggle_favorite(entry["vault_id"])
            else:
                self.host.recent_files.set_pinned(entry["key"], value)
        self.host.vault.set_setting("recent_pinned_screenshots", sorted(self._pinned_shots))
        if value and any(e.get("vault_id") for e in entries):
            self.status.setText(tr('固定的截图已加入收藏，避免到期清理；取消固定后仍保留收藏。'))
        self.refresh()

    def remove_entries(self):
        if self.files_locked:
            return
        for entry in self.selected_entries():
            if entry.get("vault_id"):
                self._hidden_shots.add(entry["vault_id"])
                self._pinned_shots.discard(entry["vault_id"])
            else:
                self.host.recent_files.remove(entry["key"])
        self.host.vault.set_setting("recent_hidden_screenshots", sorted(self._hidden_shots)[-2000:])
        self.host.vault.set_setting("recent_pinned_screenshots", sorted(self._pinned_shots))
        self.status.setText(tr('已移出浮台，原文件和图库内容保持原样。'))
        self.refresh()

    def reveal_current(self):
        if self.basket_mode:
            return self.reveal_basket_current()
        if self.files_locked:
            return
        try:
            if not reveal_file(self.current_file_path()):
                raise OSError(tr('系统未能打开所在文件夹'))
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法定位：{p0}', p0=exc))

    def file_menu(self, position):
        if self.files_locked:
            return
        if not self.selected_entries():
            return
        menu = QMenu(self)
        menu.addAction(tr('打开文件'), self.open_current).setEnabled(self.open_button.isEnabled())
        menu.addAction(tr('打开所在文件夹并选中'), self.reveal_current).setEnabled(self.reveal_button.isEnabled())
        menu.addSeparator()
        menu.addAction(tr('复制文件'), lambda: self.accept_current(paste=False))
        menu.addAction(self.pin_button.text(), self.toggle_pin)
        menu.addSeparator()
        menu.addAction(tr('从浮台移除（保留原文件）'), self.remove_entries)
        self.run_menu(menu, self.file_results.viewport().mapToGlobal(position))

    def run_menu(self, menu, point):
        self._interaction_depth += 1
        try:
            menu.exec(point)
        finally:
            self._interaction_depth -= 1
            menu.deleteLater()
            self.refresh()

    def source_menu(self):
        if self.basket_mode:
            self.basket_menu(None)
            return
        menu = QMenu(self)
        if self.files_locked:
            menu.addAction(tr('最近文件已锁定')).setEnabled(False)
            menu.addAction(tr('解锁最近文件…'), self.unlock_files)
            anchor = self.header.more_button
            self.run_menu(menu, anchor.mapToGlobal(anchor.rect().bottomLeft()))
            return
        service = self.host.recent_files
        system = menu.addAction(tr('Windows 最近打开记录'))
        system.setCheckable(True)
        system.setChecked(service.system_recent_enabled())
        system.toggled.connect(lambda enabled: None if self.files_locked else service.set_system_recent_enabled(enabled))
        ball = menu.addAction(tr('显示悬浮球'))
        ball.setCheckable(True)
        ball.setChecked(bool(self.host.vault.setting("shelf_ball_enabled", True)))
        ball.toggled.connect(lambda enabled: self.show_ball(enable=True) if enabled else self.hide_ball(permanent=True))
        appearance = menu.addMenu(tr('浮台外观'))
        for title, dark in ((tr('深色磨砂'), True), (tr('浅色磨砂'), False)):
            action = appearance.addAction(title)
            action.setCheckable(True)
            action.setChecked(self.shelf_dark == dark)
            action.triggered.connect(lambda checked=False, value=dark: self.set_appearance(value))
        menu.addSeparator()
        menu.addAction(tr('添加文件…'), self.choose_files)
        menu.addAction(tr('添加监听文件夹…'), self.choose_folder)
        menu.addSeparator()
        menu.addAction(tr('监听文件夹（只看本层文件）')).setEnabled(False)
        for folder in service.folders():
            submenu = menu.addMenu(folder)
            submenu.addAction(tr('停止监听'), lambda checked=False, path=folder: self._remove_folder(path))
        if not service.folders():
            menu.addAction(tr('尚未设置监听文件夹')).setEnabled(False)
        menu.addSeparator()
        menu.addAction(tr('重新检查文件'), service.refresh)
        anchor = self.header.more_button
        self.run_menu(menu, anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def choose_files(self):
        if self.files_locked:
            return
        self._interaction_depth += 1
        try:
            paths, _ = QFileDialog.getOpenFileNames(self, tr('添加到最近文件'), "", tr('所有文件 (*)'))
            if paths and not self.files_locked:
                self.add_paths(paths)
        finally:
            self._interaction_depth -= 1
            self.refresh()

    def choose_folder(self):
        if self.files_locked:
            return
        self._interaction_depth += 1
        try:
            path = QFileDialog.getExistingDirectory(self, tr('选择要监听的文件夹'))
            if path and not self.files_locked:
                self.host.recent_files.set_folders([*self.host.recent_files.folders(), path])
                self.status.setText(tr('已添加文件夹，正在检查最近文件…'))
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
        finally:
            self._interaction_depth -= 1
            self.refresh()

    def _basket_changed(self):
        if self._orb is not None:
            self._orb.update()
        if self.basket_mode:
            self.request_refresh()

    def basket_entries(self):
        if self.files_locked:
            return []
        return self.basket.entries(self.search.text().strip())

    def selected_basket_entries(self):
        if self.files_locked:
            return []
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.basket_results.selectedItems()]

    def refresh_basket(self):
        if self.files_locked:
            self._clear_file_details()
            return
        selected = {row["key"] for row in self.selected_basket_entries()}
        current = self.basket_results.currentItem()
        current_key = current.data(Qt.ItemDataRole.UserRole)["key"] if current else None
        scroll = self.basket_results.verticalScrollBar().value()
        rows = self.basket_entries()
        self.basket_results.blockSignals(True)
        self.basket_results.clear()
        for row in rows:
            entry = dict(row, pinned=False, source=tr('中转篮'), seen_at=None)
            item = QListWidgetItem(entry["name"] + (tr('\n已移动或删除') if not entry["exists"] else "\n" + entry["path"]))
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(entry["name"] + "\n" + entry["path"])
            item.setSizeHint(QSize(0, 64))
            self.basket_results.addItem(item)
            if entry["key"] == current_key:
                self.basket_results.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
            item.setSelected(entry["key"] in selected)
        if rows and not self.basket_results.selectedItems():
            self.basket_results.setCurrentRow(0)
        self.basket_results.verticalScrollBar().setValue(scroll)
        self.basket_results.blockSignals(False)
        self.basket_results.setVisible(bool(rows))
        self.basket_overview.show()
        self.basket_toolbar.show()
        self.empty.setVisible(not rows)
        self.empty.setText(tr('中转篮里没有匹配的文件。') if self.search.text().strip() else
                           tr('先在文件夹选中文件并按 Ctrl+C，\n回到这里按 Ctrl+V，或点击“粘贴文件”。\n也可以直接把文件拖进窗口。'))
        missing = sum(not row["exists"] for row in rows)
        self.basket_summary.setText(tr('{p0} 个暂存', p0=self.basket.count) + (tr(' · {p0} 个暂不可用', p0=missing) if missing else ""))
        self.basket_summary.setToolTip(tr('仅保留文件路径；关闭软件和休眠后仍会保留篮子。'))
        self._update_hint()
        self.update_basket_actions()

    def update_basket_actions(self):
        if not hasattr(self, "basket_clear_button"):
            return
        locked = self.files_locked
        selected = self.selected_basket_entries()
        one_available = len(selected) == 1 and selected[0]["exists"]
        available = False if locked else bool(self.basket.existing_paths())
        self.basket_drag_all_button.setEnabled(available)
        self.basket_copy_all_button.setEnabled(available)
        self.basket_paste_button.setEnabled(not locked)
        self.basket_add_button.setEnabled(not locked)
        self.basket_open_button.setEnabled(not locked and one_available)
        self.basket_reveal_button.setEnabled(not locked and one_available)
        self.basket_remove_button.setEnabled(not locked and bool(selected))
        self.basket_clear_button.setEnabled(not locked and bool(self.basket_count))

    def add_basket_paths(self, paths):
        if self.files_locked:
            return
        try:
            result = self.basket.add_paths(paths)
            self.tabs.setCurrentIndex(2)
            self.refresh()
            details = [tr('已暂存 {p0} 个文件', p0=result.added)]
            if result.duplicates:
                details.append(tr('{p0} 个已在篮中', p0=result.duplicates))
            if result.skipped:
                details.append(tr('跳过 {p0} 个非本地文件、文件夹或无效项目', p0=result.skipped))
            if result.full:
                details.append(tr('篮子已满，{p0} 个未加入', p0=result.full))
            self.status.setText(tr('；').join(details) + tr('。原文件仍在原位置。'))
            return result
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法加入中转篮：{p0}', p0=exc))

    def paste_basket_files(self):
        """Read explicit file URLs only, on the user's paste action."""
        if self.files_locked:
            return
        paths = self.drop_paths(QApplication.clipboard().mimeData())
        if not paths:
            self.status.setText(tr('剪贴板中没有文件。请在文件夹选中文件并按 Ctrl+C，再回到这里粘贴。'))
            return
        return self.add_basket_paths(paths)

    def _handle_file_paste(self):
        """Route copied files from any tab, leaving ordinary search text alone."""
        if self.files_locked:
            # The protected tabs never inspect a clipboard while locked.
            # The independent content search can still use its normal paste.
            return self.protected_mode
        paths = self.drop_paths(QApplication.clipboard().mimeData())
        if not paths:
            return False
        self.add_basket_paths(paths)
        return True

    def choose_basket_files(self):
        if self.files_locked:
            return
        self._interaction_depth += 1
        try:
            paths, _ = QFileDialog.getOpenFileNames(self, tr('添加到文件中转篮'), "", tr('所有文件 (*)'))
            if paths and not self.files_locked:
                self.add_basket_paths(paths)
        finally:
            self._interaction_depth -= 1
            self.request_refresh()

    def _basket_paths(self, all_items=False):
        if self.files_locked:
            raise ValueError(tr('最近文件与中转篮已锁定'))
        selected = self.selected_basket_entries()
        if not all_items and not selected:
            raise ValueError(tr('请先选择文件'))
        keys = None if all_items else [entry["key"] for entry in selected]
        paths = self.basket.existing_paths(keys)
        if not paths:
            raise ValueError(tr('文件已移动、删除或暂时无法访问'))
        return paths

    def copy_basket(self, all_items=False):
        if self.files_locked:
            return
        try:
            paths = self._basket_paths(all_items)
            mime = file_mime_data(paths)
            self.host._copy_owned(lambda: QApplication.clipboard().setMimeData(mime))
            total = self.basket.count if all_items else len(self.selected_basket_entries())
            missing = max(0, total - len(paths))
            skipped = tr('跳过 {p0} 个暂不可用文件，引用仍保留。', p0=missing) if missing else ""
            self.status.setText(tr('已复制 {p0} 个可用文件，可在目标窗口按 Ctrl+V。{p1}原文件不变。', p0=len(paths), p1=skipped))
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法复制：{p0}', p0=exc))
        self.request_refresh()

    def start_basket_drag(self, all_items=False):
        if self.files_locked:
            return
        try:
            paths = self._basket_paths(all_items)
            mime = file_mime_data(paths)
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法拖出：{p0}', p0=exc))
            self.request_refresh()
            return
        drag = QDrag(self.basket_drag_all_button if all_items else self.basket_results)
        drag.setMimeData(mime)
        self._dragging = True
        try:
            result = drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)
        finally:
            self._dragging = False
            self.basket_drag_all_button.setDown(False)
            drag.deleteLater()
        if self.files_locked:
            self._clear_file_details()
            return
        if result != Qt.DropAction.IgnoreAction:
            total = self.basket.count if all_items else len(self.selected_basket_entries())
            missing = max(0, total - len(paths))
            skipped = tr('跳过 {p0} 个暂不可用文件。', p0=missing) if missing else ""
            self.status.setText(tr('已拖出 {p0} 个文件入口。{p1}篮子保留，原文件不移动。', p0=len(paths), p1=skipped))
            if not self.keep_open.isChecked():
                self.collapse()
        self.request_refresh()

    def _basket_current_path(self):
        paths = self._basket_paths()
        if len(self.selected_basket_entries()) != 1 or len(paths) != 1:
            raise ValueError(tr('请选择一个可用文件'))
        return paths[0]

    def open_basket_current(self):
        if self.files_locked:
            return
        try:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._basket_current_path()))):
                raise OSError(tr('系统未能打开文件，请检查默认应用'))
            self.status.setText(tr('已请求系统默认应用打开文件。'))
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法打开：{p0}', p0=exc))
            self.request_refresh()

    def reveal_basket_current(self):
        if self.files_locked:
            return
        try:
            if not reveal_file(self._basket_current_path()):
                raise OSError(tr('系统未能打开所在文件夹'))
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法定位：{p0}', p0=exc))
            self.request_refresh()

    def remove_basket_entries(self):
        if self.files_locked:
            return
        keys = [entry["key"] for entry in self.selected_basket_entries()]
        try:
            count = self.basket.remove(keys)
            self.refresh()
            self.status.setText(tr('已从中转篮移除 {p0} 个引用，原文件未删除。', p0=count))
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法移除：{p0}', p0=exc))

    def clear_basket(self):
        if self.files_locked:
            return
        try:
            count = self.basket.clear()
            self.refresh()
            self.status.setText(tr('已清空 {p0} 个暂存引用，原文件未删除。', p0=count))
        except (OSError, ValueError) as exc:
            self.status.setText(tr('无法清空：{p0}', p0=exc))

    def basket_menu(self, position):
        menu = QMenu(self)
        if self.files_locked:
            menu.addAction(tr('最近文件与中转篮已锁定')).setEnabled(False)
            menu.addAction(tr('使用 Windows Hello 解锁'), self.unlock_files)
        else:
            selected = self.selected_basket_entries()
            one = len(selected) == 1 and selected[0]["exists"]
            menu.addAction(tr('打开文件'), self.open_basket_current).setEnabled(one)
            menu.addAction(tr('定位文件夹'), self.reveal_basket_current).setEnabled(one)
            menu.addSeparator()
            menu.addAction(tr('复制选中'), self.copy_basket).setEnabled(bool(selected))
            menu.addAction(tr('复制全部'), lambda: self.copy_basket(all_items=True)).setEnabled(bool(self.basket_count))
            menu.addSeparator()
            menu.addAction(tr('移除选中（不删原文件）'), self.remove_basket_entries).setEnabled(bool(selected))
            menu.addAction(tr('清空篮子（不删原文件）'), self.clear_basket).setEnabled(bool(self.basket_count))
        point = (self.basket_results.viewport().mapToGlobal(position) if position is not None else
                 self.header.more_button.mapToGlobal(self.header.more_button.rect().bottomLeft()))
        self.run_menu(menu, point)

    @staticmethod
    def drop_paths(mime):
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()] if mime is not None and mime.hasUrls() else []

    def add_paths(self, paths):
        if self.files_locked:
            return
        try:
            count = self.host.recent_files.add_paths(paths)
            self.tabs.setCurrentIndex(1)
            self.source_filter.setCurrentIndex(self.source_filter.findData("external"))
            self.status.setText(tr('已添加 {p0} 个文件入口，原文件保留在原位置。', p0=count))
            self.refresh()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def dragEnterEvent(self, event):
        if (not self.files_locked and self.drop_paths(event.mimeData())
                and event.source() not in (self.file_results, self.basket_results)
                and event.possibleActions() & Qt.DropAction.CopyAction):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self.drop_paths(event.mimeData())
        if (not self.files_locked and paths and event.source() not in (self.file_results, self.basket_results)
                and event.possibleActions() & Qt.DropAction.CopyAction):
            if self.add_basket_paths(paths) is None:
                event.ignore()
                return
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def _move(self, step):
        if self.basket_mode:
            if self.basket_results.count():
                self.basket_results.setCurrentRow(max(0, min(self.basket_results.count() - 1,
                                                             self.basket_results.currentRow() + step)))
            return
        if not self.files_mode:
            return super()._move(step)
        if self.file_results.count():
            self.file_results.setCurrentRow(max(0, min(self.file_results.count() - 1, self.file_results.currentRow() + step)))

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            if (event.key() == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier
                    and self._handle_file_paste()):
                return True
            if event.key() == Qt.Key.Key_Tab and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % self.tabs.count())
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.collapse()
                return True
            if self.basket_mode:
                if self.files_locked:
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                        self.copy_basket()
                    elif event.modifiers() & Qt.KeyboardModifier.AltModifier:
                        self.reveal_basket_current()
                    else:
                        self.open_basket_current()
                    return True
                if event.key() == Qt.Key.Key_Delete:
                    self.remove_basket_entries()
                    return True
                if watched is self.basket_results and event.key() in (
                        Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                    return False
            if self.files_mode and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                    self.reveal_current()
                elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    self.accept_current(paste=False)
                else:
                    self.open_current()
                return True
            if self.files_mode and event.key() == Qt.Key.Key_P and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.toggle_pin()
                return True
            # Let the file list keep Shift+arrow range selection.
            if watched is getattr(self, "file_results", None) and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                return False
        return super().eventFilter(watched, event)

    def _hide_if_inactive(self):
        if (not self.isActiveWindow() and not self._dragging and not self._interaction_depth
                and not self.authentication_busy and not self._capture_suspended and not self._window_moved and not self.keep_open.isChecked()):
            self.collapse()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.collapse()
        elif (event.key() == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier
              and self._handle_file_paste()):
            event.accept()
        else:
            super().keyPressEvent(event)

    def _remove_folder(self, path):
        if not self.files_locked:
            service = self.host.recent_files
            service.set_folders([folder for folder in service.folders() if folder != path])
