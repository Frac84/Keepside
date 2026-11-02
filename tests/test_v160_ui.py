"""1.6.0 window features: clipboard pins, resizable pins, links and QR codes,
similar screenshots, rescans and start with Windows. No real hotkey, registry
or system clipboard is touched."""
import ctypes
from ctypes import wintypes
import os
import sys
import threading

from PIL import Image
from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMessageBox
import pytest

from snapvault import autostart, hotkey, ui
from snapvault.pinboard import PinnedImageWindow
from snapvault.preferences import PreferencesPanel
from test_quick_privacy_ui import Mime, RecordingClipboard, app, flush, make_window  # noqa: F401  (fixtures)

ARTICLE = "今天下午三点在会议室讨论第四季度的预算安排，请各部门提前准备好材料并发送给财务同事确认"


class PictureClipboard(RecordingClipboard):
    def __init__(self):
        super().__init__()
        self.picture = QImage()

    def image(self):
        return self.picture


@pytest.fixture
def clipboard():
    return PictureClipboard()


class TaggingCapture(QObject):
    saved = Signal(str, bool)
    tagged = Signal(object, str, bool)
    failed = Signal(str)
    cleaned = Signal(int)
    hashed = Signal(int)

    def __init__(self, vault):
        super().__init__()
        self.jobs = []

    def start(self):
        pass

    def isRunning(self):
        return False

    def submit(self, kind, value=None, tag=None):
        self.jobs.append((kind, value, tag))
        return True


class QueueOcr(QObject):
    changed = Signal(str)
    availability = Signal(object, str)

    def __init__(self, vault):
        super().__init__()
        self.stopping = threading.Event()
        self.queued = []

    def start(self):
        pass

    def isRunning(self):
        return False

    def enqueue(self, key):
        self.queued.append(key)


@pytest.fixture
def window(make_window, monkeypatch):
    monkeypatch.setattr(ui, "CaptureWorker", TaggingCapture)
    monkeypatch.setattr(ui, "OcrWorker", QueueOcr)
    return make_window()


def picture(color="red", size=(160, 100)):
    return Image.new("RGB", size, color)


def test_clipboard_image_is_pinned_then_linked_to_its_saved_record(window, clipboard):
    clipboard.mime = Mime("")
    clipboard.picture = QImage(160, 100, QImage.Format.Format_RGB32)
    clipboard.picture.fill(0xFF2255AA)
    pin = window.pin_clipboard()
    assert pin in window.pinned_windows and pin.isVisible() and pin.item_id is None
    kind, image, tag = window.capture.jobs[-1]
    assert kind == "image" and image.size() == clipboard.picture.size() and tag is not None
    pin.copy_requested.emit(pin)
    assert clipboard.copied[-1] == ("image", (160, 100))
    key, _ = window.vault.add(picture())
    window._pin_saved(tag, key, True)
    assert pin.item_id == key and pin.image_path == window.vault.images / window.vault.get(key)["filename"]
    window.save_pin_state()
    assert [state["id"] for state in window.vault.setting("pins_state")] == [key]


def test_excluded_or_missing_clipboard_images(window, clipboard, monkeypatch):
    assert window.pin_clipboard() is None and "没有图片" in window.statusBar().currentMessage()
    clipboard.mime = Mime("")
    clipboard.picture = QImage(40, 40, QImage.Format.Format_RGB32)
    clipboard.picture.fill(0xFF000000)
    monkeypatch.setattr(ui, "should_exclude_clipboard", lambda mime: True)
    jobs = len(window.capture.jobs)
    pin = window.pin_clipboard()
    assert pin is not None and len(window.capture.jobs) == jobs and "不会存入图库" in window.statusBar().currentMessage()


def test_pin_hotkey_message_pins_the_clipboard(window, clipboard, app, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows message format")
    calls = []
    monkeypatch.setattr(window, "pin_clipboard", lambda: calls.append(True))
    window.pin_key.hwnd = 1
    monkeypatch.setattr(window.pin_key, "unregister", lambda: None)
    message = wintypes.MSG()
    message.message, message.wParam = hotkey.WM_HOTKEY, ui.PIN_HOTKEY_ID
    assert window.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message)) == (True, 0)
    flush(app)
    assert calls == [True]
    assert hotkey.hotkey_label("Alt+Shift+P") == "Alt + Shift + P"


def test_links_and_qr_codes_are_listed_opened_or_copied(window, clipboard, monkeypatch):
    key, _ = window.vault.add(picture())
    window.vault.finish_ocr(key, "官网 https://example.com/docs。扫码连接", codes=["WIFI:S:home;T:WPA;;", "https://qr.example.com/x"])
    window.refresh()
    window.active_id = key
    window.update_detail()
    texts = [window.codes_list.item(i).text() for i in range(window.codes_list.count())]
    assert texts == ["二维码 · WIFI:S:home;T:WPA;;", "二维码 · https://qr.example.com/x", "链接 · https://example.com/docs"]
    assert window.codes_list.isVisibleTo(window)
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    window._activate_code(window.codes_list.item(0))
    window._activate_code(window.codes_list.item(2))
    assert clipboard.copied[-1] == ("text", "WIFI:S:home;T:WPA;;") and opened == ["https://example.com/docs"]
    text, _ = window.vault.add_text("两个链接：https://a.example/1 和 https://b.example/2")
    window.active_id = text
    window.update_detail()
    assert window.codes_list.count() == 2
    link, _ = window.vault.add_text("https://only.example/page")
    window.active_id = link
    window.update_detail()
    assert not window.codes_list.isVisibleTo(window)  # the open-link button already covers it


def similar_pair(window):
    first, _ = window.vault.add(picture("red", (300, 500)))
    second, _ = window.vault.add(picture("blue", (300, 500)))
    other, _ = window.vault.add(picture("green", (300, 500)))
    window.vault.finish_ocr(first, ARTICLE)
    window.vault.finish_ocr(second, ARTICLE + "好的收到")
    window.vault.finish_ocr(other, "完全不同的内容，讲的是周末去爬山时看到的风景和路上的见闻")
    return first, second, other


def test_similar_scope_badges_and_keep_one(window, monkeypatch):
    first, second, other = similar_pair(window)
    window.update_similar(force=True)
    window.set_scope("similar")
    shown = [window.gallery.item(i).data(Qt.ItemDataRole.UserRole)["id"] for i in range(window.gallery.count())]
    assert set(shown) == {first, second} and window.similar_sizes == {first: 2, second: 2}
    assert window.nav_counts["similar"].text() == "2" and "1 组" in window.result_label.text()
    window.gallery.viewport().grab()  # badges paint without errors
    window.gallery.setCurrentRow(shown.index(first))
    assert window.similar_box.isVisibleTo(window) and "同组 2 张" in window.similar_label.text()
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.keep_only_active()
    assert window.vault.get(second) is None and window.vault.get(first) is not None
    assert window.vault.get(other) is not None and window.similar_sizes == {}
    assert window.gallery.count() == 0 and "没有发现相似截图" in window.empty.text()


def test_keep_one_never_deletes_favorites(window, monkeypatch):
    first, second, _ = similar_pair(window)
    window.vault.toggle_favorite(second)
    window.update_similar(force=True)
    window.active_id = first
    asked = []
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *args: asked.append(args) or QMessageBox.StandardButton.Yes)
    window.keep_only_active()
    assert asked == [] and window.vault.get(second) is not None
    assert "都已收藏" in window.statusBar().currentMessage()


def test_similar_groups_follow_library_changes_with_throttling(window, monkeypatch):
    similar_pair(window)
    window.update_similar(force=True)
    computed = []
    original = window.vault.similar_groups
    monkeypatch.setattr(window.vault, "similar_groups", lambda: computed.append(True) or original())
    window.update_similar()
    assert computed == []  # nothing changed
    window.vault.add_text("文字不参与相似截图")
    window.update_similar()
    assert computed == []
    window.vault.add(picture("black", (300, 500)))
    window._similar_time -= 10
    window.update_similar()
    assert computed == [True]


def test_rescan_queues_old_screenshots_from_settings(window, app):
    old, _ = window.vault.add(picture())
    window.vault.finish_ocr(old, "旧版识别的文字")
    panel = PreferencesPanel(window)
    assert "1 张" in panel.rescan_status.text() and panel.rescan_button.isEnabled()
    panel.rescan_button.click()
    assert window.ocr.queued == [old] and not panel.rescan_button.isEnabled()
    panel.deleteLater()
    flush(app)


def test_autostart_and_pin_hotkey_settings(window, app, monkeypatch):
    state = {"enabled": False, "writes": []}
    monkeypatch.setattr(autostart, "supported", lambda: True)
    monkeypatch.setattr(autostart, "enabled", lambda: state["enabled"])

    def set_enabled(value):
        state["writes"].append(value)
        state["enabled"] = value
        return value

    monkeypatch.setattr(autostart, "set_enabled", set_enabled)
    window.apply_quick_settings = lambda: None
    panel = PreferencesPanel(window)
    assert panel.autostart.isEnabled() and not panel.autostart.isChecked()
    panel.autostart.setChecked(True)
    panel.pin_hotkey.set_hotkey("Ctrl+Alt+P")
    assert panel.apply()
    assert state["writes"] == [True]
    assert window.vault.setting("pin_clipboard_hotkey") == "Ctrl+Alt+P" == window.pin_hotkey
    assert panel.apply() and state["writes"] == [True]  # unchanged: no second write
    panel.deleteLater()
    flush(app)


def test_autostart_entry_points_at_this_program(monkeypatch, tmp_path):
    exe = tmp_path / "拾影.exe"
    store = {}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(autostart, "_read", lambda: store.get("value"))
    monkeypatch.setattr(autostart, "_write", lambda value: store.update(value=value))
    if os.name != "nt":
        assert not autostart.supported()
        return
    assert autostart.command() == f'"{exe.resolve()}" --minimized'
    assert not autostart.enabled()
    assert autostart.set_enabled(True) and store["value"] == autostart.command()
    store["value"] = '"C:\\Old\\拾影.exe" --minimized'  # moved program
    assert not autostart.enabled()
    assert autostart.set_enabled(False) is False and store["value"] is None


@pytest.fixture
def pin(app, tmp_path):
    path = tmp_path / "pin.png"
    Image.new("RGB", (400, 200), "white").save(path)
    window = PinnedImageWindow(path)
    window.set_scale(1.0)
    window.move(300, 300)
    yield window
    window.close()


def test_dragging_edges_resizes_and_keeps_the_opposite_side(pin):
    start, scale = pin.geometry(), pin.scale_factor
    pin.resize_from_edges("r", start, scale, 100, 0)
    assert pin.width() > start.width() and pin.x() == start.x()
    assert abs(pin.width() / pin.height() - start.width() / start.height()) < 0.05
    pin.resize_from_edges("l", start, scale, -100, 0)
    assert pin.x() + pin.width() == start.x() + start.width()
    pin.resize_from_edges("lt", start, scale, 50, 50)
    assert pin.width() < start.width()
    assert pin.x() + pin.width() == start.x() + start.width() and pin.y() + pin.height() == start.y() + start.height()
    pin.resize_from_edges("b", start, scale, 0, -100000)
    assert pin.scale_factor == pytest.approx(PinnedImageWindow.MIN_SCALE)


def test_edges_cursor_and_in_memory_pins(pin, app):
    width, height = pin.width(), pin.height()
    assert pin._edges_at(QPoint(1, height // 2)) == "l"
    assert pin._edges_at(QPoint(width - 1, height - 1)) == "rb"
    assert pin._edges_at(QPoint(width // 2, height // 2)) == ""
    assert pin._edge_cursor("rb") == Qt.CursorShape.SizeFDiagCursor
    assert pin._edge_cursor("t") == Qt.CursorShape.SizeVerCursor
    image = QImage(60, 30, QImage.Format.Format_RGB32)
    image.fill(0xFF00FF00)
    memory = PinnedImageWindow(pixmap=image)
    assert memory.image_path is None and memory.source_image().size().toTuple() == (60, 30)
    assert "剪贴板贴图" in memory.windowTitle()
    memory.close()
    with pytest.raises(OSError):
        PinnedImageWindow(pixmap=QImage())
