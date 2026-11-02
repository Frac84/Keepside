from __future__ import annotations
from .i18n import tr, language
import ctypes
from datetime import datetime, timedelta
from pathlib import Path
import re
import shutil
import time
from PySide6.QtCore import Qt, QSize, QRectF, QTimer, QUrl, Signal, QItemSelectionModel, QEvent
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QImage, QPainter, QPen, QPixmap, QKeySequence, QShortcut, QTextCharFormat, QImageReader
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel, QPushButton, QLineEdit, QListWidget, QListWidgetItem, QAbstractItemView, QStyledItemDelegate, QStyle, QFrame, QSplitter, QTextEdit, QSystemTrayIcon, QMenu, QFileDialog, QMessageBox, QDialog, QFormLayout, QComboBox, QDialogButtonBox, QGraphicsView, QGraphicsScene, QCheckBox, QTabWidget, QStackedWidget, QSizePolicy, QScrollArea
from .workers import CaptureWorker, HeadTrainer, OcrWorker, TopicWorker
from .storage import safe_title
from .tab_scroller import TabScroller, FOCUS_DELAYS
from .theme import STYLE, apply_theme, native_titlebar, normalize_theme, theme
from .gallery import CardDelegate, GalleryView, glyph
from .branding import app_name, app_tagline, app_icon
from .clipboard_policy import DEFAULT_EXCLUDED_APPS, app_excluded, clipboard_source_app, normalize_app_list, sensitive_text_reason, should_exclude_clipboard
from .classification import TOPIC_LABELS, LEGACY_TOPIC_LABELS, ALL_TOPIC_LABELS, is_code_content
from .recent_files import RecentFiles
from .file_basket import FileBasket
from .glass import configure_window_glass, refresh_window_glass
from .window_chrome import MainWindowChrome
from .screenshot_preferences import PREFERENCE_KEY, REGION_KEY, normalize_preferences, normalize_region
from . import hotkey as global_hotkey
MIN_TRAIN_EXAMPLES = 10
QUICK_HOTKEY_ID = 21334
PIN_HOTKEY_ID = 21335
SHOT_HOTKEY_ID = 21336
FULL_HOTKEY_ID = 21337
_URL = re.compile("https?://[A-Za-z0-9\\-._~:/?#\\[\\]@!$&'*+,;=%()]+", re.I)

def extract_urls(text, limit=20):
    """HTTP(S) addresses written in text, without trailing punctuation."""
    urls = []
    for match in _URL.finditer(text or ''):
        url = match.group(0).rstrip('.,;:!?\'"')
        while url.endswith(')') and url.count(')') > url.count('('):
            url = url[:-1]
        if len(url) > 10 and url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls

def openable_url(value):
    url = QUrl(value.strip())
    return url.scheme().lower() in ('http', 'https') and url.isValid() and bool(url.host()) and (not any((char in value for char in '\r\n\t ')))
TOPIC_FILTER_LABELS = {'unknown': '待整理', **ALL_TOPIC_LABELS}
TYPE_LABELS = {'image': '图片', 'text': '文字', 'link': '链接'}

def label(text, name=None):
    widget = QLabel(tr(text))
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget

def button(text, callback, name=None, icon_name=None):
    widget = QPushButton(tr(text))
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(callback)
    if icon_name:
        widget.setProperty('themeIcon', icon_name)
        widget.setProperty('themePrimary', name == 'primary')
        widget.setIcon(glyph(icon_name, theme()['on_action'] if name == 'primary' else theme()['secondary']))
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget

def navigation_button(title, callback, icon_name):
    title = tr(title)
    widget = button('', callback, 'navItem')
    widget.setAccessibleName(title)
    widget.setCheckable(True)
    widget.setFixedHeight(39)
    row = QHBoxLayout(widget)
    row.setContentsMargins(12, 0, 12, 0)
    row.setSpacing(11)
    icon = label('')
    icon.setProperty('themeGlyph', icon_name)
    icon.setPixmap(glyph(icon_name, theme()['muted'], 16).pixmap(16, 16))
    row.addWidget(icon)
    row.addWidget(label(title), 1)
    count = label('', 'navCount')
    row.addWidget(count)
    for child in widget.findChildren(QLabel):
        child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return (widget, count)

def update_navigation_count(widget, count, value):
    count.setText(f'{value:,}')
    for child in widget.findChildren(QLabel):
        child.setProperty('navSelected', widget.isChecked())
        child.style().unpolish(child)
        child.style().polish(child)

class TopicDialog(QDialog):
    """A small explicit editor: save zero, one or two locally stored labels."""

    def __init__(self, tags=(), count=1, parent=None):
        super().__init__(parent)
        configure_window_glass(self)
        self.setWindowTitle(tr('修改主题标签') if count == 1 else tr('修改 {count} 条内容的标签', count=count))
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        description = label((tr('统一替换所选内容的标签。') if count > 1 else '') + tr('最多选择两个主题；不选择标签则保留在「待整理」。'), 'muted')
        description.setWordWrap(True)
        layout.addWidget(description)
        self.checks = {}
        selected = set(tags)
        choices = {**TOPIC_LABELS, **{key: title for key, title in LEGACY_TOPIC_LABELS.items() if key in selected}}
        for key, title in choices.items():
            check = QCheckBox(tr(title))
            check.setChecked(key in selected)
            check.toggled.connect(self.update_selection)
            layout.addWidget(check)
            self.checks[key] = check
        self.selection_hint = label('', 'muted')
        layout.addWidget(self.selection_hint)
        clear = button(tr('清空标签 · 放入待整理'), self.clear_tags)
        layout.addWidget(clear)
        self.actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.actions.button(QDialogButtonBox.StandardButton.Ok).setText(tr('保存标签'))
        self.actions.button(QDialogButtonBox.StandardButton.Cancel).setText(tr('取消'))
        self.actions.accepted.connect(self.accept)
        self.actions.rejected.connect(self.reject)
        layout.addWidget(self.actions)
        self.update_selection()

    def tags(self):
        return [key for key, check in self.checks.items() if check.isChecked()]

    def update_selection(self, *_):
        count = len(self.tags())
        self.selection_hint.setText(tr('已选 %d / 2') % count if count <= 2 else tr('最多选择两个主题，请取消一个。'))
        self.actions.button(QDialogButtonBox.StandardButton.Ok).setEnabled(count <= 2)

    def clear_tags(self):
        for check in self.checks.values():
            check.setChecked(False)

    def showEvent(self, event):
        super().showEvent(event)
        native_titlebar(self)
        refresh_window_glass(self, theme()['dark'], enabled=theme()['glass'])

class MainWindow(QMainWindow):
    capture_state_changed = Signal(bool)
    tabs_state_changed = Signal(bool)
    text_capture_state_changed = Signal(bool)
    theme_changed = Signal(str)
    hotkeys_state_changed = Signal()
    classif_requested = Signal(object)

    def __init__(self, vault, listen=True, use_tray=True, browser_tools=True, classify=True, hotkeys=None):
        super().__init__()
        self.window_chrome = MainWindowChrome(self)
        configure_window_glass(self, integrated_titlebar=True)
        self.vault = vault
        self.retention_days = vault.setting('retention_days', 7)
        self.excluded_apps = normalize_app_list(vault.setting('excluded_apps', list(DEFAULT_EXCLUDED_APPS)))
        self.skip_sensitive = bool(vault.setting('skip_sensitive_text', True))
        self.quick_hotkey = vault.setting('quick_paste_hotkey', global_hotkey.DEFAULT_QUICK_HOTKEY)
        self.quick_autopaste = bool(vault.setting('quick_paste_autopaste', True))
        self.restore_pins_enabled = bool(vault.setting('restore_pins', True))
        self.hotkeys_enabled = use_tray if hotkeys is None else bool(hotkeys)
        self._hotkeys_suspended = False
        self.quick_key = global_hotkey.GlobalHotkey(QUICK_HOTKEY_ID)
        self.pin_hotkey = vault.setting('pin_clipboard_hotkey', global_hotkey.DEFAULT_PIN_HOTKEY)
        self.pin_key = global_hotkey.GlobalHotkey(PIN_HOTKEY_ID)
        self.full_hotkey = vault.setting('fullscreen_hotkey', global_hotkey.DEFAULT_FULLSCREEN_HOTKEY)
        self.full_key = global_hotkey.GlobalHotkey(FULL_HOTKEY_ID)
        legacy = vault.setting('screenshot_hotkey', '')
        self.shot_hotkey = vault.setting('region_hotkey', legacy if legacy != global_hotkey.DEFAULT_FULLSCREEN_HOTKEY else global_hotkey.DEFAULT_REGION_HOTKEY)
        self.shot_key = global_hotkey.GlobalHotkey(SHOT_HOTKEY_ID)
        self._hotkey_rebind_timer = QTimer(self)
        self._hotkey_rebind_timer.setSingleShot(True)
        self._hotkey_rebind_timer.timeout.connect(self._restore_hotkey_window)
        self.grab_fullscreen = None
        self.shot_session = None
        self.screenshot_preferences = normalize_preferences(vault.setting(PREFERENCE_KEY, {}))
        self.last_screenshot_region = normalize_region(vault.setting(REGION_KEY, None))
        self.grab_screens = None
        self._pending_pins, self._pin_token = ({}, 0)
        self.quick_panel = None
        self.image_tool_dialogs = []
        self.screenshot_text_windows = []
        self._pending_screenshot_text, self._screenshot_text_token = ({}, 0)
        self._transient_screenshot_text = {}
        self.inspector_window = None
        self.recent_files = RecentFiles(vault, self, directories=None if use_tray else [], watch=use_tray)
        self.file_basket = FileBasket(vault, self)
        self.trainer = None
        self.similar_groups, self.similar_sizes, self.similar_members = ([], {}, {})
        self._similar_signature, self._similar_time = (None, 0.0)
        self._codes_key = None
        self.theme_id = apply_theme(QApplication.instance(), normalize_theme(vault.setting('theme', 'light')))
        self.scope = 'all'
        self.kind = 'all'
        self.item_type = 'all'
        self.topic = 'all'
        self.collect_text = bool(vault.setting('collect_text', False))
        self.page = 'library'
        self.preferences_panel = None
        self.pinned_windows = []
        self.rows_limit = 120
        self.active_id = None
        self.ocr_languages = []
        self.ocr_error = ''
        self.quitting = False
        self.tab_scroller = None
        self.browser_tools = browser_tools
        self.tabs_enabled = vault.setting('tabs_enabled', True)
        self.tabs_reverse = vault.setting('tabs_reverse', False)
        self.tabs_double_click = vault.setting('tabs_double_click', True)
        self.tabs_autofocus = vault.setting('tabs_autofocus', True)
        self.tabs_focus_mode = vault.setting('tabs_focus_mode', 2)
        self.tabs_focus_delay = vault.setting('tabs_focus_delay', 25)
        self.listening = listen and (not vault.setting('paused', False))
        self.last_sequence = self.sequence()
        self.clip_generation = 0
        self.own_copy = False
        self.own_copy_sequence = 0
        self.setWindowTitle(f'{app_name()} · {app_tagline()}')
        self.setWindowIcon(app_icon())
        self.resize(1440, 900)
        self.setMinimumSize(1080, 700)
        self.setAcceptDrops(True)
        self.capture = CaptureWorker(vault)
        self.ocr = OcrWorker(vault)
        self.classifier = TopicWorker(vault) if classify else None
        if self.classifier:
            self.classifier.changed.connect(self.on_classified)
            self.classifier.failed.connect(lambda message: self.statusBar().showMessage(message, 10000))
            self.classif_requested.connect(self.classifier.enqueue_pending)
        self.capture.saved.connect(self.on_saved)
        self.capture.failed.connect(self.report_error)
        self.capture.failed.connect(self._screenshot_save_failed)
        self.capture.cleaned.connect(self.on_cleaned)
        if hasattr(self.capture, 'tagged'):
            self.capture.tagged.connect(self._pin_saved)
            self.capture.tagged.connect(self._screenshot_saved)
        if hasattr(self.capture, 'hashed'):
            self.capture.hashed.connect(lambda count: self.refresh_timer.start() if count else None)
        self.ocr.changed.connect(self.on_ocr)
        if hasattr(self.ocr, 'transient'):
            self.ocr.transient.connect(self._screenshot_transient_result)
        self.ocr.availability.connect(self.on_availability)
        self.build_ui()
        self.build_tray(use_tray)
        if browser_tools:
            self.tab_scroller = TabScroller(QApplication.instance(), int(self.winId()), self.on_tab_error)
            if hasattr(self.tab_scroller, 'state_changed'):
                self.tab_scroller.state_changed.connect(self.update_tabs_state)
        self.apply_tabs_settings()
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(150)
        self.refresh_timer.timeout.connect(self.refresh)
        self.cleanup_timer = QTimer(self)
        self.cleanup_timer.setInterval(60 * 60 * 1000)
        self.cleanup_timer.timeout.connect(lambda: self.capture.submit('cleanup'))
        self.cleanup_timer.start()
        QApplication.clipboard().dataChanged.connect(self.clipboard_changed)
        self.capture.start()
        self.ocr.start()
        if self.classifier:
            self.classifier.start()
            self.classifier.enqueue_pending()
        self.capture.submit('cleanup')
        self.capture.submit('hashes')
        for key in vault.pending():
            self.ocr.enqueue(key)
        QShortcut(QKeySequence('Ctrl+F'), self, activated=self.focus_search)
        QShortcut(QKeySequence('Ctrl+V'), self, activated=self.paste_from_window)
        QShortcut(QKeySequence('Ctrl+Shift+B'), self, activated=self.show_file_basket)
        QShortcut(QKeySequence('Ctrl+O'), self, activated=self.import_images)
        QShortcut(QKeySequence('Ctrl+Q'), self, activated=self.quit_app)
        QShortcut(QKeySequence('Ctrl+P'), self, activated=self.pin_image)
        self.refresh()
        self.update_capture_state()
        self.apply_quick_settings()
        if self.restore_pins_enabled and use_tray:
            QTimer.singleShot(0, self.restore_pins)
        native_titlebar(self, self.theme_id)
        if use_tray and vault.setting('shelf_ball_enabled', True):
            QTimer.singleShot(300, self.show_floating_ball)

    def nativeEvent(self, event_type, message):
        if bytes(event_type) == b'windows_generic_MSG' and message is not None and int(message):
            from ctypes import wintypes
            native_message = wintypes.MSG.from_address(int(message))
            chrome = getattr(self, 'window_chrome', None)
            if chrome is not None:
                handled, result = chrome.handle_native_message(native_message)
                if handled:
                    return (True, result)
            kind = native_message.message
            if kind in (26, 794, 798):
                self._window_material_controller.schedule()
        keys = (self.quick_key, self.pin_key, self.shot_key, self.full_key)
        if any((key.active for key in keys)) and bytes(event_type) == b'windows_generic_MSG':
            hotkey_id = global_hotkey.hotkey_message_id(message)
            if hotkey_id == FULL_HOTKEY_ID and self.full_key.active:
                QTimer.singleShot(0, self.take_fullscreen)
                return (True, 0)
            if hotkey_id == SHOT_HOTKEY_ID and self.shot_key.active:
                QTimer.singleShot(0, self.start_screenshot)
                return (True, 0)
            if hotkey_id == QUICK_HOTKEY_ID and self.quick_key.active:
                QTimer.singleShot(0, self.toggle_quick_paste)
                return (True, 0)
            if hotkey_id == PIN_HOTKEY_ID and self.pin_key.active:
                QTimer.singleShot(0, self.pin_clipboard)
                return (True, 0)
        return (False, 0)

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.WinIdChange and hasattr(self, '_hotkey_rebind_timer') and (not getattr(self, 'quitting', False)):
            self._hotkey_rebind_timer.start(0)
        return result

    def _restore_hotkey_window(self):
        if self.quitting or not self.hotkeys_enabled or self._hotkeys_suspended:
            return
        hwnd = int(self.winId())
        pairs = ((self.quick_key, self.quick_hotkey), (self.pin_key, self.pin_hotkey), (self.full_key, self.full_hotkey), (self.shot_key, self.shot_hotkey))
        if any((shortcut.active and shortcut.hwnd != hwnd for shortcut, _value in pairs)):
            self.apply_quick_settings(report=False)

    def suspend_hotkeys(self, suspended):
        """Release every global shortcut while settings record a new key."""
        self._hotkeys_suspended = bool(suspended)
        if suspended:
            for shortcut in (self.quick_key, self.pin_key, self.full_key, self.shot_key):
                shortcut.unregister()
            self.hotkeys_state_changed.emit()
        else:
            self.apply_quick_settings(report=False)

    def apply_quick_settings(self, report=True):
        """(Re)register the global shortcuts after startup or a settings change."""
        errors = []
        if self.hotkeys_enabled and (not self._hotkeys_suspended) and (not self.quitting):
            for name, shortcut, value in ((tr('快速粘贴'), self.quick_key, self.quick_hotkey), (tr('贴图'), self.pin_key, self.pin_hotkey), (tr('全屏截图'), self.full_key, self.full_hotkey), (tr('区域截图'), self.shot_key, self.shot_hotkey)):
                shortcut.register(int(self.winId()), value)
                if shortcut.error:
                    errors.append(f'{name}：{shortcut.error}')
            if errors and report:
                self.report_error('；'.join(errors))
        else:
            for shortcut in (self.quick_key, self.pin_key, self.full_key, self.shot_key):
                shortcut.unregister()
        if self.tray:
            shortcut = global_hotkey.hotkey_label(self.quick_hotkey) if self.quick_key.active else tr('无快捷键')
            self.tray_quick.setText(tr('随手面板（{shortcut}）', shortcut=shortcut))
            shortcut = global_hotkey.hotkey_label(self.pin_hotkey) if self.pin_key.active else tr('无快捷键')
            self.tray_pin_clipboard.setText(tr('把剪贴板图片贴到屏幕（{shortcut}）', shortcut=shortcut))
            shortcut = global_hotkey.hotkey_label(self.full_hotkey) if self.full_key.active else tr('无快捷键')
            self.tray_full.setText(tr('全屏截图（{shortcut}）', shortcut=shortcut))
            shortcut = global_hotkey.hotkey_label(self.shot_hotkey) if self.shot_key.active else tr('无快捷键')
            self.tray_shot.setText(tr('区域截图（{shortcut}）', shortcut=shortcut))
        self.hotkeys_state_changed.emit()
        return errors

    def toggle_quick_paste(self):
        if self.quitting:
            return
        from .recent_panel import ShelfPanel
        if self.quick_panel is None:
            self.quick_panel = ShelfPanel(self)
        if self.quick_panel.isVisible():
            self.quick_panel.collapse()
        else:
            self.quick_panel.open()

    def show_recent_files(self):
        if self.quitting:
            return
        from .recent_panel import ShelfPanel
        if self.quick_panel is None:
            self.quick_panel = ShelfPanel(self)
        self.quick_panel.open(files=True)

    def lock_recent_files(self):
        if self.quitting:
            return
        from .recent_panel import ShelfPanel
        if self.quick_panel is None:
            self.quick_panel = ShelfPanel(self)
        if self.quick_panel.files_locked:
            self.quick_panel.open(files=True)
        elif self.quick_panel.lock_files():
            self.quick_panel.collapse()
        else:
            error = self.quick_panel.last_lock_error
            self.quick_panel.open(files=True)
            self.quick_panel.status.setText(error)

    def show_file_basket(self):
        if self.quitting:
            return
        from .recent_panel import ShelfPanel
        if self.quick_panel is None:
            self.quick_panel = ShelfPanel(self)
        self.quick_panel.open_basket()

    def paste_from_window(self):
        """File-manager copies go straight to the basket; image/text keep their flow."""
        if self.quitting:
            return
        mime = QApplication.clipboard().mimeData()
        if mime is not None and mime.hasUrls() and any((url.isLocalFile() for url in mime.urls())):
            self.show_file_basket()
            self.quick_panel.paste_basket_files()
        else:
            self.capture_current()

    def show_floating_ball(self):
        if self.quitting:
            return
        from .recent_panel import ShelfPanel
        if self.quick_panel is None:
            self.quick_panel = ShelfPanel(self)
        if self.quick_panel.isVisible():
            self.quick_panel.collapse()
        else:
            self.quick_panel.show_ball(enable=True)

    def copy_row(self, row):
        """Copy one saved item without re-collecting it; used by quick paste."""
        if row.get('item_type', 'image') == 'image':
            image = QImage(str(self.vault.images / row['filename']))
            if image.isNull():
                self.report_error(tr('原图不存在或无法读取'))
                return False
            self._copy_owned(lambda: QApplication.clipboard().setImage(image))
        else:
            text = row.get('content', '')
            self._copy_owned(lambda: QApplication.clipboard().setText(text))
        self.statusBar().showMessage(tr('已复制到剪贴板'), 4000)
        return True

    def copy_plain_text(self, text):
        self._copy_owned(lambda: QApplication.clipboard().setText(text))

    def notify(self, message):
        """Status message that is still seen when the window is in the tray."""
        self.statusBar().showMessage(message, 6000)
        if self.tray and (not self.isVisible()):
            self.tray.showMessage(app_name(), message, QSystemTrayIcon.MessageIcon.Information, 4000)

    def pin_clipboard(self):
        """Pin the clipboard image at once and save it like a manual paste."""
        if self.quitting:
            return None
        clipboard = QApplication.clipboard()
        image = clipboard.image()
        if image.isNull():
            self.notify(tr('剪贴板里没有图片；截图或复制图片后再按快捷键。'))
            return None
        if image.width() * image.height() > 80000000:
            self.report_error(tr('图片过大（超过 8000 万像素），无法贴图。'))
            return None
        mime = clipboard.mimeData()
        source = clipboard_source_app(foreground_fallback=False)
        excluded = should_exclude_clipboard(mime) or app_excluded(source, self.excluded_apps)
        keep = not excluded and self.screenshot_preferences['copy_save']
        pin = self._pin_and_save(image, tr('剪贴板贴图'), save=keep)
        if pin is not None:
            self.notify(tr('已贴到屏幕。这张图带有不收集标记或来自已排除的程序，不会存入图库。') if excluded else tr('已贴到屏幕，并保存到图库') if keep else tr('已临时贴到屏幕，未存入图库'))
        return pin

    def _pin_and_save(self, image, title, save=True):
        from .pinboard import PinnedImageWindow
        try:
            pin = PinnedImageWindow(pixmap=image, title=title)
        except (OSError, ValueError) as exc:
            self.report_error(tr('无法贴图：{exc}', exc=exc))
            return None
        self._show_pin(pin)
        if save:
            self._pin_token += 1
            self._pending_pins[self._pin_token] = pin
            self.capture.submit('image', image.copy(), tag=self._pin_token)
        return pin

    def take_fullscreen(self):
        """One key, no selection: the screen under the mouse is saved and copied."""
        if self.quitting:
            return None
        from .screenshot import Toast, grab_screen_at_cursor
        overlay = self.quick_panel
        overlay_state = overlay.suspend_for_capture() if overlay is not None else None
        try:
            pixmap = (self.grab_fullscreen or grab_screen_at_cursor)()
        finally:
            if overlay is not None:
                overlay.resume_after_capture(overlay_state)
        if pixmap is None or pixmap.isNull():
            self.report_error(tr('无法截取屏幕画面。'))
            return None
        image = pixmap.toImage()
        image.setDevicePixelRatio(1.0)
        self._copy_owned(lambda: QApplication.clipboard().setImage(image))
        keep = self.screenshot_preferences['copy_save']
        accepted = self.capture.submit('image', image.copy()) if keep else False
        stored = keep and accepted is not False
        self.statusBar().showMessage(tr('已截取全屏，已复制') + (tr('并保存到图库') if stored else tr('，未存入图库')), 5000)
        self.toast = Toast(tr('已截取全屏 {value_0}×{value_1} · ', value_0=image.width(), value_1=image.height()) + (tr('已复制并入库') if stored else tr('仅复制，未入库')))
        self.toast.show()
        return image

    def screenshot_from_window(self, *, annotate=False, repeat=False):
        """Toolbar button: step the library out of the way, then capture."""
        if repeat and (not self.last_screenshot_region):
            self.notify(tr('请先完成一次区域截图，再使用上次区域。'))
            return
        if self.tray:
            self.hide()
        else:
            self.showMinimized()
        QTimer.singleShot(300, lambda: self.start_screenshot(restore=True, annotate=annotate, repeat=repeat))

    def screenshot_annotation_from_window(self):
        self.screenshot_from_window(annotate=True)

    def start_screenshot(self, restore=False, annotate=False, repeat=False):
        if self.quitting or self.shot_session is not None:
            return False
        overlay = self.quick_panel
        self._shelf_region_state = overlay.suspend_for_capture() if overlay is not None else None
        from .screenshot import ScreenshotSession, grab_screens
        shots = (self.grab_screens or grab_screens)()
        if not shots:
            if overlay is not None:
                overlay.resume_after_capture(self._shelf_region_state)
                self._shelf_region_state = None
            self.report_error(tr('无法截取屏幕画面。'))
            if restore:
                self.show_window()
            return False
        self.shot_session = ScreenshotSession(shots, initial_tool='rectangle' if annotate and self.screenshot_preferences['tool'] == 'select' else None, preferences=self.screenshot_preferences, last_region=self.last_screenshot_region if repeat else None)
        self.shot_session.preferencesChanged.connect(self._update_screenshot_preferences)
        self.shot_session.regionSelected.connect(self._remember_screenshot_region)
        self.shot_session.captured.connect(self._screenshot_taken)
        self.shot_session.ended.connect(lambda: self._screenshot_ended(restore))
        return self.shot_session.start()

    def _update_screenshot_preferences(self, changes):
        preferences = normalize_preferences(changes, self.screenshot_preferences)
        if preferences != self.screenshot_preferences:
            self.screenshot_preferences = preferences
            self.vault.set_setting(PREFERENCE_KEY, preferences)

    def _remember_screenshot_region(self, region):
        value = normalize_region(region)
        if value is None:
            return
        self.last_screenshot_region = value
        self.vault.set_setting(REGION_KEY, value)
        self.repeat_screenshot_button.setEnabled(True)
        if hasattr(self, 'tray_repeat_shot'):
            self.tray_repeat_shot.setEnabled(True)

    def _screenshot_ended(self, restore):
        self.shot_session = None
        if self.quick_panel is not None:
            self.quick_panel.resume_after_capture(getattr(self, '_shelf_region_state', None))
            self._shelf_region_state = None
        if restore and (not self.quitting):
            self.show_window()

    def _screenshot_taken(self, image, action):
        if self.quitting:
            return
        if action == 'annotate':
            self.open_annotation(image)
            return
        if action == 'pin':
            keep = self.screenshot_preferences['copy_save']
            if self._pin_and_save(image, tr('截图贴图'), save=keep) is not None:
                self.notify(tr('截图已贴到屏幕，并保存到图库') if keep else tr('截图已临时贴到屏幕，未存入图库'))
            return
        if action == 'save_done':
            if self.screenshot_preferences['copy_save']:
                accepted = self.capture.submit('image', image.copy())
                self.notify(tr('截图已另存；保存队列已满，未入库。') if accepted is False else tr('截图已另存，并存入内容库。'))
            else:
                self.notify(tr('截图已另存，未存入内容库。'))
            return
        if action == 'save':
            self.save_screenshot(image)
            return
        if action == 'ocr':
            self.recognize_screenshot(image)
            return
        if action not in ('copy', 'copy_only'):
            self.report_error(tr('未知的截图操作，请重试。'))
            return
        self._copy_owned(lambda: QApplication.clipboard().setImage(image))
        self._update_screenshot_preferences({'copy_save': action == 'copy'})
        if action == 'copy_only':
            self.notify(tr('截图已复制，未存入内容库。'))
            return
        accepted = self.capture.submit('image', image.copy()) if self.screenshot_preferences['copy_save'] else False
        self.notify(tr('截图已复制；保存队列已满，未存入图库。') if accepted is False else tr('截图已复制到剪贴板，并保存到图库'))

    def save_screenshot(self, image):
        from .screenshot_output import write_screenshot_png
        filename, _ = QFileDialog.getSaveFileName(self, tr('截图另存为'), tr('截图_') + datetime.now().strftime('%Y%m%d_%H%M%S') + '.png', tr('PNG 图片 (*.png)'))
        if not filename:
            self.open_annotation(image)
            self.notify(tr('已取消另存，截图保留在编辑器，可继续标注或复制。'))
            return None
        try:
            path = write_screenshot_png(image, filename)
        except (OSError, ValueError) as exc:
            self.open_annotation(image)
            self.report_error(tr('保存截图失败：{exc}', exc=exc))
            return None
        accepted = self.capture.submit('image', image.copy()) if self.screenshot_preferences['copy_save'] else False
        self.notify(tr('截图已另存为 ') + path.name + (tr('；未存入图库。') if accepted is False else tr('，并保存到图库')))
        return path

    def recognize_screenshot(self, image):
        from .screenshot_output import ScreenshotTextDialog
        dialog = ScreenshotTextDialog(image, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.screenshot_text_windows.append(dialog)
        dialog.copyRequested.connect(self.copy_plain_text)
        dialog.retryRequested.connect(self.ocr.enqueue)
        dialog.finished.connect(lambda _result, current=dialog: self._release_screenshot_text(current))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._screenshot_text_token += 1
        tag = ('screenshot-ocr', self._screenshot_text_token)
        if not self.screenshot_preferences['copy_save']:
            dialog.set_transient()
            dialog._transient_image = image.copy()
            dialog.transientRetryRequested.connect(lambda current=dialog: self._queue_transient_ocr(current))
            self._queue_transient_ocr(dialog)
            return dialog
        self._pending_screenshot_text[tag] = dialog
        if not self.capture.submit('image', image.copy(), tag=tag):
            self._pending_screenshot_text.pop(tag, None)
            dialog.fail(tr('保存队列已满，请稍后重新截图。'))
        return dialog

    def _queue_transient_ocr(self, dialog):
        self._screenshot_text_token += 1
        tag = ('transient-screenshot', self._screenshot_text_token)
        self._transient_screenshot_text[tag] = dialog
        if not hasattr(self.ocr, 'enqueue_image') or not self.ocr.enqueue_image(dialog._transient_image, tag):
            self._transient_screenshot_text.pop(tag, None)
            dialog.fail(tr('本地识字队列暂不可用，请稍后重试。'))

    def _screenshot_transient_result(self, tag, result, error):
        dialog = self._transient_screenshot_text.pop(tag, None)
        if dialog is not None and (not self.quitting):
            dialog.update_transient(result, error)

    def _release_screenshot_text(self, dialog):
        if dialog in self.screenshot_text_windows:
            self.screenshot_text_windows.remove(dialog)
        self._pending_screenshot_text = {tag: current for tag, current in self._pending_screenshot_text.items() if current is not dialog}
        self._transient_screenshot_text = {tag: current for tag, current in self._transient_screenshot_text.items() if current is not dialog}

    def _screenshot_saved(self, tag, key, created):
        dialog = self._pending_screenshot_text.pop(tag, None)
        if dialog is None or self.quitting:
            return
        dialog.set_item(key)
        row = self.vault.get(key)
        dialog.update_result(row)
        if row and row.get('ocr_status') not in ('done', 'error') and (not created):
            self.ocr.enqueue(key)

    def _screenshot_save_failed(self, message):
        for dialog in list(self._pending_screenshot_text.values()):
            dialog.fail(message)
        self._pending_screenshot_text.clear()

    def _pin_saved(self, tag, key, created):
        pin = self._pending_pins.pop(tag, None)
        row = self.vault.get(key)
        if pin is None or row is None or pin not in self.pinned_windows:
            return
        pin.item_id = key
        pin.image_path = self.vault.images / row['filename']

    def set_theme(self, theme_id, persist=False):
        self.theme_id = apply_theme(QApplication.instance(), theme_id)
        if persist:
            self.vault.set_setting('theme', self.theme_id)
        p = theme(self.theme_id)
        self.search_action.setIcon(glyph('search', p['accent']))
        for control in self.findChildren(QPushButton):
            icon_name = control.property('themeIcon')
            if icon_name:
                control.setIcon(glyph(icon_name, p['on_action'] if control.property('themePrimary') else p['secondary']))
        for control in self.findChildren(QLabel):
            icon_name = control.property('themeGlyph')
            if icon_name:
                control.setPixmap(glyph(icon_name, p['muted'], 16).pixmap(16, 16))
        self.update_capture_state()
        self.update_tabs_state()
        self.update_detail()
        self.gallery.viewport().update()
        self.theme_changed.emit(self.theme_id)
        for window in QApplication.instance().topLevelWidgets():
            if window is not self and hasattr(window, 'refresh_theme'):
                window.refresh_theme()
        native_titlebar(self, self.theme_id)
        refresh_window_glass(self, p['dark'], self.centralWidget(), enabled=p['glass'])

    def build_ui(self):
        root = QWidget()
        root.setObjectName('appRoot')
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QFrame()
        header.setObjectName('appHeader')
        header.setFixedHeight(68)
        bar = QHBoxLayout(header)
        bar.setContentsMargins(22, 10, 22, 10)
        bar.setSpacing(9)
        logo = QLabel()
        logo.setPixmap(self.windowIcon().pixmap(30, 30))
        bar.addWidget(logo)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(label(app_name(), 'brand'))
        brand.addWidget(label(tr('本地内容库'), 'brandSubtitle'))
        bar.addLayout(brand)
        bar.addSpacing(18)
        self.pause_button = button('', self.toggle_pause, 'statusPill')
        self.pause_button.setToolTip(tr('点击暂停或恢复全部自动收集；手动粘贴仍然可用。'))
        bar.addWidget(self.pause_button)
        self.text_capture_button = button('', lambda: self.set_text_capture_enabled(not self.collect_text), 'statusPill')
        self.text_capture_button.setObjectName('textCaptureToggle')
        self.text_capture_button.setToolTip(tr('文字与链接自动收集默认关闭；点击开启或关闭，不会回采当前剪贴板。'))
        bar.addWidget(self.text_capture_button)
        self.tabs_button = button('', self.toggle_tabs, 'statusPill')
        self.tabs_button.setToolTip(tr('点击启停浏览器标签增强；双击关闭、方向和窗口恢复方式可在设置中调整。'))
        bar.addWidget(self.tabs_button)
        bar.addStretch()
        self.recent_button = button(tr('最近文件'), self.show_recent_files, 'headerNav', 'folder')
        self.recent_button.setToolTip(tr('打开最近文件浮台 · Alt+Shift+V 也能呼出随手面板'))
        bar.addWidget(self.recent_button)
        self.page_buttons = {}
        for key, title in (('library', tr('内容库')), ('status', tr('运行状态')), ('preferences', tr('设置'))):
            btn = button(title, lambda checked=False, k=key: self.show_page(k), 'headerNav')
            btn.setCheckable(True)
            btn.setChecked(key == 'library')
            bar.addWidget(btn)
            self.page_buttons[key] = btn
        self.window_chrome.attach_header(header, bar)
        outer.addWidget(header)
        body = QHBoxLayout()
        body.setSpacing(0)
        outer.addLayout(body, 1)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(204)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(12, 24, 12, 16)
        nav.setSpacing(3)
        nav.addWidget(label(tr('   浏览'), 'eyebrow'))
        nav.addSpacing(9)
        self.nav_buttons, self.nav_titles, self.nav_counts = ({}, {}, {})
        for key, title, icon in (('all', tr('全部内容'), 'grid'), ('today', tr('今天'), 'today'), ('yesterday', tr('昨天'), 'yesterday'), ('week', tr('最近 7 天'), 'week'), ('favorites', tr('永久收藏'), 'star'), ('similar', tr('相似截图'), 'layers')):
            btn, count = navigation_button(title, lambda checked=False, k=key: self.set_scope(k), icon)
            btn.setChecked(key == 'all')
            nav.addWidget(btn)
            self.nav_buttons[key], self.nav_titles[key] = (btn, title)
            self.nav_counts[key] = count
        nav.addSpacing(24)
        topics_header = QHBoxLayout()
        topics_header.addWidget(label(tr('   主题标签'), 'eyebrow'), 1)
        topics_header.addWidget(button(tr('全部'), lambda: self.set_topic('all'), 'categoryClear'))
        nav.addLayout(topics_header)
        self.topic_buttons, self.topic_counts = ({}, {})
        for key, title in TOPIC_FILTER_LABELS.items():
            btn, count = navigation_button(title, lambda checked=False, k=key: self.set_topic('all' if self.topic == k else k), 'folder')
            nav.addWidget(btn)
            self.topic_buttons[key] = btn
            self.topic_counts[key] = count
            if key in LEGACY_TOPIC_LABELS:
                btn.hide()
        self.reclassify_button = button(tr('重新整理自动标签'), lambda: self.reclassify_all(), 'folderButton', 'refresh')
        self.reclassify_button.setToolTip(tr('重新分析库内自动分类的内容；保留手动标签。'))
        nav.addWidget(self.reclassify_button)
        self.review_button = button(tr('整理待整理'), lambda: self.open_review('unknown'), 'folderButton', 'tabs')
        self.review_button.setToolTip(tr('逐条查看没有主题的内容，用数字键 1–7 打标签。'))
        nav.addWidget(self.review_button)
        nav.addStretch()
        storage = QFrame()
        storage.setObjectName('storageCard')
        storage_layout = QVBoxLayout(storage)
        storage_layout.setContentsMargins(14, 14, 14, 10)
        storage_layout.setSpacing(6)
        storage_layout.addWidget(label(tr('存储概览'), 'eyebrow'))
        self.storage_size = label('0 MB', 'storageSize')
        storage_layout.addWidget(self.storage_size)
        self.side_stats = label('', 'muted')
        self.side_stats.setWordWrap(True)
        storage_layout.addWidget(self.side_stats)
        storage_layout.addWidget(button(tr('打开数据目录'), self.open_data_folder, 'folderButton', 'folder'))
        nav.addWidget(storage)
        nav.addSpacing(9)
        nav.addWidget(label(tr('   所有数据留在本机'), 'eyebrow'))
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFixedWidth(204)
        sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_scroll.setWidget(sidebar)
        body.addWidget(sidebar_scroll)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        library = QWidget()
        content = QVBoxLayout(library)
        content.setContentsMargins(20, 18, 20, 12)
        content.setSpacing(16)
        self.pause_banner = QFrame()
        self.pause_banner.setObjectName('pauseBanner')
        pause_layout = QHBoxLayout(self.pause_banner)
        pause_layout.setContentsMargins(12, 5, 8, 5)
        pause_layout.addWidget(label(tr('自动收集已暂停，仍可手动粘贴内容或导入图片')))
        pause_layout.addStretch()
        pause_layout.addWidget(button(tr('恢复监听'), self.toggle_pause))
        content.addWidget(self.pause_banner)
        toolbar = QFrame()
        toolbar.setObjectName('toolbar')
        tools = QVBoxLayout(toolbar)
        tools.setContentsMargins(12, 12, 12, 10)
        tools.setSpacing(10)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName('gallerySearch')
        self.search.setMinimumWidth(190)
        self.search.setPlaceholderText(tr('搜索图片文字、原文或链接 · Ctrl+F'))
        self.search_action = self.search.addAction(glyph('search'), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(button(tr('粘贴内容'), self.paste_from_window, 'primary', 'clipboard'))
        search_row.addWidget(button(tr('导入图片'), self.import_images, icon_name='import'))
        shot_button = button(tr('区域截图'), self.screenshot_from_window, icon_name='image')
        shot_button.setObjectName('screenshotButton')
        shot_button.setToolTip(tr('隐藏窗口后框选屏幕区域；全屏截图请在任何地方按全屏截图快捷键'))
        search_row.addWidget(shot_button)
        reload_button = button('', self.refresh, icon_name='refresh')
        reload_button.setToolTip(tr('刷新内容库；拖入的文件会直接加入中转篮'))
        search_row.addWidget(reload_button)
        tools.addLayout(search_row)
        quick_actions = QHBoxLayout()
        quick_actions.setSpacing(8)
        self.basket_button = button(tr('文件中转篮'), self.show_file_basket, icon_name='folder')
        self.basket_button.setObjectName('openFileBasket')
        self.basket_button.setToolTip(tr('直接打开 · Ctrl+Shift+B；复制文件后在主窗口按 Ctrl+V 直接放入'))
        self.annotate_capture_button = button(tr('截图标注'), self.screenshot_annotation_from_window, icon_name='image')
        self.annotate_capture_button.setObjectName('captureAndAnnotate')
        self.annotate_capture_button.setToolTip(tr('框选屏幕后直接标注：箭头、文字、画笔和马赛克'))
        self.compress_file_button = button(tr('图片压缩…'), self.compress_local_image, icon_name='import')
        self.compress_file_button.setObjectName('compressLocalImage')
        self.compress_file_button.setToolTip(tr('选择图片，压缩到指定 KB / MB 后另存或复制文件'))
        self.repeat_screenshot_button = button(tr('再次截取上次区域'), lambda: self.screenshot_from_window(repeat=True), icon_name='image')
        self.repeat_screenshot_button.setObjectName('repeatScreenshot')
        self.repeat_screenshot_button.setToolTip(tr('重新冻结屏幕并恢复上次区域；确认后才输出'))
        self.repeat_screenshot_button.setEnabled(bool(self.last_screenshot_region))
        for control in (self.basket_button, self.annotate_capture_button, self.compress_file_button, self.repeat_screenshot_button):
            quick_actions.addWidget(control)
        quick_actions.addStretch()
        tools.addLayout(quick_actions)
        filters = QHBoxLayout()
        filters.setSpacing(5)
        self.filter_buttons = {}
        self.type_filter = QComboBox()
        self.type_filter.setObjectName('itemTypeFilter')
        for key, title in (('all', tr('全部类型')), ('image', tr('图片')), ('text', tr('文字')), ('link', tr('链接'))):
            self.type_filter.addItem(title, key)
        self.type_filter.currentIndexChanged.connect(lambda: self.set_item_type(self.type_filter.currentData()))
        filters.addWidget(self.type_filter)
        for key, title in (('all', tr('全部')), ('text', tr('含文字')), ('pending', tr('识别中')), ('error', tr('识别失败')), ('favorites', tr('已收藏'))):
            btn = button(title, lambda checked=False, k=key: self.set_kind(k), 'filterChip')
            btn.setCheckable(True)
            btn.setChecked(key == 'all')
            filters.addWidget(btn)
            self.filter_buttons[key] = btn
        filters.addStretch()
        self.sort_box = QComboBox()
        self.sort_box.setObjectName('gallerySort')
        self.sort_box.addItem(tr('时间：从新到旧'), 'newest')
        self.sort_box.addItem(tr('时间：从旧到新'), 'oldest')
        self.sort_box.addItem(tr('搜索：相关度优先'), 'relevance')
        self.sort_box.setItemData(2, tr('有搜索词时，标题命中和多次命中排在前面；没有搜索词时按时间从新到旧。'), Qt.ItemDataRole.ToolTipRole)
        self.sort_box.currentIndexChanged.connect(lambda: self.refresh())
        filters.addWidget(self.sort_box)
        tools.addLayout(filters)
        content.addWidget(toolbar)
        self.library_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.library_splitter.setChildrenCollapsible(False)
        self.library_splitter.setHandleWidth(14)
        stream = QWidget()
        stream.setMinimumWidth(285)
        flow = QVBoxLayout(stream)
        flow.setContentsMargins(0, 0, 0, 0)
        flow.setSpacing(12)
        flow_header = QHBoxLayout()
        self.heading = label(tr('全部内容'), 'galleryHeading')
        self.result_label = label('', 'muted')
        flow_header.addWidget(self.heading)
        flow_header.addWidget(self.result_label)
        flow_header.addStretch()
        self.detail_toggle = button(tr('详情'), self.toggle_detail, 'iconButton', 'tabs')
        flow_header.addWidget(self.detail_toggle)
        flow.addLayout(flow_header)
        self.gallery = GalleryView()
        self.gallery.setObjectName('screenshotGallery')
        self.card_delegate = CardDelegate(self)
        self.gallery.setItemDelegate(self.card_delegate)
        self.gallery.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.gallery.setToolTip(tr('按住 Ctrl 或 Shift 可多选；右键批量修改标签或恢复自动分类。'))
        self.gallery.currentItemChanged.connect(self.selection_changed)
        self.gallery.itemDoubleClicked.connect(self.preview)
        self.gallery.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.gallery.customContextMenuRequested.connect(self.gallery_menu)
        self.gallery.geometry_changed.connect(self.update_preview_image)
        flow.addWidget(self.gallery, 1)
        self.empty = label('', 'muted')
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        flow.addWidget(self.empty, 1)
        self.more = button(tr('加载更多内容'), self.load_more)
        flow.addWidget(self.more)
        self.retention_label = label('', 'retentionBanner')
        self.retention_label.setWordWrap(True)
        flow.addWidget(self.retention_label)
        self.library_splitter.addWidget(stream)
        self.detail_panel = QFrame()
        self.detail_panel.setObjectName('details')
        self.detail_panel.setMinimumWidth(280)
        self.detail_panel.setMaximumWidth(430)
        panel = QVBoxLayout(self.detail_panel)
        panel.setContentsMargins(16, 16, 16, 16)
        panel.setSpacing(12)
        detail_header = QHBoxLayout()
        detail_header.addWidget(label(tr('内容详情'), 'detailtitle'))
        detail_header.addStretch()
        self.previous_btn = button('', lambda: self.select_relative(-1), 'iconButton', 'previous')
        self.next_btn = button('', lambda: self.select_relative(1), 'iconButton', 'next')
        self.previous_btn.setToolTip(tr('上一条内容'))
        self.next_btn.setToolTip(tr('下一条内容'))
        detail_header.addWidget(self.previous_btn)
        detail_header.addWidget(self.next_btn)
        panel.addLayout(detail_header)
        self.detail_title = label(tr('还没有选择内容'), 'selectionTitle')
        self.detail_title.setWordWrap(True)
        self.detail_title.setMaximumHeight(46)
        self.detail_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        panel.addWidget(self.detail_title)
        self.preview_label = label(tr('选择一条内容\n查看原图或原文'), 'previewSurface')
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setFixedHeight(170)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        panel.addWidget(self.preview_label)
        self.pin_btn = button(tr('贴到屏幕'), self.pin_image, 'primary', 'pin')
        self.pin_btn.setObjectName('pinToScreen')
        self.pin_btn.setToolTip(tr('将原图置顶悬浮；拖动移动、滚轮缩放，可调透明度 · Ctrl+P'))
        panel.addWidget(self.pin_btn)
        image_tools = QHBoxLayout()
        image_tools.setSpacing(7)
        self.annotate_btn = button(tr('标注这张图'), self.annotate_image)
        self.annotate_btn.setObjectName('annotateSelectedImage')
        self.compress_btn = button(tr('压缩这张图'), self.compress_image)
        self.compress_btn.setObjectName('compressSelectedImage')
        image_tools.addWidget(self.annotate_btn)
        image_tools.addWidget(self.compress_btn)
        panel.addLayout(image_tools)
        self.detail_actions = QGridLayout()
        self.detail_actions.setSpacing(7)
        self._detail_layout_type = 'image'
        self.copy_btn = button(tr('复制原图'), self.copy_image, icon_name='image')
        self.star_btn = button(tr('收藏'), self.favorite, icon_name='star')
        self.open_btn = button(tr('放大'), self.preview, icon_name='expand')
        for column, btn in enumerate((self.copy_btn, self.star_btn, self.open_btn)):
            self.detail_actions.addWidget(btn, 0, column)
        self.export_btn = button(tr('导出'), self.export_image)
        self.retry_btn = button(tr('重新识别'), self.retry_ocr)
        self.delete_btn = button(tr('删除'), self.delete_image, 'danger')
        for column, btn in enumerate((self.export_btn, self.retry_btn, self.delete_btn)):
            self.detail_actions.addWidget(btn, 1, column)
        panel.addLayout(self.detail_actions)
        self.open_link_btn = button(tr('打开链接'), self.open_link, icon_name='link')
        self.open_link_btn.setObjectName('openContentLink')
        self.open_link_btn.hide()
        panel.addWidget(self.open_link_btn)
        self.similar_box = QFrame()
        self.similar_box.setObjectName('similarBox')
        similar_layout = QVBoxLayout(self.similar_box)
        similar_layout.setContentsMargins(0, 0, 0, 0)
        similar_layout.setSpacing(6)
        self.similar_label = label('', 'eyebrow')
        similar_layout.addWidget(self.similar_label)
        similar_actions = QHBoxLayout()
        similar_actions.setSpacing(6)
        self.similar_view_btn = button(tr('查看这一组'), self.show_similar_group)
        similar_actions.addWidget(self.similar_view_btn, 1)
        self.keep_one_btn = button(tr('只保留这张'), self.keep_only_active)
        self.keep_one_btn.setToolTip(tr('永久删除同组的其他截图；收藏的不删。'))
        similar_actions.addWidget(self.keep_one_btn, 1)
        similar_layout.addLayout(similar_actions)
        self.similar_box.hide()
        panel.addWidget(self.similar_box)
        self.codes_heading = label(tr('链接与二维码'), 'eyebrow')
        panel.addWidget(self.codes_heading)
        self.codes_list = QListWidget()
        self.codes_list.setObjectName('detectedLinks')
        self.codes_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.codes_list.setToolTip(tr('双击打开网页链接；其他内容双击复制。右键可选择复制或打开。'))
        self.codes_list.itemActivated.connect(self._activate_code)
        self.codes_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.codes_list.customContextMenuRequested.connect(self._codes_menu)
        panel.addWidget(self.codes_list)
        self.codes_heading.hide()
        self.codes_list.hide()
        topic_row = QHBoxLayout()
        topic_row.addWidget(label(tr('主题标签'), 'eyebrow'), 1)
        self.edit_tags_btn = button(tr('修改'), lambda: self.edit_tags([self.active_id] if self.active_id else []), 'copyTextAction')
        self.edit_tags_btn.setObjectName('editContentTags')
        topic_row.addWidget(self.edit_tags_btn)
        panel.addLayout(topic_row)
        self.detail_tags = label('', 'metadata')
        self.detail_tags.setWordWrap(True)
        self.detail_tags.setObjectName('contentTags')
        panel.addWidget(self.detail_tags)
        self.topic_status = label('', 'muted')
        self.topic_status.setWordWrap(True)
        panel.addWidget(self.topic_status)
        ocr_header = QHBoxLayout()
        self.content_heading = label(tr('识别文字 · 离线 OCR'), 'eyebrow')
        ocr_header.addWidget(self.content_heading)
        ocr_header.addStretch()
        self.copy_text_btn = button(tr('复制文字'), self.copy_text, 'copyTextAction')
        ocr_header.addWidget(self.copy_text_btn)
        panel.addLayout(ocr_header)
        self.ocr_text = QTextEdit()
        self.ocr_text.setReadOnly(True)
        self.ocr_text.setMinimumHeight(150)
        self.ocr_text.setPlaceholderText(tr('原文或本地图片识别结果会显示在这里。'))
        panel.addWidget(self.ocr_text, 1)
        self.ocr_status = label('', 'muted')
        self.ocr_status.setWordWrap(True)
        panel.addWidget(self.ocr_status)
        panel.addWidget(label(tr('记录信息'), 'eyebrow'))
        self.detail_meta = label('', 'metadata')
        self.detail_meta.setWordWrap(True)
        panel.addWidget(self.detail_meta)
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail_scroll.setMinimumWidth(280)
        self.detail_scroll.setMaximumWidth(430)
        self.detail_scroll.setWidget(self.detail_panel)
        self.library_splitter.addWidget(self.detail_scroll)
        self.library_splitter.setStretchFactor(0, 1)
        self.library_splitter.setStretchFactor(1, 0)
        self.library_splitter.setSizes([860, 318])
        self.library_splitter.splitterMoved.connect(lambda *_: self.update_preview_image())
        content.addWidget(self.library_splitter, 1)
        self.pages.addWidget(library)
        self.build_status_page()
        self.detail_buttons = [self.copy_btn, self.star_btn, self.open_btn, self.export_btn, self.copy_text_btn, self.retry_btn, self.delete_btn, self.pin_btn, self.annotate_btn, self.compress_btn]
        self.statusBar().showMessage(tr('已准备好 · 图片进入剪贴板后自动保存'))
        for control in self.centralWidget().findChildren(QWidget):
            control.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and watched is getattr(self, 'search', None):
            if event.key() == Qt.Key.Key_V and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                mime = QApplication.clipboard().mimeData()
                if mime is not None and mime.hasUrls() and any((url.isLocalFile() for url in mime.urls())):
                    self.paste_from_window()
                    return True
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            mime = event.mimeData()
            if mime.hasUrls() and any((url.isLocalFile() for url in mime.urls())):
                if event.type() == QEvent.Type.Drop:
                    self.dropEvent(event)
                else:
                    self.dragEnterEvent(event)
                return True
        return super().eventFilter(watched, event)

    def build_status_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(15)
        layout.addWidget(label(tr('托盘与运行状态'), 'title'))
        layout.addWidget(label(tr('关闭主窗口后，自动收集与标签增强继续独立运行。'), 'muted'))
        self.service_labels = {}
        for key, title, description, action, action_text in (('capture', tr('剪贴板自动收集'), tr('自动保存图片；文字与链接默认关闭，可从主窗口或设置中开启。'), self.toggle_pause, tr('暂停 / 恢复收集')), ('browser', tr('浏览器标签增强'), tr('沿用原版 AutoIt 引擎，支持滚轮切换和双击关闭当前标签。'), self.toggle_tabs, tr('启停标签增强')), ('ocr', tr('离线文字识别'), tr('离线小模型自动识别中英文；截图逐张排队，识别完成后释放模型。'), self.retry_all, tr('重试失败识别'))):
            card = QFrame()
            card.setObjectName('statusCard')
            box = QVBoxLayout(card)
            box.setContentsMargins(20, 18, 20, 18)
            row = QHBoxLayout()
            row.addWidget(label(title, 'detailtitle'))
            row.addStretch()
            state = label('', 'eyebrow')
            self.service_labels[key] = state
            row.addWidget(state)
            box.addLayout(row)
            desc = label(description, 'muted')
            desc.setWordWrap(True)
            box.addWidget(desc)
            actions = QHBoxLayout()
            actions.addStretch()
            actions.addWidget(button(action_text, action))
            box.addLayout(actions)
            layout.addWidget(card)
        card = QFrame()
        card.setObjectName('statusCard')
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 18, 20, 18)
        row = QHBoxLayout()
        row.addWidget(label(tr('本地主题分类'), 'detailtitle'))
        row.addStretch()
        self.topic_state = label('', 'eyebrow')
        row.addWidget(self.topic_state)
        box.addLayout(row)
        self.topic_summary = label('', 'muted')
        self.topic_summary.setWordWrap(True)
        box.addWidget(self.topic_summary)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button(tr('整理待整理'), lambda: self.open_review('unknown')))
        actions.addWidget(button(tr('逐条确认自动标签'), lambda: self.open_review('auto')))
        self.train_button = button(tr('训练个人模型'), self.train_personal_model)
        self.train_button.setToolTip(tr('用你的手动标签在本机微调分类头；训练后先报告效果，由你决定是否启用。'))
        actions.addWidget(self.train_button)
        self.personal_button = button(tr('停用个人模型'), self.toggle_personal_model)
        actions.addWidget(self.personal_button)
        box.addLayout(actions)
        layout.addWidget(card)
        info = label(tr('数据保存在本机明文文件中，不上传到服务器。\n鼠标标签操作由独立的原版引擎进程处理，自动收集可单独暂停。'), 'muted')
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addStretch()
        actions = QHBoxLayout()
        actions.addWidget(button(tr('打开数据目录'), self.open_data_folder, icon_name='folder'))
        actions.addWidget(button(tr('隐藏到托盘'), self.hide_to_tray))
        actions.addStretch()
        actions.addWidget(button(tr('退出 {APP_NAME}', APP_NAME=app_name()), self.quit_app, 'danger'))
        layout.addLayout(actions)
        self.pages.addWidget(page)

    def show_page(self, page):
        if self.page == 'preferences' and self.preferences_panel is not None:
            saved_theme = normalize_theme(self.vault.setting('theme', 'light'))
            if self.theme_id != saved_theme:
                self.set_theme(saved_theme)
        if page == 'preferences':
            from .preferences import PreferencesPanel
            if self.preferences_panel is not None:
                self.pages.removeWidget(self.preferences_panel)
                self.preferences_panel.deleteLater()
            self.preferences_panel = PreferencesPanel(self)
            self.preferences_panel.layout().setContentsMargins(14, 12, 14, 12)
            self.preferences_panel.saved.connect(lambda: self.show_page('library'))
            self.preferences_panel.cancelled.connect(lambda: self.show_page('library'))
            self.pages.addWidget(self.preferences_panel)
            self.pages.setCurrentWidget(self.preferences_panel)
        else:
            self.pages.setCurrentIndex(1 if page == 'status' else 0)
            if page == 'status':
                self.update_topic_summary()
        self.page = page
        for key, btn in self.page_buttons.items():
            btn.setChecked(key == page)

    def open_data_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.vault.root)))

    def focus_search(self):
        self.show_page('library')
        self.search.setFocus()

    def hide_to_tray(self):
        if self.tray:
            self.close()
        else:
            self.showMinimized()

    def toggle_detail(self):
        self.detail_scroll.setVisible(self.detail_scroll.isHidden())
        QTimer.singleShot(0, self.update_preview_image)

    def select_relative(self, direction):
        index = self.gallery.currentRow() + direction
        if 0 <= index < self.gallery.count():
            self.gallery.setCurrentRow(index)

    def update_preview_image(self):
        if not hasattr(self, 'preview_label'):
            return
        if not self.active_id:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(tr('选择一条内容\n查看原图或原文'))
            return
        row = self.vault.get(self.active_id)
        if row and row.get('item_type', 'image') != 'image':
            self.preview_label.setPixmap(QPixmap())
            kind = row.get('item_type')
            host = QUrl(row.get('content', '').strip()).host() if kind == 'link' else ''
            self.preview_label.setText((tr('链接\n') + host if kind == 'link' else tr('文字记录')) + tr('\n原文显示在下方'))
            return
        pix = self.card_delegate.cache.get(self.active_id)
        self.preview_label.setPixmap(pix.scaled(max(20, self.preview_label.width() - 18), self.preview_label.height() - 18, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def build_tray(self, enabled):
        self.tray = None
        if enabled and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.windowIcon(), self)
            menu = QMenu(self)
            menu.addAction(tr('打开 {APP_NAME}', APP_NAME=app_name()), self.show_window)
            self.tray_pause = menu.addAction(tr('暂停自动收集'), self.toggle_pause)
            self.tray_text = menu.addAction(tr('自动收集文字与链接'))
            self.tray_text.setCheckable(True)
            self.tray_text.setChecked(self.collect_text)
            self.tray_text.toggled.connect(self.set_text_capture_enabled)
            menu.addAction(tr('粘贴当前内容'), self.capture_current)
            self.tray_full = menu.addAction(tr('全屏截图'), lambda: QTimer.singleShot(300, self.take_fullscreen))
            self.tray_shot = menu.addAction(tr('区域截图'), lambda: QTimer.singleShot(300, self.start_screenshot))
            self.tray_repeat_shot = menu.addAction(tr('再次截取上次区域'), lambda: QTimer.singleShot(300, lambda: self.start_screenshot(repeat=True)))
            self.tray_repeat_shot.setEnabled(bool(self.last_screenshot_region))
            self.tray_quick = menu.addAction(tr('随手面板'), self.toggle_quick_paste)
            menu.addAction(tr('最近文件浮台'), self.show_recent_files)
            menu.addAction(tr('文件中转篮'), self.show_file_basket)
            menu.addAction(tr('截图标注'), lambda: QTimer.singleShot(300, lambda: self.start_screenshot(annotate=True)))
            menu.addAction(tr('图片压缩…'), self.compress_local_image)
            menu.addAction(tr('锁定最近文件'), self.lock_recent_files)
            menu.addAction(tr('显示悬浮球'), self.show_floating_ball)
            self.tray_pin_clipboard = menu.addAction(tr('把剪贴板图片贴到屏幕'), self.pin_clipboard)
            self.tray_close_pins = menu.addAction(tr('关闭全部贴图'), self.close_all_pins)
            self.tray_close_pins.setEnabled(False)
            self.tray_pin_input = menu.addAction(tr('恢复贴图鼠标操作'), self.restore_pin_input)
            self.tray_pin_input.setVisible(False)
            menu.addSeparator()
            self.tray_tabs = menu.addAction(tr('滚轮切换标签页'))
            self.tray_tabs.setCheckable(True)
            self.tray_tabs.toggled.connect(self.set_tabs_enabled)
            menu.addAction(tr('工具设置'), self.settings_dialog)
            menu.addSeparator()
            menu.addAction(tr('退出'), self.quit_app)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda reason: self.show_window() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
            self.tray.show()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @staticmethod
    def sequence():
        try:
            return ctypes.windll.user32.GetClipboardSequenceNumber()
        except (AttributeError, OSError):
            return 0

    def clipboard_changed(self):
        sequence = self.sequence()
        if self.own_copy or (sequence and sequence in (self.own_copy_sequence, self.last_sequence)):
            return
        self.clip_generation += 1
        self.last_sequence = sequence
        if self.listening and (not self.quitting) and (not self.own_copy):
            self.read_clipboard(self.clip_generation, sequence, 0)

    def read_clipboard(self, generation, sequence, attempt, manual=False):
        if self.quitting or generation != self.clip_generation or (not manual and (not self.listening)):
            return
        if sequence and sequence != self.sequence():
            return
        if not manual and sequence and (sequence == self.own_copy_sequence):
            return
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if should_exclude_clipboard(mime):
            if manual:
                self.statusBar().showMessage(tr('这项剪贴板内容带有不收集标记或来自文件复制，已跳过。'), 6000)
            return
        source = clipboard_source_app(foreground_fallback=not manual)
        if app_excluded(source, self.excluded_apps):
            if manual:
                self.statusBar().showMessage(tr('这项内容来自已排除的程序（{source}），已跳过。', source=source), 6000)
            return
        image = clipboard.image()
        if not image.isNull():
            if image.width() * image.height() > 80000000:
                self.report_error(tr('图片过大（超过 8000 万像素），未自动保存。'))
                return
            if generation != self.clip_generation or (sequence and sequence != self.sequence()):
                return
            self.capture.submit('image', image.copy())
        elif (manual or self.collect_text) and mime is not None and mime.hasText() and (not mime.hasImage()):
            text = mime.text()
            if generation != self.clip_generation or (sequence and sequence != self.sequence()):
                return
            if text.strip():
                reason = '' if manual or not self.skip_sensitive else sensitive_text_reason(text)
                if reason:
                    self.statusBar().showMessage(tr('内容疑似{reason}，已跳过自动收集；需要保存时可手动粘贴。', reason=reason), 8000)
                    return
                self.capture.submit('text', text)
            elif manual:
                self.statusBar().showMessage(tr('剪贴板中没有可保存的文字或图片。'), 5000)
        elif attempt < 4:
            QTimer.singleShot(60 * (attempt + 1), lambda: self.read_clipboard(generation, sequence, attempt + 1, manual))
        elif manual:
            self.statusBar().showMessage(tr('剪贴板中没有可保存的图片、文字或链接。'), 8000)

    def capture_current(self):
        self.clip_generation += 1
        self.read_clipboard(self.clip_generation, self.sequence(), 0, True)

    def toggle_pause(self):
        self.listening = not self.listening
        self.vault.set_setting('paused', not self.listening)
        self.clip_generation += 1
        self.last_sequence = self.sequence()
        self.update_capture_state()
        self.capture_state_changed.emit(self.listening)

    def update_capture_state(self):
        text = tr('暂停自动收集') if self.listening else tr('恢复自动收集')
        self.pause_button.setText(tr('自动收集中') if self.listening else tr('收集已暂停'))
        self.pause_button.setIcon(glyph('dot', theme()['success'] if self.listening else theme()['warning'], 12))
        self.pause_button.setProperty('paused', not self.listening)
        self.pause_button.style().unpolish(self.pause_button)
        self.pause_button.style().polish(self.pause_button)
        self.pause_banner.setVisible(not self.listening)
        self.service_labels['capture'].setText(tr('正在监听') if self.listening else tr('已暂停'))
        self.text_capture_button.setText(tr('文字与链接 ') + (tr('开') if self.collect_text else tr('关')))
        self.text_capture_button.setIcon(glyph('text', theme()['accent'] if self.collect_text else theme()['muted'], 14))
        if self.tray:
            self.tray_pause.setText(text)
            self.tray_text.blockSignals(True)
            self.tray_text.setChecked(self.collect_text)
            self.tray_text.blockSignals(False)
        self.statusBar().showMessage(tr('正在自动收集图片') + (tr('、文字和链接') if self.collect_text else tr(' · 文字与链接收集已关闭')) if self.listening else tr('自动收集已暂停 · 可手动粘贴内容'))

    def set_text_capture_enabled(self, enabled, persist=True):
        self.collect_text = bool(enabled)
        if persist:
            self.vault.set_setting('collect_text', self.collect_text)
        self.clip_generation += 1
        self.last_sequence = self.sequence()
        self.update_capture_state()
        self.text_capture_state_changed.emit(self.collect_text)

    def on_tab_error(self, message):
        self.statusBar().showMessage(tr('浏览器工具：') + tr(message), 10000)
        self.update_tabs_state()

    def apply_tabs_settings(self):
        if self.tab_scroller:
            self.tab_scroller.configure(enabled=self.tabs_enabled, reverse=self.tabs_reverse, double_click=self.tabs_double_click, autofocus=self.tabs_autofocus, focus_mode=self.tabs_focus_mode, focus_delay=self.tabs_focus_delay)
        self.update_tabs_state()

    def update_tabs_state(self):
        if not self.browser_tools:
            state = tr('未启动')
        elif not self.tabs_enabled:
            state = tr('已暂停')
        elif getattr(self.tab_scroller, 'starting', False):
            state = tr('启动中')
        elif self.tab_scroller and self.tab_scroller.active:
            state = tr('已开启')
        else:
            state = tr('不可用')
        self.tabs_button.setText(tr('标签增强 ') + state)
        self.tabs_button.setIcon(glyph('tabs', theme()['muted'], 14))
        self.tabs_button.setEnabled(self.browser_tools)
        self.service_labels['browser'].setText(state)
        if self.tray:
            self.tray_tabs.blockSignals(True)
            self.tray_tabs.setChecked(self.tabs_enabled and bool(self.tab_scroller and self.tab_scroller.active))
            self.tray_tabs.blockSignals(False)
            self.tray_tabs.setEnabled(self.browser_tools)
            self.tray.setToolTip(tr('{APP_NAME} · 自动收集{value_1} · 文字/链接{value_2} · 标签滚轮{state}', APP_NAME=app_name(), value_1=tr('开启') if self.listening else tr('暂停'), value_2=tr('开启') if self.collect_text else tr('关闭'), state=state))

    def toggle_tabs(self):
        self.set_tabs_enabled(not self.tabs_enabled)

    def set_tabs_enabled(self, enabled):
        self.tabs_enabled = bool(enabled)
        self.vault.set_setting('tabs_enabled', self.tabs_enabled)
        self.apply_tabs_settings()
        self.tabs_state_changed.emit(self.tabs_enabled)

    def set_scope(self, scope):
        self.scope = scope
        self.show_page('library')
        self.rows_limit = 120
        for key, btn in self.nav_buttons.items():
            btn.setChecked(key == scope)
        self.refresh()

    def set_topic(self, topic):
        self.topic = topic if topic in TOPIC_FILTER_LABELS else 'all'
        for key, btn in self.topic_buttons.items():
            btn.setChecked(key == self.topic)
        self.rows_limit = 120
        self.show_page('library')
        self.refresh()

    def set_category(self, category):
        """Keep internal callers from old releases on their preserved manual labels."""
        self.set_topic(category if category in ('all', 'tech') else 'legacy_' + category)

    def set_item_type(self, item_type):
        self.item_type = item_type if item_type in TYPE_LABELS else 'all'
        self.rows_limit = 120
        self.type_filter.blockSignals(True)
        self.type_filter.setCurrentIndex(max(0, self.type_filter.findData(self.item_type)))
        self.type_filter.blockSignals(False)
        self.refresh()

    def selected_keys(self):
        return [item.data(Qt.ItemDataRole.UserRole)['id'] for item in self.gallery.selectedItems()]

    def edit_tags(self, keys=None):
        keys = list(dict.fromkeys(keys if keys is not None else self.selected_keys()))
        rows = [row for key in keys if (row := self.vault.get(key)) is not None]
        if not rows:
            return
        initial = rows[0].get('tags', [])
        if any((row.get('tags', []) != initial for row in rows[1:])):
            initial = []
        dialog = TopicDialog(initial, len(rows), self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.apply_tags([row['id'] for row in rows], dialog.tags())
        finally:
            dialog.deleteLater()

    def apply_tags(self, keys, tags):
        self.vault.set_tags_many(keys, tags)
        self.refresh()
        self.statusBar().showMessage(tr('已修改 {value_0} 条内容的标签 · 手动标签会保留', value_0=len(keys)), 6000)

    def restore_auto_classification(self, keys=None):
        keys = list(dict.fromkeys(keys if keys is not None else self.selected_keys()))
        if not keys:
            return
        self.vault.reset_tags_to_automatic(keys)
        self.classif_requested.emit(keys)
        self.refresh()
        self.statusBar().showMessage(tr('已将 {value_0} 条内容加入本地分类队列', value_0=len(keys)), 6000)

    def load_more(self):
        self.rows_limit += 120
        self.refresh()

    def set_kind(self, kind):
        self.kind = kind
        self.rows_limit = 120
        for key, btn in self.filter_buttons.items():
            btn.setChecked(key == kind)
        self.refresh()

    def refresh(self):
        if self.quitting:
            return
        self.update_similar()
        if self.scope == 'similar':
            rows = self.vault.rows_by_ids([key for group in self.similar_groups for key in group])
            self.more.setVisible(False)
        else:
            rows = self.vault.query(self.search.text(), self.scope, self.rows_limit + 1, kind=self.kind, sort=self.sort_box.currentData(), topic=self.topic, item_type=self.item_type)
            self.more.setVisible(len(rows) > self.rows_limit)
            rows = rows[:self.rows_limit]
        shown = [self.gallery.item(index).data(Qt.ItemDataRole.UserRole)['id'] for index in range(self.gallery.count())]
        if shown == [row['id'] for row in rows]:
            for index, row in enumerate(rows):
                item = self.gallery.item(index)
                if item.data(Qt.ItemDataRole.UserRole) != row:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                    item.setToolTip(row['title'])
        else:
            self._rebuild_gallery(rows)
        self.update_detail()
        scope_title = {'all': tr('全部内容'), 'today': tr('今天'), 'yesterday': tr('昨天'), 'week': tr('最近 7 天'), 'favorites': tr('永久收藏'), 'similar': tr('相似截图')}[self.scope]
        if self.scope == 'similar':
            self.heading.setText(scope_title)
            self.result_label.setText(tr('{value_0} 组 · {value_1} 张 · 按组排列，不受其他筛选影响', value_0=len(self.similar_groups), value_1=len(rows)))
        else:
            self.heading.setText(' · '.join([scope_title] + ([tr(TOPIC_FILTER_LABELS[self.topic])] if self.topic != 'all' else []) + ([tr(TYPE_LABELS[self.item_type])] if self.item_type != 'all' else [])))
            self.result_label.setText(tr('已显示 {value_0} 条', value_0=len(rows)) + (tr(' · 搜索结果') if self.search.text() else ''))
        self.empty.setVisible(not rows)
        self.gallery.setVisible(bool(rows))
        if self.scope == 'similar':
            self.empty.setText(tr('没有发现相似截图\n\n内容几乎相同的截图会自动归成一组，方便挑一张留下。\n更早的截图会在后台补算后参与比较。'))
        elif self.search.text() or self.kind != 'all' or self.topic != 'all' or (self.item_type != 'all'):
            self.empty.setText(tr('未找到匹配内容\n\n试试其他关键词，或清除类型与主题筛选。\n图片文字需要等待本地 OCR 完成后才能搜索。'))
        elif self.scope == 'favorites':
            self.empty.setText(tr('收藏重要内容\n\n选中记录后点击「收藏」，就会永久保留。'))
        else:
            self.empty.setText(tr('还没有保存的内容\n\n截图进入剪贴板后自动保存。\n文字与链接自动收集默认关闭，可在上方开启。\n\n粘贴图片或文字可存入内容库；导入图片请点上方按钮。\n拖入或粘贴文件会直接放入中转篮。'))
        stats = self.vault.stats()
        size = f"{stats['size'] / 1024 / 1024:.1f} MB"
        self.storage_size.setText(size)
        self.side_stats.setText(tr('本地已存 {value_0:,} 条记录', value_0=stats['total']))
        counts = self.vault.topic_counts()
        for key, btn in self.topic_buttons.items():
            update_navigation_count(btn, self.topic_counts[key], counts.get(key, 0))
            btn.setAccessibleName(tr('{value_0}，{value_1:,} 条', value_0=tr(TOPIC_FILTER_LABELS[key]), value_1=counts.get(key, 0)))
            if key in LEGACY_TOPIC_LABELS:
                btn.setVisible(bool(counts.get(key, 0)) or self.topic == key)
        for key, btn in self.nav_buttons.items():
            count = len(self.similar_sizes) if key == 'similar' else stats['total' if key == 'all' else key]
            update_navigation_count(btn, self.nav_counts[key], count)
            btn.setAccessibleName(tr('{value_0}，{count:,} 条', value_0=self.nav_titles[key], count=count))
        for key, title in (('all', tr('全部')), ('text', tr('含文字')), ('pending', tr('识别中')), ('error', tr('识别失败')), ('favorites', tr('已收藏'))):
            self.filter_buttons[key].setText(f"{title}  {stats['total' if key == 'all' else key]:,}")
        days = self.retention_days
        self.retention_label.setText(tr('未收藏内容保留 {days} 天，到期自动清理 · 收藏后永久保留', days=days) if days else tr('自动清理已关闭 · 所有内容持续保留'))
        self.service_labels['ocr'].setText(tr('识别中 {value_0} · 失败 {value_1}', value_0=stats['pending'], value_1=stats['error']))
        if self.tray:
            tabs = tr('开启') if self.tab_scroller and self.tab_scroller.active else tr('暂停')
            self.tray.setToolTip(tr('{APP_NAME} · 自动收集{value_1} · 文字/链接{value_2}\n今天 {value_3} 条 · 最近 7 天 {value_4} 条 · {size}\n标签滚轮{tabs}', APP_NAME=app_name(), value_1=tr('开启') if self.listening else tr('暂停'), value_2=tr('开启') if self.collect_text else tr('关闭'), value_3=stats['today'], value_4=stats['week'], size=size, tabs=tabs))

    def _rebuild_gallery(self, rows):
        selected, scroll = (self.active_id, self.gallery.verticalScrollBar().value())
        selected_keys = set(self.selected_keys())
        self.gallery.blockSignals(True)
        self.gallery.clear()
        selected_item = None
        for row in rows:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setSizeHint(self.gallery.card_size)
            item.setToolTip(row['title'])
            self.gallery.addItem(item)
            if row['id'] in selected_keys:
                item.setSelected(True)
            if row['id'] == selected:
                selected_item = item
        if selected_item is None and self.gallery.count():
            selected_item = self.gallery.item(0)
        if selected_item:
            self.gallery.setCurrentItem(selected_item, QItemSelectionModel.SelectionFlag.NoUpdate)
            if not self.gallery.selectedItems():
                selected_item.setSelected(True)
        self.gallery.blockSignals(False)
        self.gallery.verticalScrollBar().setValue(scroll)
        self.active_id = selected_item.data(Qt.ItemDataRole.UserRole)['id'] if selected_item else None

    def selection_changed(self, item, previous=None):
        self.active_id = item.data(Qt.ItemDataRole.UserRole)['id'] if item else None
        self.update_detail()

    def update_detail(self):
        row = self.vault.get(self.active_id) if self.active_id else None
        is_image = bool(row and row.get('item_type', 'image') == 'image')
        self.preview_label.setVisible(is_image or row is None)
        for control in (self.pin_btn, self.copy_btn, self.open_btn, self.retry_btn, self.annotate_btn, self.compress_btn):
            control.setVisible(is_image)
        layout_type = 'image' if is_image else 'text'
        if self._detail_layout_type != layout_type:
            actions = (self.copy_btn, self.star_btn, self.open_btn, self.export_btn, self.retry_btn, self.delete_btn)
            for control in actions:
                self.detail_actions.removeWidget(control)
            visible_actions = actions if is_image else (self.star_btn, self.export_btn, self.delete_btn)
            for position, control in enumerate(visible_actions):
                self.detail_actions.addWidget(control, position // 3, position % 3)
            self._detail_layout_type = layout_type
        self.copy_text_btn.setText(tr('复制文字') if is_image else tr('复制原文'))
        self.copy_text_btn.setProperty('primaryAction', bool(row and (not is_image)))
        self.copy_text_btn.style().unpolish(self.copy_text_btn)
        self.copy_text_btn.style().polish(self.copy_text_btn)
        for btn in self.detail_buttons:
            btn.setEnabled(bool(row))
        self.previous_btn.setEnabled(self.gallery.currentRow() > 0)
        self.next_btn.setEnabled(0 <= self.gallery.currentRow() < self.gallery.count() - 1)
        self.update_preview_image()
        self.edit_tags_btn.setEnabled(bool(row))
        tags = row.get('tags', []) if row else []
        self.detail_tags.setText(' · '.join((tr(ALL_TOPIC_LABELS.get(key, key)) for key in tags)) if tags else tr('待整理') if row else '')
        status = row.get('classification_status', 'pending') if row else ''
        if row and row.get('tags_manual'):
            self.topic_status.setText(tr('手动标签 · 自动分类不会覆盖'))
        elif status == 'pending':
            self.topic_status.setText(tr('等待图片文字识别后分类') if is_image and row.get('ocr_status') == 'pending' else tr('正在等待本地分类…'))
        elif status == 'error':
            self.topic_status.setText(tr('分类暂未完成，可右键恢复自动分类重试'))
        elif row:
            self.topic_status.setText(tr('本地自动分类') if tags else tr('暂未找到合适主题，可手动添加标签'))
        else:
            self.topic_status.clear()
        self.topic_status.setToolTip(row.get('classification_error', '') if row else '')
        self.open_link_btn.setVisible(bool(row and row.get('item_type') == 'link'))
        self._update_codes(row)
        group = self.similar_members.get(row['id']) if row else None
        self.similar_box.setVisible(bool(group))
        if group:
            self.similar_label.setText(tr('相似截图 · 同组 {value_0} 张', value_0=len(group)))
            self.similar_view_btn.setVisible(self.scope != 'similar')
        if not row:
            self.detail_title.setText(tr('还没有选择内容'))
            self.detail_meta.setText(tr('点击左侧卡片查看记录'))
            self.ocr_text.clear()
            self.ocr_status.clear()
            return
        item_type = row.get('item_type', 'image')
        is_image = item_type == 'image'
        for btn in (self.copy_btn, self.open_btn, self.pin_btn, self.retry_btn, self.annotate_btn, self.compress_btn):
            btn.setEnabled(is_image)
        self.detail_title.setText(QUrl(row.get('content', '')).host() if item_type == 'link' else row['title'])
        self.detail_title.setToolTip(row.get('content', '') if item_type == 'link' else row['title'])
        stamp = datetime.fromisoformat(row['captured_at']).astimezone()
        days = self.retention_days
        expiry = tr('永久收藏') if row['favorite'] else tr('{value_0:%m/%d %H:%M} 到期', value_0=stamp + timedelta(days=days)) if days else tr('永久保留')
        if is_image:
            self.detail_meta.setText(tr('图像尺寸    {value_0} × {value_1} px\n保存时间    {stamp:%Y/%m/%d %H:%M:%S}\n文件大小    {value_3:.1f} KB · PNG 原图\n保留策略    {expiry}', value_0=row['width'], value_1=row['height'], stamp=stamp, value_3=row['size'] / 1024, expiry=expiry))
        else:
            self.detail_meta.setText(tr('内容类型    {value_0}\n保存时间    {stamp:%Y/%m/%d %H:%M:%S}\n内容大小    {value_2:,} 字符 · {value_3:.1f} KB\n保留策略    {expiry}', value_0=tr(TYPE_LABELS.get(item_type, '文字')), stamp=stamp, value_2=len(row.get('content', '')), value_3=row['size'] / 1024, expiry=expiry))
        self.star_btn.setText(tr('已收藏') if row['favorite'] else tr('收藏'))
        self.star_btn.setIcon(glyph('star', theme()['favorite'] if row['favorite'] else theme()['muted']))
        text = row.get('ocr_text', '') if is_image else row.get('content', '')
        self.content_heading.setText(tr('识别文字 · 离线 OCR') if is_image else tr('链接原文') if item_type == 'link' else tr('原始文字'))
        self.ocr_text.setStyleSheet("font-family: Consolas, 'Microsoft YaHei UI';" if not is_image and is_code_content(text) else '')
        if self.ocr_text.toPlainText() != text:
            self.ocr_text.setPlainText(text)
        self.highlight_search()
        self.copy_text_btn.setEnabled(bool(text))
        if not is_image:
            self.ocr_status.setText(tr('原文已保存在本机 · 无需 OCR'))
            return
        if row['ocr_status'] == 'error':
            prefix = tr('重新识别失败，已保留原有文字：') if row['ocr_text'] else tr('识别失败：')
            self.ocr_status.setText(prefix + row['ocr_error'][:240])
        else:
            self.ocr_status.setText({'pending': tr('正在等待本地识别队列…'), 'done': tr('本地识别完成') if row['ocr_text'] else tr('识别完成，未发现可读文字')}[row['ocr_status']])

    def _update_codes(self, row):
        entries = []
        if row and row.get('item_type') != 'link':
            is_image = row.get('item_type', 'image') == 'image'
            codes = self.vault.image_codes(row['id']) if is_image else []
            text = row.get('ocr_text', '') if is_image else row.get('content', '')
            entries = [(tr('二维码'), code) for code in codes]
            for url in extract_urls(text):
                if url not in codes:
                    entries.append((tr('链接'), url))
        key = (row['id'] if row else None, tuple(entries))
        if key == self._codes_key:
            return
        self._codes_key = key
        self.codes_list.clear()
        for kind, value in entries:
            item = QListWidgetItem(f'{kind} · {value}')
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setToolTip(value)
            self.codes_list.addItem(item)
        if entries:
            row_height = self.codes_list.sizeHintForRow(0)
            self.codes_list.setFixedHeight(min(len(entries), 4) * row_height + 10)
        self.codes_heading.setVisible(bool(entries))
        self.codes_list.setVisible(bool(entries))

    def _activate_code(self, item):
        value = item.data(Qt.ItemDataRole.UserRole)
        if openable_url(value):
            if not QDesktopServices.openUrl(QUrl(value.strip())):
                self.report_error(tr('无法打开系统浏览器。'))
        else:
            self.copy_plain_text(value)
            self.statusBar().showMessage(tr('已复制二维码内容'), 4000)

    def _codes_menu(self, position):
        item = self.codes_list.itemAt(position)
        if item is None:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction(tr('复制'), lambda: (self.copy_plain_text(value), self.statusBar().showMessage(tr('已复制'), 3000)))
        if openable_url(value):
            menu.addAction(tr('打开链接'), lambda: QDesktopServices.openUrl(QUrl(value.strip())))
        try:
            menu.exec(self.codes_list.viewport().mapToGlobal(position))
        finally:
            menu.deleteLater()

    def update_similar(self, force=False):
        """Recompute similar groups when the library changed, at most every few seconds."""
        signature = self.vault.similarity_signature()
        stale = signature != self._similar_signature
        throttled = time.monotonic() - self._similar_time < 5 and self.scope != 'similar'
        if force or (stale and (not throttled)) or self._similar_signature is None:
            self.similar_groups = self.vault.similar_groups()
            self.similar_sizes = {key: len(group) for group in self.similar_groups for key in group}
            self.similar_members = {key: group for group in self.similar_groups for key in group}
            self._similar_signature, self._similar_time = (signature, time.monotonic())
        elif stale:
            self.refresh_timer.start(5000)

    def show_similar_group(self):
        self.set_scope('similar')

    def keep_only_active(self):
        group = self.similar_members.get(self.active_id) or []
        others = self.vault.rows_by_ids([key for key in group if key != self.active_id])
        removable = [row for row in others if not row['favorite']]
        kept = len(others) - len(removable)
        if not removable:
            self.statusBar().showMessage(tr('同组其他截图都已收藏，没有需要删除的。'), 6000)
            return
        message = tr('将永久删除同组其余 {value_0} 张截图', value_0=len(removable)) + (tr('，已收藏的 {kept} 张会保留', kept=kept) if kept else '') + tr('。此操作不可撤销。')
        if QMessageBox.question(self, tr('只保留这张'), message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            for row in removable:
                self.vault.delete(row['id'])
        except OSError as exc:
            self.report_error(tr('删除失败：{exc}', exc=exc))
        self.update_similar(force=True)
        self.refresh()
        self.statusBar().showMessage(tr('已删除 {value_0} 张相似截图', value_0=len(removable)), 6000)

    def rescan_old_images(self):
        """Re-recognize older screenshots to add text positions and QR codes."""
        keys = self.vault.images_needing_rescan()
        for key in keys:
            self.ocr.enqueue(key)
        if keys:
            self.statusBar().showMessage(tr('已将 {value_0} 张截图加入识别队列；原有文字和标签会保留。', value_0=len(keys)), 8000)
        return len(keys)

    def highlight_search(self):
        selections = []
        document = self.ocr_text.document()
        for term in self.search.text().split():
            cursor = document.find(term, 0)
            while not cursor.isNull() and len(selections) < 500:
                selection = QTextEdit.ExtraSelection()
                selection.cursor = cursor
                selection.format.setBackground(QColor(theme()['highlight']))
                selection.format.setForeground(QColor(theme()['highlight_text']))
                selections.append(selection)
                cursor = document.find(term, cursor.position())
        self.ocr_text.setExtraSelections(selections)

    def on_saved(self, key, created):
        if self.quick_panel is not None:
            self.quick_panel.request_refresh()
        row = self.vault.get(key)
        image = row and row.get('item_type', 'image') == 'image'
        if created and image:
            self.ocr.enqueue(key)
        if created and (not image) and self.classifier:
            self.classifier.enqueue(key)
        message = (tr('已保存图片 · 已加入本地识别队列') if image else tr('已保存文字或链接 · 已加入本地分类队列')) if created else tr('这项内容已在库中，已跳过重复保存')
        self.statusBar().showMessage(message, 6000)
        self.refresh_timer.start()

    def on_ocr(self, key):
        for dialog in list(self.screenshot_text_windows):
            if dialog.item_id == key:
                dialog.update_result(self.vault.get(key))
        if self.quick_panel is not None:
            self.quick_panel.request_refresh()
        if self.classifier and (not self.quitting):
            self.classifier.enqueue(key)
        self.refresh_timer.start()

    def on_classified(self, key):
        if not self.quitting:
            self.refresh_timer.start()

    def reclassify_all(self):
        """Snapshot the database before explicitly re-queuing automatic tags."""
        import sqlite3
        import uuid
        directory = self.vault.root / 'backups'
        try:
            directory.mkdir(exist_ok=True)
            target = directory / ('before-classification-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6] + '.sqlite3')
            with self.vault.connect() as source:
                with sqlite3.connect(target) as backup:
                    source.backup(backup)
            count = self.vault.request_reclassification()
            self.classif_requested.emit(None)
            self.statusBar().showMessage(tr('已备份分类记录，{count} 条内容等待本地分类；手动标签已保留。', count=count), 10000)
            self.refresh()
        except Exception:
            self.report_error(tr('暂时无法备份或重新分类，已有内容和手动标签仍保留。'))

    def open_review(self, scope='unknown'):
        from .review import ReviewDialog
        dialog = ReviewDialog(self, scope)
        dialog.exec()
        self.update_topic_summary()

    def update_topic_summary(self):
        if not hasattr(self, 'topic_summary'):
            return
        stats = self.vault.classification_agreement()
        model = self.vault.personal_model()
        if stats['reviewed']:
            percent = round(100 * stats['agree'] / stats['reviewed'])
            agreement = tr('已核对 {value_0} 条自动结果，其中 {value_1} 条与你的判断一致（{percent}%）；你补上 {value_3} 个标签、去掉 {value_4} 个。', value_0=stats['reviewed'], value_1=stats['agree'], percent=percent, value_3=stats['added'], value_4=stats['removed'])
        else:
            agreement = tr('还没有核对过自动标签。整理待整理或逐条确认后，这里会显示自动分类与你判断的一致率。')
        lines = [agreement, tr('可用于训练的手动标签 {value_0} 条（至少 {MIN_TRAIN_EXAMPLES} 条才能训练）。', value_0=stats['trainable'], MIN_TRAIN_EXAMPLES=MIN_TRAIN_EXAMPLES)]
        report = model.get('report') or {}
        if model['enabled'] and report:
            lines.append(tr('个人模型已启用：交叉验证中原模型一致 {value_0}/{value_1} 条，个人模型一致 {value_2}/{value_3} 条。', value_0=report.get('cv_base_agree', 0), value_1=report.get('examples', 0), value_2=report.get('cv_personal_agree', 0), value_3=report.get('examples', 0)))
        elif model['available']:
            lines.append(tr('个人模型已停用，自动分类使用随程序提供的模型。'))
        self.topic_summary.setText('\n'.join(lines))
        self.topic_state.setText(tr('个人模型') if model['enabled'] else tr('内置模型'))
        training = bool(self.trainer and self.trainer.isRunning())
        self.train_button.setEnabled(stats['trainable'] >= MIN_TRAIN_EXAMPLES and (not training) and bool(self.classifier))
        self.train_button.setText(tr('正在训练…') if training else tr('训练个人模型'))
        self.personal_button.setVisible(model['available'])
        self.personal_button.setText(tr('停用个人模型') if model['enabled'] else tr('启用个人模型'))

    def train_personal_model(self):
        if self.trainer and self.trainer.isRunning():
            return
        if self.vault.classification_agreement()['trainable'] < MIN_TRAIN_EXAMPLES:
            QMessageBox.information(self, tr('手动标签还不够'), tr('至少需要 {MIN_TRAIN_EXAMPLES} 条带主题的手动标签才能训练。', MIN_TRAIN_EXAMPLES=MIN_TRAIN_EXAMPLES))
            return
        self.trainer = HeadTrainer(self.vault)
        self.trainer.trained.connect(self.on_trained)
        self.trainer.failed.connect(lambda message: (self.report_error(message), self.update_topic_summary()))
        self.trainer.finished.connect(self.update_topic_summary)
        self.trainer.start()
        self.statusBar().showMessage(tr('正在本机训练个人模型，完成后会先报告效果…'), 10000)
        self.update_topic_summary()

    def on_trained(self, report):
        if self.quitting:
            self.vault.discard_personal_candidate()
            return
        total = report.get('examples', 0)
        base, personal = (report.get('cv_base_agree', 0), report.get('cv_personal_agree', 0))
        better = personal > base
        message = tr('交叉验证（每条手动标签都由没见过它的模型来预测）：\n原模型与你一致 {base}/{total} 条\n个人模型与你一致 {personal}/{value_3} 条\n\n启用后，约 {value_4}/{value_5} 条自动标签会改变；手动标签不受影响。', base=base, total=total, personal=personal, value_3=total, value_4=report.get('anchors_changed', 0), value_5=report.get('anchors', 0))
        if not better:
            message += tr('\n\n个人模型没有比原模型更准，建议暂不启用，继续积累手动标签后再试。')
        message += tr('\n\n现在启用并重新整理自动标签吗？会先备份分类记录。')
        default = QMessageBox.StandardButton.Yes if better else QMessageBox.StandardButton.No
        answer = QMessageBox.question(self, tr('个人模型训练完成'), message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, default)
        if answer == QMessageBox.StandardButton.Yes:
            self.vault.activate_personal_head(report)
            self.reclassify_all()
        else:
            self.vault.discard_personal_candidate()
        self.update_topic_summary()

    def toggle_personal_model(self):
        enabled = not self.vault.personal_model()['enabled']
        verb = tr('启用') if enabled else tr('停用')
        if QMessageBox.question(self, tr('{verb}个人模型', verb=verb), tr('{verb}后会重新整理全部自动标签（先备份分类记录），手动标签不受影响。继续吗？', verb=verb), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.vault.set_personal_model_enabled(enabled)
            self.reclassify_all()
        self.update_topic_summary()

    def on_availability(self, langs, error):
        self.ocr_languages, self.ocr_error = (langs, error)
        if not langs:
            self.statusBar().showMessage(tr('图片保存可用；离线文字识别暂不可用，可在设置中重试失败项目。'))

    def on_cleaned(self, count):
        if count:
            self.statusBar().showMessage(tr('已清理 {count} 条到期的未收藏内容', count=count), 8000)
            self.refresh_timer.start()

    def report_error(self, message):
        self.statusBar().showMessage(message)
        if self.tray:
            self.tray.showMessage(app_name(), message, QSystemTrayIcon.MessageIcon.Warning, 8000)

    def import_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr('导入截图'), '', tr('图片 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)'))
        for path in paths:
            self.capture.submit('file', path)

    def dragEnterEvent(self, event):
        if event.possibleActions() & Qt.DropAction.CopyAction and event.mimeData().hasUrls() and any((url.isLocalFile() for url in event.mimeData().urls())):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.possibleActions() & Qt.DropAction.CopyAction:
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths:
            event.ignore()
            return
        self.show_file_basket()
        result = self.quick_panel.add_basket_paths(paths)
        if result is None:
            event.ignore()
        else:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    def copy_image(self):
        row = self.vault.get(self.active_id)
        if row and row.get('item_type', 'image') == 'image':
            image = QImage(str(self.vault.images / row['filename']))
            if image.isNull():
                return self.report_error(tr('原图不存在或无法读取'))
            self._copy_owned(lambda: QApplication.clipboard().setImage(image))
            self.statusBar().showMessage(tr('已复制原图，可以直接粘贴到微信'), 5000)

    def copy_text(self):
        row = self.vault.get(self.active_id) if self.active_id else None
        if row:
            text = row.get('ocr_text', '') if row.get('item_type', 'image') == 'image' else row.get('content', '')
            self._copy_owned(lambda: QApplication.clipboard().setText(text))
            self.statusBar().showMessage(tr('已复制全文'), 4000)

    def _copy_owned(self, setter):
        self.clip_generation += 1
        self.own_copy = True
        try:
            setter()
        finally:
            self.own_copy_sequence = self.sequence()
            self.last_sequence = self.own_copy_sequence
            self.own_copy = False

    def open_link(self):
        row = self.vault.get(self.active_id) if self.active_id else None
        if not row or row.get('item_type') != 'link':
            return
        value = row.get('content', '').strip()
        url = QUrl(value)
        if url.scheme().lower() not in ('http', 'https') or not url.isValid() or (not url.host()) or any((char in value for char in '\r\n\t')):
            return self.report_error(tr('仅支持主动打开有效的 HTTP 或 HTTPS 链接。'))
        if not QDesktopServices.openUrl(url):
            self.report_error(tr('无法打开系统浏览器。'))

    def favorite(self):
        if self.active_id:
            self.vault.toggle_favorite(self.active_id)
            self.refresh()

    def retry_ocr(self):
        row = self.vault.get(self.active_id) if self.active_id else None
        if row and row.get('item_type', 'image') == 'image':
            self.ocr.enqueue(self.active_id)
            self.ocr_status.setText(tr('已加入本地识别队列…'))

    def preview(self, *args):
        row = self.vault.get(self.active_id) if self.active_id else None
        if row and row.get('item_type', 'image') != 'image':
            self.detail_scroll.show()
            self.ocr_text.setFocus()
        elif row:
            from .inspector import ImageInspector
            dialog = self.inspector_window
            if dialog is None:
                dialog = ImageInspector(self)
                dialog.setWindowModality(Qt.WindowModality.NonModal)
                self.inspector_window = dialog
                dialog.finished.connect(lambda _result, current=dialog: self._release_inspector(current))
                dialog.destroyed.connect(lambda _object=None, current=dialog: self._release_inspector(current))
            else:
                dialog.refresh()
            if dialog.isMinimized():
                dialog.showNormal()
            else:
                dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return dialog

    def _release_inspector(self, dialog):
        if self.inspector_window is dialog:
            self.inspector_window = None

    def _selected_tool_image(self):
        row = self.vault.get(self.active_id) if self.active_id else None
        if not row or row.get('item_type', 'image') != 'image':
            return (None, None)
        path = self.vault.images / row['filename']
        image = QImage(str(path))
        if image.isNull():
            self.report_error(tr('原图不存在或无法读取'))
            return (None, None)
        return (image, path)

    def _show_image_tool(self, dialog):
        self.image_tool_dialogs.append(dialog)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.finished.connect(lambda _result, current=dialog: self.image_tool_dialogs.remove(current) if current in self.image_tool_dialogs else None)
        dialog.copyRequested.connect(self._copy_tool_image)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _copy_tool_image(self, image):
        if not self.quitting and (not image.isNull()):
            self._copy_owned(lambda: QApplication.clipboard().setImage(image))
            self.statusBar().showMessage(tr('已复制处理后的图片，原图保持原样。'), 5000)

    def _save_tool_image(self, image):
        if not self.quitting and (not image.isNull()):
            self.capture.submit('image', image.copy())
            self.statusBar().showMessage(tr('已将标注结果作为新图片保存到内容库。'), 5000)

    def open_annotation(self, image):
        if self.quitting or image.isNull():
            return None
        from .annotation import AnnotationDialog
        dialog = AnnotationDialog(image, self, preferences=self.screenshot_preferences)
        dialog.preferencesChanged.connect(self._update_screenshot_preferences)
        dialog.saveToLibraryRequested.connect(self._save_tool_image)
        return self._show_image_tool(dialog)

    def annotate_image(self):
        image, _path = self._selected_tool_image()
        return self.open_annotation(image) if image is not None else None

    def _open_compression(self, image, path=None):
        if self.quitting or image.isNull():
            return None
        from .image_compression import CompressionDialog
        try:
            size = path.stat().st_size if path else None
            dialog = CompressionDialog(image, self, source_name=path.stem if path else tr('图片'), source_size=size, source_path=path)
        except (OSError, ValueError) as exc:
            self.report_error(tr('无法打开图片压缩：{exc}', exc=exc))
            return None
        dialog.fileCopyRequested.connect(lambda result: self._copy_compressed_file(result, dialog))
        return self._show_image_tool(dialog)

    def compress_image(self):
        image, path = self._selected_tool_image()
        return self._open_compression(image, path) if image is not None else None

    def compress_local_image(self):
        if self.quitting:
            return None
        name, _ = QFileDialog.getOpenFileName(self, tr('选择要压缩的图片'), '', tr('图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)'))
        if not name:
            return None
        reader = QImageReader(name)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            self.report_error(tr('这张图片无法读取，请选择普通图片文件。'))
            return None
        return self._open_compression(image, Path(name))

    def _copy_compressed_file(self, result, dialog=None):
        if self.quitting:
            return
        from .file_transfer import encoded_image_file, file_mime_data
        try:
            suffix = {'JPEG': '.jpg', 'WEBP': '.webp', 'PNG': '.png'}[result.format.upper()]
            path = encoded_image_file(self.vault, result.data, tr('压缩图片') + suffix)
            mime = file_mime_data([path])
            self._copy_owned(lambda: QApplication.clipboard().setMimeData(mime))
            self.statusBar().showMessage(tr('已复制压缩文件（{value_0:.1f} KB），可在目标窗口粘贴。', value_0=len(result.data) / 1024), 6000)
            if dialog is not None:
                dialog.status.setText(tr('已复制 {value_0:.1f} KB 的压缩文件，可粘贴发送。', value_0=len(result.data) / 1024))
            return path
        except (OSError, ValueError, KeyError) as exc:
            if dialog is not None:
                dialog.status.setText(tr('复制失败：{exc}', exc=exc))
            self.report_error(tr('复制压缩文件失败：{exc}', exc=exc))

    def gallery_menu(self, position):
        item = self.gallery.itemAt(position)
        if item is None:
            return
        if item.isSelected():
            self.gallery.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
        else:
            self.gallery.setCurrentItem(item)
        keys = self.selected_keys()
        menu = QMenu(self)
        row = item.data(Qt.ItemDataRole.UserRole)
        if len(keys) > 1:
            menu.addAction(tr('修改 {value_0} 条内容的标签…', value_0=len(keys)), lambda: self.edit_tags(keys))
            menu.addAction(tr('恢复自动分类'), lambda: self.restore_auto_classification(keys))
        elif row.get('item_type', 'image') == 'image':
            menu.addAction(glyph('pin'), tr('贴到屏幕'), self.pin_image)
            menu.addAction(tr('放大预览'), self.preview)
            menu.addAction(tr('复制原图'), self.copy_image)
            menu.addAction(tr('标注这张图'), self.annotate_image)
            menu.addAction(tr('压缩这张图'), self.compress_image)
        else:
            menu.addAction(tr('复制全文'), self.copy_text)
            if row.get('item_type') == 'link':
                menu.addAction(tr('打开链接'), self.open_link)
            menu.addAction(tr('导出 TXT'), self.export_image)
        if len(keys) <= 1:
            menu.addSeparator()
            menu.addAction(tr('修改标签…'), lambda: self.edit_tags(keys))
            menu.addAction(tr('恢复自动分类'), lambda: self.restore_auto_classification(keys))
        try:
            menu.exec(self.gallery.viewport().mapToGlobal(position))
        finally:
            menu.deleteLater()

    def pin_image(self):
        if self.quitting:
            return None
        row = self.vault.get(self.active_id) if self.active_id else None
        return self.pin_row(row)

    def pin_row(self, row, state=None):
        if self.quitting or not row or row.get('item_type', 'image') != 'image':
            return None
        from .pinboard import PinnedImageWindow
        try:
            pin = PinnedImageWindow(self.vault.images / row['filename'], title=row['title'], item_id=row['id'])
        except (OSError, ValueError) as exc:
            self.report_error(tr('无法贴图：{exc}', exc=exc))
            return None
        if state:
            pin.restore_state(state)
        self._show_pin(pin, offset=not state)
        if not state:
            self.statusBar().showMessage(tr('截图已置顶 · 拖动移动，滚轮缩放；右键可让点击落到下层窗口，图边按钮可恢复。'), 7000)
        return pin

    def _show_pin(self, pin, offset=True):
        pin.setWindowIcon(self.windowIcon())
        if offset:
            step = len(self.pinned_windows) % 8 * 24
            pin.move(pin.x() + step, pin.y() + step)
        self.pinned_windows.append(pin)
        pin.destroyed.connect(lambda _=None, current=pin: self._pin_closed(current))
        pin.copy_requested.connect(self._copy_pin)
        pin.click_through_changed.connect(lambda *_: self._update_pin_action())
        pin.show()
        self._update_pin_action()

    def _copy_pin(self, pin):
        image = pin.source_image()
        if image.isNull():
            return self.report_error(tr('原图不存在或无法读取'))
        self._copy_owned(lambda: QApplication.clipboard().setImage(image))
        self.statusBar().showMessage(tr('已复制贴图原图'), 4000)

    def restore_pin_input(self):
        for pin in list(self.pinned_windows):
            pin.set_click_through(False)
        self._update_pin_action()

    def save_pin_state(self):
        self.vault.set_setting('pins_state', [pin.state() for pin in self.pinned_windows if pin.item_id])

    def restore_pins(self):
        """Reopen last session's pins at their place; click-through is never restored."""
        states = self.vault.setting('pins_state', [])
        for state in states[:20] if isinstance(states, list) else ():
            row = self.vault.get(state.get('id')) if isinstance(state, dict) else None
            if row:
                self.pin_row(row, state)

    def _pin_closed(self, pin):
        if pin in self.pinned_windows:
            self.pinned_windows.remove(pin)
        self._update_pin_action()

    def _update_pin_action(self):
        if self.tray:
            self.tray_close_pins.setText(tr('关闭全部贴图（{value_0}）', value_0=len(self.pinned_windows)))
            self.tray_close_pins.setEnabled(bool(self.pinned_windows))
            passing = sum((pin.click_through for pin in self.pinned_windows))
            self.tray_pin_input.setText(tr('恢复贴图鼠标操作（{passing}）', passing=passing))
            self.tray_pin_input.setVisible(bool(passing))

    def close_all_pins(self):
        pins, self.pinned_windows = (self.pinned_windows, [])
        for pin in pins:
            pin.close()
        self._update_pin_action()

    def export_image(self):
        row = self.vault.get(self.active_id)
        if not row:
            return
        stamp = datetime.fromisoformat(row['captured_at']).astimezone().strftime('%Y-%m-%d_%H-%M-%S')
        if row.get('item_type', 'image') != 'image':
            name = f"{stamp}_{safe_title(row['title'])}.txt"
            path, _ = QFileDialog.getSaveFileName(self, tr('导出文字'), name, tr('纯文本 (*.txt)'))
            if path:
                try:
                    Path(path).write_text(row.get('content', ''), encoding='utf-8', newline='')
                    self.statusBar().showMessage(tr('全文已导出为 UTF-8 TXT'), 5000)
                except OSError as exc:
                    self.report_error(tr('导出失败：{exc}', exc=exc))
            return
        name = f"{stamp}_{safe_title(row['title'])}.png"
        path, _ = QFileDialog.getSaveFileName(self, tr('导出原图'), name, tr('PNG 图片 (*.png)'))
        if path:
            try:
                shutil.copy2(self.vault.images / row['filename'], path)
                self.statusBar().showMessage(tr('原图已导出'), 5000)
            except OSError as exc:
                self.report_error(tr('导出失败：{exc}', exc=exc))

    def delete_image(self):
        if self.active_id and QMessageBox.question(self, tr('删除内容'), tr('永久删除这条记录及其保存内容？此操作不可撤销。'), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                self.vault.delete(self.active_id)
                self.active_id = None
                self.refresh()
            except OSError as exc:
                self.report_error(tr('删除失败：{exc}', exc=exc))

    def settings_dialog(self):
        from .preferences import PreferencesPanel
        dialog = QDialog(self)
        configure_window_glass(dialog)
        dialog.setWindowTitle(tr('偏好设置 · {APP_NAME}', APP_NAME=app_name()))
        dialog.resize(1020, 780)
        dialog.setMinimumSize(780, 620)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        panel = PreferencesPanel(self)
        panel.layout().setContentsMargins(16, 14, 16, 14)
        panel.saved.connect(dialog.accept)
        panel.cancelled.connect(dialog.reject)
        layout.addWidget(panel)
        material_preview = lambda theme_id: refresh_window_glass(dialog, theme(theme_id)['dark'], enabled=theme(theme_id)['glass'])
        self.theme_changed.connect(material_preview)
        dialog._window_material_controller.dark = theme(self.theme_id)['dark']
        dialog._window_material_controller.requested = theme(self.theme_id)['glass']
        try:
            dialog.exec()
        finally:
            self.theme_changed.disconnect(material_preview)
            saved_theme = normalize_theme(self.vault.setting('theme', 'light'))
            if self.theme_id != saved_theme:
                self.set_theme(saved_theme)
            dialog.deleteLater()

    def retry_all(self):
        for key in self.vault.pending(include_errors=True):
            self.ocr.enqueue(key)
        self.statusBar().showMessage(tr('已重新排队等待识别'), 5000)

    def closeEvent(self, event):
        if not self.quitting and self.tray:
            event.ignore()
            self.hide()
            if not self.vault.setting('tray_hint_seen', False):
                self.tray.showMessage(tr('{APP_NAME} 仍在后台运行', APP_NAME=app_name()), tr('会按当前开关继续自动收集，屏幕贴图保持置顶。点击托盘图标打开图库，右键可暂停或退出。'))
                self.vault.set_setting('tray_hint_seen', True)
        elif not self.quitting:
            event.ignore()
            self.quit_app()
        else:
            event.accept()

    def hideEvent(self, event):
        self.card_delegate.cache.clear()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        native_titlebar(self, self.theme_id)
        p = theme(self.theme_id)
        refresh_window_glass(self, p['dark'], self.centralWidget(), enabled=p['glass'])

    def quit_app(self):
        if self.quitting:
            return
        self.quitting = True
        self._hotkey_rebind_timer.stop()
        if self.inspector_window is not None:
            self.inspector_window.close()
        for dialog in list(self.image_tool_dialogs):
            dialog.close()
        for dialog in list(self.screenshot_text_windows):
            dialog.close()
        try:
            self.save_pin_state()
        except Exception:
            pass
        self.close_all_pins()
        self.quick_key.unregister()
        self.pin_key.unregister()
        self.shot_key.unregister()
        self.full_key.unregister()
        if self.shot_session is not None:
            self.shot_session.close()
        if self.quick_panel is not None:
            self.quick_panel.shutdown()
        self.recent_files.stop()
        if self.trainer is not None:
            self.trainer.stopping.set()
        if self.tab_scroller:
            self.tab_scroller.stop()
        self.listening = False
        self.cleanup_timer.stop()
        self.ocr.stopping.set()
        if self.classifier:
            self.classifier.stopping.set()
        self.setEnabled(False)
        self.statusBar().showMessage(tr('正在保存剩余内容，请稍候…'))
        self.shutdown_timer = QTimer(self)
        self.stop_queued = False
        self.shutdown_timer.timeout.connect(self.finish_quit)
        self.shutdown_timer.start(100)

    def set_language(self, code):
        from .i18n import normalize_language, language
        code = normalize_language(code)
        self.vault.set_setting('ui_language', code)
        if code == language():
            return
        if (self.image_tool_dialogs or self.screenshot_text_windows or self.shot_session is not None
                or any(not pin.item_id for pin in self.pinned_windows)):
            self.notify(tr('语言已保存。请先关闭图片编辑器，下次启动时切换界面语言。'))
            return
        self.language_restart_requested = True
        QTimer.singleShot(0, self.quit_app)

    def finish_quit(self):
        if not self.stop_queued:
            self.stop_queued = self.capture.submit('stop')
        if not self.capture.isRunning() and (not self.ocr.isRunning()) and (not self.classifier or not self.classifier.isRunning()) and (not self.trainer or not self.trainer.isRunning()) and (self.quick_panel is None or not self.quick_panel.authentication_busy):
            self.shutdown_timer.stop()
            self.vault.checkpoint()
            if self.tray:
                self.tray.hide()
            QApplication.quit()
