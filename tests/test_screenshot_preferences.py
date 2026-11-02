"""Screenshot habits and an explicit no-library output boundary."""
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QPixmap

from snapvault import ui
from snapvault.preferences import PreferencesPanel
from snapvault.screenshot_preferences import (DEFAULTS, PREFERENCE_KEY, REGION_KEY,
                                             normalize_preferences, normalize_region)
from test_quick_privacy_ui import app, flush, make_window  # noqa: F401
from test_v160_ui import window, clipboard  # noqa: F401
from test_screenshot_outputs import picture


def test_preferences_whitelist_carries_no_content():
    value = normalize_preferences(dict(tool="arrow", color="#0078D4", width=7,
        copy_save=False, text="must never persist", image="private", region="other"))
    assert value == dict(tool="arrow", color="#0078d4", width=7, copy_save=False)
    assert normalize_preferences(dict(tool="invalid", color="invalid", width=True, copy_save=0)) == DEFAULTS
    assert normalize_preferences({"width": 2}, value)["tool"] == "arrow"


def test_only_valid_local_region_is_remembered():
    value = dict(geometry=[-1920, 0, 1920, 1080], selection=[12, 22, 300, 200])
    assert normalize_region(value) == value
    for malformed in ({}, dict(value, selection=[-1, 0, 20, 20]),
                      dict(value, selection=[1900, 0, 300, 100]),
                      dict(value, geometry=[0, 0, True, 1080])):
        assert normalize_region(malformed) is None


def test_copy_only_is_remembered_and_fullscreen_respects_it(window, clipboard, app):
    window.capture.jobs.clear()
    window._screenshot_taken(picture(), "copy_only")
    assert clipboard.copied[-1] == ("image", (160, 100)) and window.capture.jobs == []
    assert window.vault.setting(PREFERENCE_KEY)["copy_save"] is False
    window.grab_fullscreen = lambda: QPixmap.fromImage(picture())
    window.take_fullscreen()
    assert window.capture.jobs == [] and "未存入图库" in window.statusBar().currentMessage()
    window.toast.close()
    window._screenshot_taken(picture(), "copy")
    assert window.capture.jobs[-1][0] == "image"
    assert window.vault.setting(PREFERENCE_KEY)["copy_save"] is True
    flush(app)


def test_temporary_pin_and_already_saved_png_do_not_enter_library(window):
    window._update_screenshot_preferences({"copy_save": False})
    window.capture.jobs.clear()
    window._screenshot_taken(picture(), "pin")
    pin = window.pinned_windows[-1]
    assert pin.item_id is None and not window.capture.jobs
    window.save_pin_state()
    assert window.vault.setting("pins_state") == []
    window._screenshot_taken(picture(), "save_done")
    assert not window.capture.jobs and "未存入" in window.statusBar().currentMessage()


def test_habits_and_repeat_region_survive_new_host(window, make_window):
    window._update_screenshot_preferences(dict(tool="arrow", color="#0078d4", width=8, copy_save=False))
    region = dict(geometry=[0, 0, 400, 300], selection=[10, 20, 100, 80])
    window._remember_screenshot_region(region)
    assert window.repeat_screenshot_button.isEnabled()
    restored = make_window(window.vault)
    assert restored.screenshot_preferences == window.screenshot_preferences
    assert restored.last_screenshot_region == region
    assert restored.vault.setting(REGION_KEY) == region


def test_settings_switch_preserves_other_habits(window, app):
    window._update_screenshot_preferences(dict(tool="arrow", color="#0078d4", width=8))
    panel = PreferencesPanel(window)
    panel.screenshot_keep.setChecked(False)
    assert panel.apply()
    assert window.screenshot_preferences == dict(tool="arrow", color="#0078d4", width=8, copy_save=False)
    panel.close()
    flush(app)


def test_transient_ocr_is_local_and_never_submits_a_library_job(window, clipboard, app):
    class TransientOcr(QObject):
        transient = Signal(object, object, str)
        def __init__(self):
            super().__init__()
            self.jobs = []
        def enqueue_image(self, image, tag):
            self.jobs.append((image.copy(), tag))
            return True
        def enqueue(self, _key):
            raise AssertionError("Transient screenshot used a library item")
    fake = TransientOcr()
    window.ocr = fake
    fake.transient.connect(window._screenshot_transient_result)
    window._update_screenshot_preferences({"copy_save": False})
    window.capture.jobs.clear()
    dialog = window.recognize_screenshot(picture())
    assert dialog.item_id is None and not window.capture.jobs
    assert len(fake.jobs) == 1 and not window.vault.query(limit=10)
    fake.transient.emit(fake.jobs[-1][1], {"text": "临时识别结果"}, "")
    assert dialog.text.toPlainText() == "临时识别结果" and "未入库" in dialog.status.text()
    assert dialog.retry_button.isEnabled()
    dialog.copy_button.click()
    assert clipboard.copied[-1] == ("text", "临时识别结果")
    dialog.retry_button.click()
    assert len(fake.jobs) == 2 and not window.capture.jobs
    dialog.close()
    flush(app)
    fake.transient.emit(fake.jobs[-1][1], {"text": "late"}, "")
    assert not window._transient_screenshot_text
    # Restore the fixture's normal worker before host teardown.
    from test_v160_ui import QueueOcr
    window.ocr = QueueOcr(window.vault)
