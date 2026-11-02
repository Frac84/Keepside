"""Quick paste, privacy rules, pins, gallery refresh and OCR selection in the window.

Uses fake clipboard, workers and input services; no global hotkey is registered
and no keystroke is sent.
"""
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import os
import threading

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
import pytest

from snapvault import clipboard_policy as policy
from snapvault import hotkey, ui
from snapvault.inspector import ImageInspector, lines_in, selection_text
from snapvault.preferences import PreferencesPanel
from snapvault.quickpaste import QuickPastePanel
from snapvault.storage import Vault


class Mime:
    def __init__(self, text):
        self._text = text

    def hasText(self):
        return True

    def text(self):
        return self._text

    def hasImage(self):
        return False

    def hasFormat(self, name):
        return False

    def hasUrls(self):
        return False


class RecordingClipboard(QObject):
    dataChanged = Signal()

    def __init__(self):
        super().__init__()
        self.mime = None
        self.copied = []

    def mimeData(self):
        if self.mime is None:
            raise AssertionError("Unexpected clipboard read")
        return self.mime

    def image(self):
        return QImage()

    def setImage(self, image):
        self.copied.append(("image", image.size().toTuple()))

    def setText(self, text):
        self.copied.append(("text", text))


class FakeCapture(QObject):
    saved = Signal(str, bool)
    failed = Signal(str)
    cleaned = Signal(int)

    def __init__(self, vault):
        super().__init__()
        self.jobs = []

    def start(self):
        pass

    def isRunning(self):
        return False

    def submit(self, kind, value=None):
        self.jobs.append((kind, value))
        return True


class FakeOcr(QObject):
    changed = Signal(str)
    availability = Signal(object, str)

    def __init__(self, vault):
        super().__init__()
        self.stopping = threading.Event()

    def start(self):
        pass

    def isRunning(self):
        return False

    def enqueue(self, key):
        pass


def flush(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(ui.STYLE)
    return application


@pytest.fixture
def clipboard():
    return RecordingClipboard()


@pytest.fixture
def make_window(app, tmp_path, monkeypatch, clipboard):
    class IsolatedApplication:
        @staticmethod
        def clipboard():
            return clipboard

        @staticmethod
        def instance():
            return app

        @staticmethod
        def quit():
            pass

    monkeypatch.setattr(ui, "QApplication", IsolatedApplication)
    monkeypatch.setattr(ui.MainWindow, "sequence", staticmethod(lambda: 0))
    monkeypatch.setattr(ui, "CaptureWorker", FakeCapture)
    monkeypatch.setattr(ui, "OcrWorker", FakeOcr)
    monkeypatch.setattr(ui, "should_exclude_clipboard", lambda mime: False)
    monkeypatch.setattr(ui, "clipboard_source_app", lambda **kwargs: "")
    created = []

    def create(vault=None, **kwargs):
        vault = vault or Vault(tmp_path / f"vault{len(created)}")
        vault.set_setting("retention_days", 0)
        options = dict(listen=True, use_tray=False, browser_tools=False, classify=False)
        options.update(kwargs)
        window = ui.MainWindow(vault, **options)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        flush(app)
        created.append(window)
        return window

    yield create
    for window in created:
        window.quit_app()
        window.finish_quit()
        window.hide()
        window.deleteLater()
    flush(app)


def text_jobs(window):
    return [job for job in window.capture.jobs if job[0] == "text"]


def test_sensitive_text_is_skipped_only_for_automatic_collection(make_window, clipboard):
    window = make_window()
    window.collect_text = True
    clipboard.mime = Mime("token: ghp_" + "a" * 36)
    window.read_clipboard(window.clip_generation, 0, 0)
    assert text_jobs(window) == [] and "访问令牌" in window.statusBar().currentMessage()
    window.capture_current()
    assert text_jobs(window) == [("text", clipboard.mime.text())]
    window.skip_sensitive = False
    window.read_clipboard(window.clip_generation, 0, 0)
    assert len(text_jobs(window)) == 2


def test_excluded_source_program_is_skipped(make_window, clipboard, monkeypatch):
    window = make_window()
    window.collect_text = True
    clipboard.mime = Mime("从密码管理器复制的普通文字")
    calls = []
    monkeypatch.setattr(ui, "clipboard_source_app", lambda **kwargs: calls.append(kwargs) or "keepass.exe")
    window.read_clipboard(window.clip_generation, 0, 0)
    window.capture_current()
    assert text_jobs(window) == []
    assert calls == [{"foreground_fallback": True}, {"foreground_fallback": False}]
    assert "keepass.exe" in window.statusBar().currentMessage()
    window.excluded_apps = []
    window.capture_current()
    assert text_jobs(window) == [("text", clipboard.mime.text())]


def test_cards_paint_without_reading_settings(make_window, monkeypatch):
    window = make_window()
    for index in range(3):
        window.vault.add_text(f"卡片 {index}")
    window.retention_days = 7
    window.refresh()
    monkeypatch.setattr(window.vault, "setting", lambda *args: (_ for _ in ()).throw(AssertionError("DB read while painting")))
    window.gallery.viewport().grab()


def test_refresh_updates_changed_cards_in_place(make_window, monkeypatch):
    window = make_window()
    keys = [window.vault.add(Image.new("RGB", (30 + index, 20), "white"))[0] for index in range(3)]
    window.refresh()
    window.gallery.setCurrentRow(1)
    rebuilt = []
    original_clear = window.gallery.clear
    monkeypatch.setattr(window.gallery, "clear", lambda: rebuilt.append(True) or original_clear())
    window.vault.finish_ocr(keys[0], "识别完成的文字")
    window.refresh()
    assert rebuilt == []
    texts = {window.gallery.item(i).data(Qt.ItemDataRole.UserRole)["id"]: window.gallery.item(i).data(Qt.ItemDataRole.UserRole)["ocr_text"]
             for i in range(window.gallery.count())}
    assert texts[keys[0]] == "识别完成的文字" and window.gallery.currentRow() == 1
    window.vault.add_text("新的一条")
    window.refresh()
    assert rebuilt == [True] and window.gallery.count() == 4


def test_pins_copy_and_restore_where_they_were(make_window, clipboard, app):
    window = make_window()
    key, _ = window.vault.add(Image.new("RGB", (200, 120), "blue"))
    window.refresh()
    window.active_id = key
    pin = window.pin_image()
    pin.set_scale(0.5)
    pin.move(123, 145)
    pin.copy_requested.emit(pin)
    assert clipboard.copied == [("image", (200, 120))]
    pin.set_click_through(True)
    window.save_pin_state()
    state = window.vault.setting("pins_state")
    assert state == [{"id": key, "x": pin.x(), "y": pin.y(), "scale": 0.5, "opacity": 1.0}]
    window.close_all_pins()
    flush(app)
    window.restore_pins()
    restored = window.pinned_windows[0]
    assert restored.item_id == key and restored.scale_factor == 0.5 and not restored.click_through
    assert (restored.x(), restored.y()) == (state[0]["x"], state[0]["y"])


def test_quick_paste_searches_copies_and_pastes_into_the_previous_window(make_window, clipboard, monkeypatch):
    window = make_window()
    now = datetime.now(timezone.utc)
    window.vault.add_text("会议纪要\n下午讨论预算", now - timedelta(minutes=2))
    title, _ = window.vault.add_text("预算表\n第二行", now - timedelta(minutes=5))
    window.vault.add_text("别的内容", now)
    pasted = []
    monkeypatch.setattr(hotkey, "foreground_window", lambda: 4242)
    monkeypatch.setattr(hotkey, "focus_window", lambda hwnd: pasted.append(("focus", hwnd)) or True)
    monkeypatch.setattr(hotkey, "send_paste", lambda: pasted.append("paste"))
    panel = QuickPastePanel(window)
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    panel.open()
    assert panel.target == 4242 and panel.results.count() == 3
    panel.search.setText("预算")
    panel.refresh()
    assert panel.results.count() == 2 and panel.current_id() == title  # title hit ranks first
    panel.accept_current(paste=False)
    assert clipboard.copied == [("text", "预算表\n第二行")] and pasted == [] and not panel.isVisible()
    panel.open()
    panel.accept_current()
    assert pasted == [("focus", 4242)]
    window.quick_autopaste = False
    panel.open()
    panel.accept_current()
    assert pasted == [("focus", 4242)] and len(clipboard.copied) == 3
    panel.close()


def test_hotkey_message_opens_the_quick_panel(make_window, app, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows message format")
    window = make_window()
    monkeypatch.setattr(hotkey, "foreground_window", lambda: 0)
    window.quick_key.hwnd = 1  # pretend the shortcut is registered
    monkeypatch.setattr(window.quick_key, "unregister", lambda: None)
    message = wintypes.MSG()
    message.message, message.wParam = hotkey.WM_HOTKEY, ui.QUICK_HOTKEY_ID
    handled = window.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message))
    assert handled == (True, 0)
    flush(app)
    assert window.quick_panel is not None and window.quick_panel.isVisible()
    window.quick_panel.hide()
    message.wParam = 7
    assert window.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message)) == (False, 0)
    message.message, message.wParam = 0x0200, ui.QUICK_HOTKEY_ID  # WM_MOUSEMOVE is not a hotkey
    assert window.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message)) == (False, 0)


def test_hotkey_parsing_and_registration_errors(monkeypatch):
    assert hotkey.parse_hotkey("Alt+Shift+V") == (hotkey.MOD_ALT | hotkey.MOD_SHIFT, ord("V"))
    assert hotkey.parse_hotkey("Ctrl+`") == (hotkey.MOD_CONTROL, 0xC0)
    assert hotkey.parse_hotkey("ctrl+shift+space") == (hotkey.MOD_CONTROL | hotkey.MOD_SHIFT, 0x20)
    assert hotkey.parse_hotkey("Ctrl+F5") == (hotkey.MOD_CONTROL, 0x74)
    assert hotkey.parse_hotkey("") is None
    for bad in ("V", "Hyper+V", "Ctrl+Enterprise"):
        with pytest.raises(ValueError):
            hotkey.parse_hotkey(bad)
    calls = []

    class User:
        def __init__(self, ok):
            self.ok = ok

        def RegisterHotKey(self, hwnd, ident, modifiers, key):
            calls.append(("register", hwnd, ident, modifiers, key))
            return self.ok

        def UnregisterHotKey(self, hwnd, ident):
            calls.append(("unregister", hwnd, ident))
            return True

    shortcut = hotkey.GlobalHotkey(9)
    monkeypatch.setattr(hotkey.GlobalHotkey, "_user32", staticmethod(lambda: User(False)))
    assert not shortcut.register(77, "Alt+Shift+V") and "占用" in shortcut.error and not shortcut.active
    monkeypatch.setattr(hotkey.GlobalHotkey, "_user32", staticmethod(lambda: User(True)))
    assert shortcut.register(77, "Alt+Shift+V") and shortcut.active
    assert calls[-1] == ("register", 77, 9, hotkey.MOD_ALT | hotkey.MOD_SHIFT | hotkey.MOD_NOREPEAT, ord("V"))
    assert not shortcut.register(77, "")  # turning it off unregisters
    assert ("unregister", 77, 9) in calls and not shortcut.active


@pytest.mark.parametrize("text,reason", [
    ("普通的一段话", ""),
    ("订单号 4111111111111111", ""),
    ("快递单号 773012345678901", ""),
    ("卡号 4111111111111111", "银行卡号"),
    ("4111 1111 1111 1111", "银行卡号"),
    ("身份证 11010519491231002X", "身份证号"),
    ("11010519491231002Y", ""),
    ("key=sk-proj-abcdefghijklmnopqrstuvwx", "访问令牌"),
    ("AKIAABCDEFGHIJKLMNOP", "访问令牌"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "私钥"),
    ("账号 admin 密码：hunter22", "密码"),
    ("def password_hash(pwd): pass", ""),
])
def test_sensitive_text_detection_is_narrow(text, reason):
    assert policy.sensitive_text_reason(text) == reason


def test_app_list_normalization():
    names = policy.normalize_app_list('KeePass.exe\nbitwarden, "C:\\Apps\\Bank App.exe" ,，keepass.exe')
    assert names == ["keepass.exe", "bitwarden.exe", "bank app.exe"]
    assert policy.app_excluded("KeePass.EXE", names) and not policy.app_excluded("", names)


def test_selection_helpers_follow_reading_order():
    lines = [{"x": 100, "y": 10, "w": 60, "h": 12, "text": "右边"},
             {"x": 10, "y": 10, "w": 60, "h": 12, "text": "左边"},
             {"x": 10, "y": 40, "w": 60, "h": 12, "text": "下 一 行"}]
    # The same CJK spacing cleanup as the full OCR text.
    assert selection_text(lines_in(lines, QRectF(0, 0, 200, 30))) == "左边右边"
    assert [line["text"] for line in lines_in(lines, QRectF(15, 45, 1, 1))] == ["下 一 行"]
    assert selection_text(lines_in(lines, QRectF(0, 0, 200, 60))) == "左边右边\n下一行"
    assert lines_in(lines, QRectF(300, 300, 20, 20)) == []


def test_inspector_highlights_search_hits_and_copies_a_selection(make_window, clipboard, app):
    window = make_window()
    key, _ = window.vault.add(Image.new("RGB", (300, 120), "white"))
    window.vault.finish_ocr(key, "发票号码\n合计金额", lines=[
        {"x": 10, "y": 10, "w": 120, "h": 20, "text": "发票号码"},
        {"x": 10, "y": 60, "w": 120, "h": 20, "text": "合计金额"}])
    window.search.setText("金额")
    window.refresh()
    window.active_id = key
    dialog = ImageInspector(window)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    flush(app)
    visible = [item.data(0) for item in dialog.view._line_items if item.isVisible()]
    assert visible == ["合计金额"] and dialog.select_button.isEnabled()
    dialog.select_button.click()
    assert dialog.view.select_mode and all(item.isVisible() for item in dialog.view._line_items)
    dialog.view.selection_made.emit(QRectF(0, 0, 200, 40))
    assert clipboard.copied == [("text", "发票号码")]
    dialog.reject()
    flush(app)


def test_privacy_settings_round_trip(make_window, app):
    window = make_window()
    applied = []
    window.apply_quick_settings = lambda: applied.append(window.quick_hotkey)
    panel = PreferencesPanel(window)
    assert panel.pages.tabText(3) == "快捷与隐私"
    panel.quick_hotkey.set_hotkey("Ctrl+Alt+V")
    panel.quick_autopaste.setChecked(False)
    panel.skip_sensitive.setChecked(False)
    panel.restore_pins.setChecked(False)
    panel.excluded_apps.setPlainText("MyBank.exe\n\nkeepass")
    assert panel.apply()
    vault = window.vault
    assert vault.setting("quick_paste_hotkey") == "Ctrl+Alt+V" and applied == ["Ctrl+Alt+V"]
    assert vault.setting("excluded_apps") == ["mybank.exe", "keepass.exe"] == window.excluded_apps
    assert window.skip_sensitive is False and window.quick_autopaste is False and window.restore_pins_enabled is False
    panel._restore_default_apps()
    assert panel.excluded_apps.toPlainText().splitlines() == list(policy.DEFAULT_EXCLUDED_APPS)
    panel.deleteLater()
    flush(app)


def test_status_page_reports_agreement_and_training_threshold(make_window):
    window = make_window(classify=True)
    window.classifier.stopping.set()
    key, _ = window.vault.add_text("周会改到下午")
    snapshot = window.vault.classification_input(key)
    window.vault.apply_classification(key, ["chat"], {}, snapshot["fingerprint"])
    window.vault.set_tags(key, ["tasks"], source="review")
    window.show_page("status")
    text = window.topic_summary.text()
    assert "已核对 1 条" in text and "0 条与你的判断一致" in text and "手动标签 1 条" in text
    assert not window.train_button.isEnabled() and not window.personal_button.isVisible()
