"""Native preferences cards, shared by the settings page and its modal preview."""
from __future__ import annotations
from .i18n import tr, language
from PySide6.QtCore import QEvent, Qt, QSize, QRectF, QUrl, Signal, QTimer
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QBoxLayout, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QTabWidget, QVBoxLayout, QWidget
from . import autostart
from .branding import app_name
from .clipboard_policy import DEFAULT_EXCLUDED_APPS, normalize_app_list
from .hotkey import DEFAULT_FULLSCREEN_HOTKEY, DEFAULT_PIN_HOTKEY, DEFAULT_QUICK_HOTKEY, DEFAULT_REGION_HOTKEY, hotkey_label
from .hotkey_edit import HotkeyEdit
from .tab_scroller import FOCUS_DELAYS
from .theme import THEMES, normalize_theme, preferences_style, theme
from .screenshot_preferences import PREFERENCE_KEY, normalize_preferences

class ToggleSwitch(QCheckBox):
    """A keyboard-accessible QCheckBox painted as a compact Windows-style switch."""

    def __init__(self, text, object_name, parent=None):
        super().__init__(text, parent)
        self.setObjectName(object_name)
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(46, 28)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def sizeHint(self):
        return QSize(46, 28)

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active, enabled = (self.isChecked(), self.isEnabled())
        colors = theme()
        track = QColor(colors['accent'] if active else colors['switch_off'])
        if not enabled:
            track = QColor(colors['disabled_bg'])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(2, 3, 42, 22), 11, 11)
        knob = colors['text'] if not active and colors.get('dark') else colors['switch_knob']
        painter.setBrush(QColor(knob))
        painter.drawEllipse(QRectF(24 if active else 4, 5, 18, 18))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(colors['accent']), 1))
            painter.drawRoundedRect(QRectF(0.5, 0.5, 45, 27), 13, 13)

class ThemePreview(QWidget):
    """Small, local illustration of a theme without loading gallery content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme_id = 'light'
        self.setObjectName('themePreview')
        self.setAccessibleName(tr('主题配色预览'))
        self.setMinimumHeight(200)
        self.setMaximumHeight(220)

    def set_theme(self, theme_id):
        self.theme_id = normalize_theme(theme_id)
        self.update()

    def paintEvent(self, event):
        colors = theme(self.theme_id)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(colors['border']), 1))
        painter.setBrush(QColor(colors['canvas']))
        painter.drawRoundedRect(bounds, 10, 10)
        painter.save()
        clip = self.rect().adjusted(2, 2, -2, -2)
        painter.setClipRect(clip)
        sidebar_width = min(130, max(90, self.width() * 0.2))
        painter.fillRect(QRectF(2, 2, sidebar_width, self.height() - 4), QColor(colors['surface']))
        painter.setPen(QColor(colors['text']))
        painter.drawText(QRectF(18, 15, sidebar_width - 20, 24), app_name())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors['accent_soft']))
        painter.drawRoundedRect(QRectF(12, 54, sidebar_width - 20, 30), 6, 6)
        painter.setPen(QColor(colors['accent']))
        painter.drawText(QRectF(23, 55, sidebar_width - 35, 28), Qt.AlignmentFlag.AlignVCenter, tr('全部内容'))
        painter.setPen(QColor(colors['muted']))
        painter.drawText(QRectF(23, 96, sidebar_width - 35, 24), tr('我的收藏'))
        painter.drawText(QRectF(23, 133, sidebar_width - 35, 24), tr('自动分类'))
        content_x = sidebar_width + 20
        content_width = max(30, self.width() - content_x - 18)
        painter.setPen(QColor(colors['text']))
        painter.drawText(QRectF(content_x, 16, content_width, 26), tr('你的内容，一目了然'))
        card_gap = 12
        card_width = max(20, (content_width - card_gap) / 2)
        for index, label in enumerate((tr('截图'), tr('文字与链接'))):
            x = content_x + index * (card_width + card_gap)
            painter.setPen(QPen(QColor(colors['border']), 1))
            painter.setBrush(QColor(colors['card']))
            painter.drawRoundedRect(QRectF(x, 56, card_width, 126), 8, 8)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors['image_preview'] if index == 0 else colors['link_preview']))
            painter.drawRoundedRect(QRectF(x + 9, 65, card_width - 18, 65), 5, 5)
            painter.setPen(QColor(colors['text']))
            painter.drawText(QRectF(x + 12, 140, card_width - 24, 22), label)
        painter.restore()

def _label(text, name, wrap=False):
    control = QLabel(text)
    control.setObjectName(name)
    control.setTextFormat(Qt.TextFormat.PlainText)
    control.setWordWrap(wrap)
    if wrap:
        control.setMinimumWidth(0)
        control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        control.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    return control

class _SettingCard(QFrame):
    """Keep a wide settings row, stacking its editor when reading gets cramped."""

    def __init__(self):
        super().__init__()
        self.header = None
        self.trailing_control = None

    def set_trailing_control(self, control):
        self.trailing_control = control
        control.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._arrange_header()

    def _arrange_header(self):
        if getattr(self, 'header', None) is None or getattr(self, 'trailing_control', None) is None:
            return
        control = self.trailing_control
        preferred_width = max(control.minimumWidth(), control.minimumSizeHint().width(), control.sizeHint().width())
        margins = self.layout().contentsMargins()
        available = self.width() - margins.left() - margins.right()
        horizontal = available >= 360 + self.header.spacing() + preferred_width
        if self.property('stackedEditor') == (not horizontal):
            return
        direction = QBoxLayout.Direction.LeftToRight if horizontal else QBoxLayout.Direction.TopToBottom
        self.header.setDirection(direction)
        self.header.setStretch(0, 1 if horizontal else 0)
        self.header.setStretch(1, 0)
        self.header.setAlignment(control, Qt.AlignmentFlag.AlignTop if horizontal else Qt.AlignmentFlag.AlignLeft)
        self.setProperty('stackedEditor', not horizontal)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_header()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange, QEvent.Type.LayoutRequest):
            self._arrange_header()
        return result

def _button(text, name, callback):
    control = QPushButton(text)
    control.setObjectName(name)
    control.setCursor(Qt.CursorShape.PointingHandCursor)
    control.clicked.connect(callback)
    return control

def _theme_icon(theme_id):
    colors = theme(theme_id)
    swatches = QPixmap(64, 16)
    swatches.fill(Qt.GlobalColor.transparent)
    painter = QPainter(swatches)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for index, token in enumerate(('canvas', 'card', 'accent', 'text')):
        painter.setPen(QPen(QColor(colors['border']), 1))
        painter.setBrush(QColor(colors[token]))
        painter.drawRoundedRect(QRectF(index * 16 + 0.5, 0.5, 13, 14), 3, 3)
    painter.end()
    return QIcon(swatches)

class PreferencesPanel(QWidget):
    """Edit a MainWindow's preferences; only apply() persists configuration.

    Connect saved/cancelled to the host's navigation or to QDialog.accept/reject.
    A persistent embedded panel can call reload() whenever it is shown.
    """
    saved = Signal()
    cancelled = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self._hotkey_status_bindings = []
        self.setObjectName('settingsPage')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(preferences_style(getattr(host, 'theme_id', None)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        heading = QFrame()
        heading.setObjectName('settingsIntro')
        heading_layout = QVBoxLayout(heading)
        heading_layout.setContentsMargins(2, 2, 2, 0)
        heading_layout.setSpacing(7)
        heading_layout.addWidget(_label(tr('偏好设置'), 'settingsTitle'))
        heading_layout.addWidget(_label(tr('管理内容收集、浏览器标签操作与界面外观。修改后点击保存生效。'), 'settingDescription', True))
        layout.addWidget(heading)
        self.pages = QTabWidget()
        self.pages.setObjectName('preferencesTabs')
        layout.addWidget(self.pages, 1)
        self.capture_layout = self._page(tr('内容收集'), 'captureSettings')
        self.browser_layout = self._page(tr('浏览器标签'), 'browserSettings')
        self.appearance_layout = self._page(tr('外观'), 'appearanceSettings')
        self.privacy_layout = self._page(tr('快捷与隐私'), 'privacySettings')
        self._build_capture()
        self._build_privacy()
        self._build_browser()
        self._build_appearance()
        self._build_footer(layout)
        self.pages.currentChanged.connect(lambda _index: self.stop_hotkey_recording())
        self.reload()
        if hasattr(host, 'capture_state_changed'):
            host.capture_state_changed.connect(self.capture_enabled.setChecked)
        if hasattr(host, 'text_capture_state_changed'):
            host.text_capture_state_changed.connect(self.text_capture_enabled.setChecked)
        if hasattr(host, 'tabs_state_changed'):
            host.tabs_state_changed.connect(self.tabs_enabled.setChecked)
        if hasattr(host, 'theme_changed'):
            host.theme_changed.connect(self._theme_changed)
        if hasattr(host, 'hotkeys_state_changed'):
            host.hotkeys_state_changed.connect(self._refresh_hotkey_status)
        worker = getattr(host, 'ocr', None)
        if worker is not None:
            worker.availability.connect(self._ocr_availability)
            worker.changed.connect(self._refresh_failures)

    def _page(self, title, name):
        scroll = QScrollArea()
        scroll.setObjectName('preferencesScroll')
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName('preferencesContent')
        content.setProperty('page', name)
        page_layout = QVBoxLayout(content)
        page_layout.setContentsMargins(0, 0, 10, 8)
        page_layout.setSpacing(12)
        scroll.setWidget(content)
        self.pages.addTab(scroll, title)
        return page_layout

    def _card(self, page_layout, title, description):
        card = _SettingCard()
        card.setObjectName('settingCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)
        header = QHBoxLayout()
        header.setSpacing(18)
        card.header = header
        text = QVBoxLayout()
        text.setSpacing(5)
        text.addWidget(_label(title, 'settingTitle'))
        if description:
            text.addWidget(_label(description, 'settingDescription', True))
        header.addLayout(text, 1)
        card_layout.addLayout(header)
        page_layout.addWidget(card)
        return (card, card_layout, header)

    def _switch_card(self, page_layout, title, description, name):
        card, _, header = self._card(page_layout, title, description)
        control = ToggleSwitch(title, name)
        header.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        return (control, card)

    def _build_capture(self):
        page = self.capture_layout
        page.addWidget(_label(tr('内容收集与保留策略'), 'sectionTitle'))
        self.capture_enabled, _ = self._switch_card(page, tr('启用剪贴板自动收集'), tr('自动保存图片；启用下方开关后也收集文字和链接。暂停会停止所有自动收集。'), 'captureEnabled')
        self.autostart, self.autostart_card = self._switch_card(page, tr('开机时自动启动'), tr('登录 Windows 后在托盘中启动，不弹出窗口，避免漏收开机后的截图。只写入当前用户的启动项，关闭后立即移除。'), 'autostartEnabled')
        if not autostart.supported():
            self.autostart.setEnabled(False)
            self.autostart_card.setToolTip(tr('从源码运行时不可用，仅打包后的程序支持开机启动。'))
        self.text_capture_enabled, _ = self._switch_card(page, tr('自动保存文字与链接'), tr('默认关闭。开启后将你复制的文字和网址保存到本机，并按内容自动分类；不会访问网址获取预览。'), 'collectTextEnabled')
        _, _, dedup_header = self._card(page, tr('自动去重'), tr('图片按像素去重，文字与链接按内容去重，相同内容只保存一份。'))
        dedup_header.addWidget(_label(tr('始终开启'), 'settingBadge'))
        _, retention_layout, _ = self._card(page, tr('未收藏内容的保留时间'), tr('到期后自动清理。已收藏的内容永久保留，不受此限制。'))
        strip = QFrame()
        strip.setObjectName('retentionStrip')
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(5, 5, 5, 5)
        strip_layout.setSpacing(4)
        self.retention_group = QButtonGroup(self)
        self.retention_group.setObjectName('retentionDays')
        for days, text in ((7, tr('7 天')), (14, tr('14 天')), (30, tr('30 天')), (90, tr('90 天')), (0, tr('永久保留'))):
            choice = QPushButton(text)
            choice.setObjectName(f'retention{days}')
            choice.setProperty('retentionOption', True)
            choice.setCheckable(True)
            choice.setCursor(Qt.CursorShape.PointingHandCursor)
            self.retention_group.addButton(choice, days)
            strip_layout.addWidget(choice, 1)
        retention_layout.addWidget(strip)
        retention_layout.addWidget(_label(tr('缩短保留时间后，保存设置会立即清理已到期的未收藏内容。'), 'settingDescription', True))
        _, path_layout, _ = self._card(page, tr('本地数据存储路径'), tr('图片、文字、链接及搜索索引保存在本机。此处仅显示当前目录，不支持直接迁移。'))
        path_row = QHBoxLayout()
        self.storage_path = QLineEdit(str(self.host.vault.root))
        self.storage_path.setObjectName('storagePath')
        self.storage_path.setReadOnly(True)
        self.storage_path.setAccessibleName(tr('本地数据存储路径'))
        path_row.addWidget(self.storage_path, 1)
        path_row.addWidget(_button(tr('打开数据目录'), 'openStorageFolder', self._open_storage))
        path_layout.addLayout(path_row)
        _, language_layout, language_header = self._card(page, tr('离线文字识别'), tr('使用离线小模型自动识别中英文，截图逐张排队，识别完成后释放模型。已有识别结果不会自动重算。'))
        self.language_status = _label('', 'settingBadge')
        language_header.addWidget(self.language_status)
        self.language = QComboBox()
        self.language.setObjectName('ocrLanguage')
        self.language.setAccessibleName(tr('自动识别中英文'))
        language_layout.addWidget(self.language)
        self.language_warning = _label('', 'settingDescription', True)
        language_layout.addWidget(self.language_warning)
        _, _, retry_header = self._card(page, tr('重试失败的文字识别'), tr('将失败项目重新放入识别队列；重试失败时保留原有文字，成功后更新结果。'))
        self.retry_button = _button(tr('重试失败识别'), 'retryFailedOcr', self._retry_ocr)
        retry_header.addWidget(self.retry_button)
        self.failure_status = _label('', 'settingDescription')
        retry_header.itemAt(0).layout().addWidget(self.failure_status)
        _, _, rescan_header = self._card(page, tr('为旧截图补充文字位置和二维码'), tr('1.5.0 之前识别的截图没有文字位置，1.6.0 之前的没有识别二维码。重新识别一次后即可框选文字、查看二维码；原有文字、标题和标签都会保留。'))
        self.rescan_button = _button(tr('开始补充'), 'rescanOldImages', self._rescan_images)
        rescan_header.addWidget(self.rescan_button)
        self.rescan_status = _label('', 'settingDescription')
        rescan_header.itemAt(0).layout().addWidget(self.rescan_status)
        page.addWidget(_label(tr('内容按本地规则分类，不上传、不自动访问链接。数据未加密。关闭主窗口后仍可在托盘继续运行。'), 'settingDescription', True))
        page.addStretch()

    def _build_privacy(self):
        page = self.privacy_layout
        page.addWidget(_label(tr('全局快捷键'), 'sectionTitle'))
        page.addWidget(_label(tr('点击按键框，或聚焦后按 Enter / 空格开始录入；Esc 取消。修改后请保存。录入结束后会自动恢复其他快捷键。'), 'settingDescription', True))
        self.full_hotkey = self._hotkey_card(page, tr('全屏截图'), tr('截取鼠标所在的整个屏幕并复制；是否入库使用下面的截图设置。'), DEFAULT_FULLSCREEN_HOTKEY, 'fullscreenHotkey')
        self.full_hotkey.field.setToolTip(self.full_hotkey.field.toolTip() + tr('\n使用 PrtSc 时，该按键可能由 Windows 系统截图工具接管。'))
        self.shot_hotkey = self._hotkey_card(page, tr('区域截图'), tr('框选后可标注，选择仅复制或复制并入库；也可另存、贴图或识字。'), DEFAULT_REGION_HOTKEY, 'screenshotHotkey')
        self.pin_hotkey = self._hotkey_card(page, tr('把剪贴板图片贴到屏幕'), tr('将剪贴板图片贴到屏幕，支持拖动、缩放；是否入库使用截图设置。'), DEFAULT_PIN_HOTKEY, 'pinHotkey')
        self.quick_hotkey = self._hotkey_card(page, tr('随手面板'), tr('在任何程序里呼出内容库、最近文件和中转篮。'), DEFAULT_QUICK_HOTKEY, 'quickHotkey')
        self.screenshot_keep, _ = self._switch_card(page, tr('截图默认存入内容库'), tr('关闭后，全屏截图、贴图和识字默认不入库。区域截图仍可单次选择“复制并入库”，并记住选择。与剪贴板自动收集分开控制。'), 'screenshotKeep')
        self.quick_autopaste, _ = self._switch_card(page, tr('回车后自动粘贴到原窗口'), tr('关闭后回车只复制，由你自己粘贴。部分以管理员身份运行的程序不接受自动粘贴，届时内容仍已复制。'), 'quickAutopaste')
        self.restore_pins, _ = self._switch_card(page, tr('启动时恢复上次的贴图'), tr('退出时记录贴图的位置、缩放和不透明度，下次启动后放回原处。鼠标穿透不会被恢复。'), 'restorePins')
        page.addWidget(_label(tr('隐私保护'), 'sectionTitle'))
        self.skip_sensitive, _ = self._switch_card(page, tr('跳过疑似敏感的文字'), tr('自动收集文字时，跳过疑似密钥或令牌、私钥、写明的密码、通过校验的身份证号和银行卡号。只是常见格式的本地识别，不能保证找出全部敏感信息；手动粘贴不受影响。'), 'skipSensitive')
        _, apps_layout, apps_header = self._card(page, tr('不收集这些程序复制的内容'), tr('按复制来源的程序名排除，每行一个，例如 KeePass.exe。图片、文字和链接都会跳过。'))
        apps_header.addWidget(_button(tr('恢复默认'), 'restoreExcludedApps', self._restore_default_apps))
        self.excluded_apps = QPlainTextEdit()
        self.excluded_apps.setObjectName('excludedApps')
        self.excluded_apps.setAccessibleName(tr('不收集的程序列表'))
        self.excluded_apps.setFixedHeight(130)
        apps_layout.addWidget(self.excluded_apps)
        page.addWidget(_label(tr('程序名在本机读取，不会上传。系统级的「不记录剪贴板历史」标记始终生效。'), 'settingDescription', True))
        page.addStretch()

    def _hotkey_card(self, page, title, description, default, name):
        card, _, header = self._card(page, title, description)
        edit = HotkeyEdit(default)
        edit.setObjectName(name)
        edit.field.setAccessibleName(tr('{title}的快捷键', title=title))
        edit.setMinimumWidth(300)
        status = _label('', 'settingDescription', True)
        status.setAccessibleName(tr('{title}的快捷键状态', title=title))
        edit.registration_status = status
        edit.layout().addWidget(status)
        registration, value = {'fullscreenHotkey': ('full_key', 'full_hotkey'), 'screenshotHotkey': ('shot_key', 'shot_hotkey'), 'pinHotkey': ('pin_key', 'pin_hotkey'), 'quickHotkey': ('quick_key', 'quick_hotkey')}[name]
        self._hotkey_status_bindings.append((edit, status, registration, value))
        edit.recording.connect(self._recording_changed)
        edit.changed.connect(self._refresh_hotkey_status)
        header.addWidget(edit, 0, Qt.AlignmentFlag.AlignTop)
        edit.setToolTip(tr('单键可用 F1–F24、PrtSc、Pause、Scroll Lock；字母、数字等请配合 Ctrl、Alt、Shift 或 Win。'))
        card.set_trailing_control(edit)
        return edit

    def _recording_changed(self, _recording):
        if hasattr(self.host, 'suspend_hotkeys'):
            self.host.suspend_hotkeys(any((edit.is_recording for edit, *_ in self._hotkey_status_bindings)))
        self._refresh_hotkey_status()

    def stop_hotkey_recording(self):
        """Cancel drafts in progress before leaving, hiding, saving or reloading."""
        for edit, *_ in self._hotkey_status_bindings:
            edit.stop_recording()
        self._refresh_hotkey_status()

    def _refresh_hotkey_status(self, *_args):
        host = self.host
        suspended = bool(getattr(host, '_hotkeys_suspended', False) or any((edit.is_recording for edit, *_ in self._hotkey_status_bindings)))
        for edit, status, registration_name, value_name in self._hotkey_status_bindings:
            value = edit.hotkey()
            saved = getattr(host, value_name, value)
            registration = getattr(host, registration_name, None)
            error = getattr(registration, 'error', '')
            failed = False
            if suspended:
                text = tr('正在录入，其他全局快捷键暂时暂停')
            elif value != saved:
                text = tr('待保存，保存后生效') if value else tr('待保存，保存后将取消此项快捷键')
            elif not value:
                text = tr('未绑定，此项没有快捷键')
            elif not getattr(host, 'hotkeys_enabled', False):
                text = tr('未注册')
            elif error:
                text, failed = (tr('未启用：') + error, True)
            elif getattr(registration, 'active', False) and getattr(registration, 'text', saved) == saved:
                text = tr('已启用，可在其他程序使用')
            else:
                text = tr('未注册，请保存设置后重试')
            status.setText(text)
            status.setStyleSheet('color: ' + theme()['danger' if failed else 'secondary'] + ';')

    def hideEvent(self, event):
        self.stop_hotkey_recording()
        super().hideEvent(event)

    def _restore_default_apps(self):
        self.excluded_apps.setPlainText('\n'.join(DEFAULT_EXCLUDED_APPS))

    def _build_browser(self):
        page = self.browser_layout
        page.addWidget(_label(tr('浏览器标签操作'), 'sectionTitle'))
        self.tabs_enabled, _ = self._switch_card(page, tr('启用浏览器标签操作'), tr('鼠标停在浏览器顶部标签区域时，滚动滚轮即可切换标签页。'), 'tabsEnabled')
        self.reverse, _ = self._switch_card(page, tr('反转滚轮方向'), tr('默认向上切换前一页、向下切换后一页；开启后方向相反。'), 'tabsReverse')
        self.double_click, _ = self._switch_card(page, tr('双击标签区域关闭当前标签页'), tr('沿用你添加的双击关闭功能，在浏览器顶部标签区域双击时执行。'), 'tabsDoubleClick')
        self.autofocus, _ = self._switch_card(page, tr('允许操作后台浏览器'), tr('浏览器未在前台时，先聚焦再执行标签操作，随后按下面的模式处理窗口。'), 'tabsAutofocus')
        self.focus_card, focus_layout, _ = self._card(page, tr('自动聚焦后的窗口行为'), tr('三种行为与原版一致。默认完整恢复窗口顺序。'))
        choices_layout = QHBoxLayout()
        choices_layout.setSpacing(8)
        self.focus_modes = QButtonGroup(self)
        self.focus_modes.setObjectName('focusModes')
        self.focus_frames = []
        choices = ((0, tr('保持浏览器在前台'), tr('操作后继续在浏览器窗口中工作。')), (1, tr('快速切回原窗口'), tr('操作后切回先前的窗口，采用简化的顺序恢复。')), (2, tr('完整恢复窗口顺序'), tr('恢复窗口原本的前后顺序，再回到先前窗口。')))
        for value, title, description in choices:
            frame = QFrame()
            frame.setProperty('focusOption', True)
            frame.setProperty('selected', False)
            box = QVBoxLayout(frame)
            box.setContentsMargins(14, 14, 14, 14)
            box.setSpacing(8)
            radio = QRadioButton(title)
            radio.setObjectName(f'focusMode{value}')
            radio.setCursor(Qt.CursorShape.PointingHandCursor)
            self.focus_modes.addButton(radio, value)
            box.addWidget(radio)
            box.addWidget(_label(description, 'settingDescription', True))
            box.addStretch()
            choices_layout.addWidget(frame, 1)
            self.focus_frames.append(frame)
        self.focus_modes.idToggled.connect(self._update_focus_selection)
        focus_layout.addLayout(choices_layout)
        self.delay_card, _, delay_header = self._card(page, tr('聚焦后的等待时间'), tr('给浏览器留出响应时间后再执行标签操作，原版默认 25 毫秒。'))
        self.focus_delay = QComboBox()
        self.focus_delay.setObjectName('focusDelay')
        self.focus_delay.setAccessibleName(tr('聚焦后的等待时间'))
        for delay in FOCUS_DELAYS:
            self.focus_delay.addItem(tr('{delay} 毫秒', delay=delay) + (tr('（默认）') if delay == 25 else ''), delay)
        self.focus_delay.setMinimumWidth(160)
        delay_header.addWidget(self.focus_delay)
        self.engine_status = _label('', 'settingDescription', True)
        self.engine_status.setObjectName('browserEngineStatus')
        page.addWidget(self.engine_status)
        page.addWidget(_label(tr('请保持原独立滚轮工具关闭，以免重复处理。标签区域沿用原版范围判断，请避开新建标签及窗口控制按钮。'), 'settingDescription', True))
        page.addStretch()
        self.tabs_enabled.toggled.connect(self._update_dependencies)
        self.autofocus.toggled.connect(self._update_dependencies)

    def _build_footer(self, layout):
        footer = QFrame()
        footer.setObjectName('settingsFooter')
        row = QHBoxLayout(footer)
        row.setContentsMargins(18, 13, 18, 13)
        row.setSpacing(10)
        row.addWidget(_label(tr('主题即时预览，保存后保留。其他设置保存后生效。'), 'settingDescription', True), 1)
        row.addWidget(_button(tr('取消'), 'cancelPreferences', self.cancel))
        row.addWidget(_button(tr('保存设置'), 'primary', self.apply))
        layout.addWidget(footer)

    def _build_appearance(self):
        page = self.appearance_layout
        page.addWidget(_label(tr('界面外观'), 'sectionTitle'))
        _, language_layout, _ = self._card(page, tr('界面语言'), tr('保存后重启应用以切换语言；有未关闭编辑器时，下次启动生效。你的文件名、原文和识别结果不会被翻译。'))
        self.ui_language_choice = QComboBox()
        self.ui_language_choice.setObjectName('uiLanguage')
        self.ui_language_choice.setAccessibleName(tr('界面语言'))
        self.ui_language_choice.addItem('English', 'en_US')
        self.ui_language_choice.addItem(tr('简体中文'), 'zh_CN')
        language_layout.addWidget(self.ui_language_choice)
        _, card_layout, _ = self._card(page, tr('主题配色'), tr('选择后立即预览整个界面。点击保存保留选择，取消会恢复原来的主题。'))
        self.theme_selector = QComboBox()
        self.theme_selector.setObjectName('themeSelector')
        self.theme_selector.setAccessibleName(tr('界面主题'))
        self.theme_selector.setMinimumWidth(240)
        self.theme_selector.setIconSize(QSize(64, 16))
        for theme_id, spec in THEMES.items():
            if theme_id == 'glass_night':
                continue
            self.theme_selector.addItem(_theme_icon(theme_id), tr('磨砂玻璃') if spec['glass'] else app_name() + ' · ' + tr('浅色') if theme_id == 'light' else tr(spec['name']), theme_id)
        card_layout.addWidget(self.theme_selector)
        self._glass_theme_id = 'glass_day'
        self.glass_mode_row = QFrame()
        self.glass_mode_row.setObjectName('glassModeRow')
        mode_layout = QHBoxLayout(self.glass_mode_row)
        mode_layout.setContentsMargins(0, 2, 0, 2)
        mode_layout.setSpacing(10)
        self.glass_mode_group = QButtonGroup(self)
        self.glass_mode_group.setExclusive(True)
        self.glass_mode_buttons = {}
        for theme_id, label in (('glass_day', tr('日间')), ('glass_night', tr('夜间'))):
            mode = QPushButton(label)
            mode.setObjectName('glassDayMode' if theme_id == 'glass_day' else 'glassNightMode')
            mode.setProperty('glassMode', True)
            mode.setAccessibleName(tr('磨砂玻璃{mode}模式', mode=label))
            mode.setCheckable(True)
            mode.setMinimumHeight(44)
            mode.setMinimumWidth(104)
            mode.setCursor(Qt.CursorShape.PointingHandCursor)
            self.glass_mode_group.addButton(mode)
            self.glass_mode_buttons[theme_id] = mode
            mode_layout.addWidget(mode)
            mode.clicked.connect(lambda checked=False, value=theme_id: self._preview_glass_mode(value))
        mode_layout.addStretch()
        card_layout.addWidget(self.glass_mode_row)
        self.theme_description = _label('', 'settingDescription', True)
        card_layout.addWidget(self.theme_description)
        self.theme_preview = ThemePreview()
        card_layout.addWidget(self.theme_preview)
        self.theme_selector.currentIndexChanged.connect(self._preview_theme)
        page.addStretch()

    def _preview_theme(self, *_):
        theme_id = self.selected_theme_id()
        self.host.set_theme(theme_id, persist=False)

    def selected_theme_id(self):
        selected = self.theme_selector.currentData()
        return self._glass_theme_id if selected == 'glass_day' else normalize_theme(selected)

    def select_theme(self, theme_id):
        """Preview a palette, including a mode of the single glass family."""
        theme_id = normalize_theme(theme_id)
        self._theme_changed(theme_id)
        self.host.set_theme(theme_id, persist=False)

    def _preview_glass_mode(self, theme_id):
        self._glass_theme_id = theme_id
        self.host.set_theme(theme_id, persist=False)

    def _theme_changed(self, theme_id):
        theme_id = normalize_theme(theme_id)
        self.setStyleSheet(preferences_style(theme_id))
        blocked = self.theme_selector.blockSignals(True)
        glass = THEMES[theme_id]['glass']
        if glass:
            self._glass_theme_id = theme_id
        self.theme_selector.setCurrentIndex(self.theme_selector.findData('glass_day' if glass else theme_id))
        self.theme_selector.blockSignals(blocked)
        self.glass_mode_row.setVisible(glass)
        for mode_id, control in self.glass_mode_buttons.items():
            signals = control.blockSignals(True)
            control.setChecked(mode_id == self._glass_theme_id)
            control.blockSignals(signals)
        self.theme_description.setText(tr(THEMES[theme_id]['description']))
        self.theme_preview.set_theme(theme_id)
        for control in self.findChildren(ToggleSwitch):
            control.update()

    def reload(self):
        self.stop_hotkey_recording()
        host, vault = (self.host, self.host.vault)
        self.ui_language_choice.setCurrentIndex(self.ui_language_choice.findData(vault.setting('ui_language', language())))
        self.capture_enabled.setChecked(bool(host.listening))
        self.text_capture_enabled.setChecked(bool(getattr(host, 'collect_text', vault.setting('collect_text', False))))
        retention = vault.setting('retention_days', 7)
        (self.retention_group.button(retention) or self.retention_group.button(7)).setChecked(True)
        for edit, attribute, key, default in ((self.quick_hotkey, 'quick_hotkey', 'quick_paste_hotkey', DEFAULT_QUICK_HOTKEY), (self.pin_hotkey, 'pin_hotkey', 'pin_clipboard_hotkey', DEFAULT_PIN_HOTKEY), (self.full_hotkey, 'full_hotkey', 'fullscreen_hotkey', DEFAULT_FULLSCREEN_HOTKEY), (self.shot_hotkey, 'shot_hotkey', 'region_hotkey', DEFAULT_REGION_HOTKEY)):
            edit.set_hotkey(getattr(host, attribute, vault.setting(key, default)))
        self.autostart.setChecked(autostart.enabled())
        self.quick_autopaste.setChecked(bool(getattr(host, 'quick_autopaste', vault.setting('quick_paste_autopaste', True))))
        self.screenshot_keep.setChecked(normalize_preferences(getattr(host, 'screenshot_preferences', vault.setting(PREFERENCE_KEY, {})))['copy_save'])
        self.restore_pins.setChecked(bool(getattr(host, 'restore_pins_enabled', vault.setting('restore_pins', True))))
        self.skip_sensitive.setChecked(bool(getattr(host, 'skip_sensitive', vault.setting('skip_sensitive_text', True))))
        apps = getattr(host, 'excluded_apps', None)
        if apps is None:
            apps = normalize_app_list(vault.setting('excluded_apps', list(DEFAULT_EXCLUDED_APPS)))
        self.excluded_apps.setPlainText('\n'.join(apps))
        self._load_languages(getattr(host, 'ocr_languages', []), getattr(host, 'ocr_error', ''), vault.setting('ocr_language', 'auto'))
        for control, name, default in ((self.tabs_enabled, 'tabs_enabled', True), (self.reverse, 'tabs_reverse', False), (self.double_click, 'tabs_double_click', True), (self.autofocus, 'tabs_autofocus', True)):
            control.setChecked(bool(getattr(host, name, default)))
        mode = getattr(host, 'tabs_focus_mode', 2)
        (self.focus_modes.button(mode) or self.focus_modes.button(2)).setChecked(True)
        delay_index = self.focus_delay.findData(getattr(host, 'tabs_focus_delay', 25))
        self.focus_delay.setCurrentIndex(delay_index if delay_index >= 0 else self.focus_delay.findData(25))
        issue = getattr(getattr(host, 'tab_scroller', None), 'error', '')
        self.engine_status.setText(tr('当前状态：') + issue if issue else '')
        self.engine_status.setVisible(bool(issue))
        self._update_dependencies()
        self._update_focus_selection()
        self._refresh_failures()
        self._theme_changed(getattr(host, 'theme_id', vault.setting('theme', 'light')))
        self._refresh_hotkey_status()

    def _load_languages(self, languages, error, selected):
        self.language.clear()
        self.language.addItem(tr('自动识别（中英文混合）'), 'auto')
        self.language.setCurrentIndex(0)
        self.language.setEnabled(False)
        self.language_status.setText(tr('暂不可用') if error else tr('离线识别就绪') if languages else tr('正在准备'))
        warning = tr('离线识别暂不可用：') + error[:180] if error else ''
        if not languages and (not error):
            warning = tr('正在准备本地识别，图片仍会正常保存。')
        self.language_warning.setText(warning)
        self.language_warning.setVisible(bool(warning))

    def _ocr_availability(self, languages, error):
        self._load_languages(languages, error, self.language.currentData())
        self._refresh_failures()

    def _refresh_failures(self, *_):
        vault = self.host.vault
        with vault.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM screenshots WHERE ocr_status='error' AND item_type='image'").fetchone()[0]
        self.failure_status.setText(tr('当前有 {count} 项识别失败', count=count) if count else tr('目前没有识别失败的项目'))
        self.retry_button.setEnabled(bool(count))
        missing = len(vault.images_needing_rescan()) if hasattr(vault, 'images_needing_rescan') else 0
        self.rescan_status.setText(tr('有 {missing} 张截图可以补充', missing=missing) if missing else tr('所有截图都已包含文字位置和二维码识别'))
        self.rescan_button.setEnabled(bool(missing) and hasattr(self.host, 'rescan_old_images'))

    def _rescan_images(self):
        count = self.host.rescan_old_images()
        self.rescan_status.setText(tr('已将 {count} 张截图加入识别队列，可继续使用', count=count) if count else tr('没有需要补充的截图'))
        self.rescan_button.setEnabled(False)

    def _retry_ocr(self):
        self.host.retry_all()
        self.failure_status.setText(tr('失败项目已加入识别队列'))

    def _open_storage(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.host.vault.root)))

    def _update_focus_selection(self, *_):
        selected = self.focus_modes.checkedId()
        for index, frame in enumerate(self.focus_frames):
            frame.setProperty('selected', index == selected)
            frame.style().unpolish(frame)
            frame.style().polish(frame)

    def _update_dependencies(self, *_):
        enabled = self.tabs_enabled.isChecked()
        for control in (self.double_click, self.reverse, self.autofocus):
            control.setEnabled(enabled)
        focus_enabled = enabled and self.autofocus.isChecked()
        self.focus_card.setEnabled(focus_enabled)
        self.delay_card.setEnabled(focus_enabled)

    def cancel(self):
        self.stop_hotkey_recording()
        self.host.set_theme(normalize_theme(self.host.vault.setting('theme', 'light')), persist=False)
        self.reload()
        self.cancelled.emit()

    def apply(self):
        self.stop_hotkey_recording()
        host, vault = (self.host, self.host.vault)
        settings = {'ui_language': self.ui_language_choice.currentData(), 'retention_days': self.retention_group.checkedId(), 'ocr_language': 'auto', 'paused': not self.capture_enabled.isChecked(), 'collect_text': self.text_capture_enabled.isChecked(), 'tabs_enabled': self.tabs_enabled.isChecked(), 'tabs_double_click': self.double_click.isChecked(), 'tabs_reverse': self.reverse.isChecked(), 'tabs_autofocus': self.autofocus.isChecked(), 'tabs_focus_mode': self.focus_modes.checkedId(), 'tabs_focus_delay': self.focus_delay.currentData(), 'theme': self.selected_theme_id(), 'quick_paste_hotkey': self.quick_hotkey.hotkey(), 'pin_clipboard_hotkey': self.pin_hotkey.hotkey(), 'fullscreen_hotkey': self.full_hotkey.hotkey(), 'region_hotkey': self.shot_hotkey.hotkey(), 'quick_paste_autopaste': self.quick_autopaste.isChecked(), 'restore_pins': self.restore_pins.isChecked(), PREFERENCE_KEY: normalize_preferences({'copy_save': self.screenshot_keep.isChecked()}, getattr(host, 'screenshot_preferences', vault.setting(PREFERENCE_KEY, {}))), 'skip_sensitive_text': self.skip_sensitive.isChecked(), 'excluded_apps': normalize_app_list(self.excluded_apps.toPlainText())}
        chosen = [value for value in (settings['quick_paste_hotkey'], settings['pin_clipboard_hotkey'], settings['fullscreen_hotkey'], settings['region_hotkey']) if value]
        duplicate = next((value for value in chosen if chosen.count(value) > 1), None)
        if duplicate:
            host.report_error(tr('快捷键 {value_0} 被设置给了多个功能，请换一个。设置未保存。', value_0=hotkey_label(duplicate)))
            return False
        try:
            for name, value in settings.items():
                vault.set_setting(name, value)
            for name, value in settings.items():
                if name.startswith('tabs_'):
                    setattr(host, name, value)
            listening = not settings['paused']
            if host.listening != listening:
                host.clip_generation += 1
            host.listening = listening
            host.retention_days = settings['retention_days']
            host.quick_hotkey = settings['quick_paste_hotkey']
            host.pin_hotkey = settings['pin_clipboard_hotkey']
            host.full_hotkey = settings['fullscreen_hotkey']
            host.shot_hotkey = settings['region_hotkey']
            if autostart.supported() and self.autostart.isChecked() != autostart.enabled():
                if autostart.set_enabled(self.autostart.isChecked()) != self.autostart.isChecked():
                    raise OSError(tr('无法修改开机启动项'))
            host.quick_autopaste = settings['quick_paste_autopaste']
            host.restore_pins_enabled = settings['restore_pins']
            host.screenshot_preferences = settings[PREFERENCE_KEY]
            host.skip_sensitive = settings['skip_sensitive_text']
            host.excluded_apps = settings['excluded_apps']
            if hasattr(host, 'apply_quick_settings'):
                host.apply_quick_settings()
            host.set_theme(settings['theme'], persist=False)
            host.set_text_capture_enabled(settings['collect_text'], persist=False)
            host.update_capture_state()
            host.apply_tabs_settings()
            host.capture.submit('cleanup')
            host.refresh()
        except Exception as exc:
            host.report_error(tr('保存设置失败：{exc}', exc=exc))
            return False
        self.saved.emit()
        requested_language = settings['ui_language']
        if requested_language != language() and hasattr(host, 'set_language'):
            QTimer.singleShot(0, lambda: host.set_language(requested_language))
        return True
