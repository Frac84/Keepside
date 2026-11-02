"""A field that records whatever key or key combination the user presses."""
from __future__ import annotations
from .i18n import tr

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MODIFIER_VKS, format_hotkey, hotkey_label

_QT_KEYS = {getattr(Qt.Key, f"Key_{letter}"): ord(letter) for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
_QT_KEYS.update({getattr(Qt.Key, f"Key_{digit}"): ord(digit) for digit in "0123456789"})
_QT_KEYS.update({getattr(Qt.Key, f"Key_F{number}"): 0x6F + number for number in range(1, 25)})
_QT_KEYS.update({
    Qt.Key.Key_Print: 0x2C, Qt.Key.Key_Pause: 0x13, Qt.Key.Key_ScrollLock: 0x91, Qt.Key.Key_Space: 0x20,
    Qt.Key.Key_Tab: 0x09, Qt.Key.Key_Insert: 0x2D, Qt.Key.Key_Delete: 0x2E, Qt.Key.Key_Home: 0x24,
    Qt.Key.Key_End: 0x23, Qt.Key.Key_PageUp: 0x21, Qt.Key.Key_PageDown: 0x22, Qt.Key.Key_Left: 0x25,
    Qt.Key.Key_Up: 0x26, Qt.Key.Key_Right: 0x27, Qt.Key.Key_Down: 0x28, Qt.Key.Key_QuoteLeft: 0xC0,
    Qt.Key.Key_Minus: 0xBD, Qt.Key.Key_Equal: 0xBB, Qt.Key.Key_BracketLeft: 0xDB,
    Qt.Key.Key_BracketRight: 0xDD, Qt.Key.Key_Backslash: 0xDC, Qt.Key.Key_Semicolon: 0xBA,
    Qt.Key.Key_Apostrophe: 0xDE, Qt.Key.Key_Comma: 0xBC, Qt.Key.Key_Period: 0xBE, Qt.Key.Key_Slash: 0xBF,
})
# A modifier's own key press does not always carry its modifier flag yet.
_QT_MODIFIER_KEYS = {Qt.Key.Key_Control: MOD_CONTROL, Qt.Key.Key_Alt: MOD_ALT, Qt.Key.Key_AltGr: MOD_ALT,
                     Qt.Key.Key_Shift: MOD_SHIFT, Qt.Key.Key_Meta: MOD_WIN}


def _modifiers(event):
    flags = event.modifiers()
    value = 0
    for qt_flag, flag in ((Qt.KeyboardModifier.ControlModifier, MOD_CONTROL), (Qt.KeyboardModifier.AltModifier, MOD_ALT),
                          (Qt.KeyboardModifier.ShiftModifier, MOD_SHIFT), (Qt.KeyboardModifier.MetaModifier, MOD_WIN)):
        if flags & qt_flag:
            value |= flag
    return value


def _virtual_key(event):
    # The native code follows the physical key whatever the keyboard layout
    # or Shift state; the Qt key is only a fallback (e.g. synthetic events).
    return event.nativeVirtualKey() or _QT_KEYS.get(Qt.Key(event.key()), 0)


class HotkeyEdit(QWidget):
    """Click, then press a key or combination; Esc cancels, 清除 removes it."""

    changed = Signal(str)
    recording = Signal(bool)

    def __init__(self, default="", parent=None):
        super().__init__(parent)
        self._value, self._default, self._recording = "", default, False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.field = QLineEdit()
        self.field.setObjectName("hotkeyField")
        self.field.setReadOnly(True)
        self.field.setMinimumWidth(190)
        self.field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.field.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.field.setToolTip(tr('点击开始录入；也可聚焦后按 Enter 或空格开始。Esc 取消'))
        self.field.installEventFilter(self)
        row.addWidget(self.field, 1)
        self.clear_button = QPushButton(tr('清除'))
        self.clear_button.setToolTip(tr('不使用快捷键'))
        self.clear_button.clicked.connect(lambda: self._set_value(""))
        row.addWidget(self.clear_button)
        self.default_button = QPushButton(tr('默认'))
        self.default_button.setToolTip(tr('恢复默认：{v0}', v0=hotkey_label(default)))
        self.default_button.clicked.connect(lambda: self._set_value(self._default))
        self.default_button.setVisible(bool(default))
        row.addWidget(self.default_button)
        for button in (self.clear_button, self.default_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addLayout(row)
        self.message = QLabel("")
        self.message.setObjectName("hotkeyError")
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)
        self.recording_hint = QLabel(tr('正在录入，按 Esc 取消；其他快捷键暂时暂停'))
        self.recording_hint.setObjectName("settingDescription")
        self.recording_hint.setWordWrap(True)
        self.recording_hint.hide()
        layout.addWidget(self.recording_hint)
        self._show()

    def hotkey(self):
        return self._value

    def set_hotkey(self, text):
        self.stop_recording()
        self._value = text or ""
        self.message.clear()
        self.message.hide()
        self._show()

    @property
    def is_recording(self):
        return self._recording

    def _set_value(self, text):
        self.set_hotkey(text)
        self.changed.emit(self._value)

    def _show(self, partial=None):
        if self._recording:
            self.field.setText(partial or "")
            self.field.setPlaceholderText(tr('请按下快捷键…（Esc 取消）'))
        else:
            self.field.setText(hotkey_label(self._value) if self._value else tr('未启用'))
            self.field.setPlaceholderText(tr('未设置 · 点击录入'))

    def start_recording(self):
        if not self._recording:
            self._recording = True
            self.message.clear()
            self.message.hide()
            self.recording_hint.show()
            self._show()
            self.recording.emit(True)

    def stop_recording(self):
        self.message.clear()
        self.message.hide()
        self.recording_hint.hide()
        if self._recording:
            self._recording = False
            self._show()
            self.recording.emit(False)

    def record(self, modifiers, vk):
        """Use one key press while recording; returns the saved text or None."""
        if vk in MODIFIER_VKS:
            labels = [label for flag, label in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))
                      if modifiers & flag]
            self._show(" + ".join(labels + ["…"]))
            return None
        try:
            text = format_hotkey(modifiers, vk)
        except ValueError as exc:
            self.message.setText(str(exc))
            self.message.show()
            self._show()
            return None
        self._value = text
        self.message.hide()
        self.changed.emit(text)
        self.field.clearFocus()
        self.stop_recording()
        return text

    def eventFilter(self, watched, event):
        if watched is self.field:
            kind = event.type()
            if kind == QEvent.Type.FocusOut:
                self.stop_recording()
            elif (kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton
                  and not self._recording):
                self.field.setFocus(Qt.FocusReason.MouseFocusReason)
                self.start_recording()
                return True
            elif kind == QEvent.Type.ShortcutOverride and self._recording:
                event.accept()  # window shortcuts must not fire while a key is recorded
                return True
            elif kind == QEvent.Type.KeyPress and self._recording:
                if event.key() == Qt.Key.Key_Escape and not event.modifiers():
                    self.field.clearFocus()
                    self.stop_recording()
                elif Qt.Key(event.key()) in _QT_MODIFIER_KEYS:
                    self.record(_modifiers(event) | _QT_MODIFIER_KEYS[Qt.Key(event.key())], 0x11)
                else:
                    self.record(_modifiers(event), _virtual_key(event))
                return True
            elif (kind == QEvent.Type.KeyPress and not self._recording
                  and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
                  and not event.modifiers()):
                self.start_recording()
                return True
            elif kind == QEvent.Type.KeyRelease and self._recording:
                # Windows reports Print Screen only when the key is released.
                if _virtual_key(event) == 0x2C:
                    self.record(_modifiers(event), 0x2C)
                return True
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self.stop_recording()
        super().hideEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.DeferredDelete:
            self.stop_recording()
        return super().event(event)
