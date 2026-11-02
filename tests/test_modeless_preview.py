"""The production preview entry must keep the library operable."""
from hashlib import sha256

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from snapvault import inspector
from test_auxiliary_glass import selected_image
from test_themes import app, flush, harness  # noqa: F401


def test_production_preview_does_not_enter_a_modal_loop_and_reuses_one_viewer(harness, app, monkeypatch):
    host = harness()
    row = selected_image(host)
    path = host.vault.images / row["filename"]
    before = sha256(path.read_bytes()).hexdigest()

    def no_modal_exec(_dialog):
        raise AssertionError("A preview must not block the library in exec()")

    monkeypatch.setattr(inspector.ImageInspector, "exec", no_modal_exec)
    viewer = host.preview()
    flush(app)
    assert viewer is host.inspector_window and viewer.isVisible()
    assert viewer.windowModality() == Qt.WindowModality.NonModal
    assert not viewer.isModal() and QApplication.activeModalWidget() is None
    assert host.isEnabled()
    assert host.preview() is viewer
    assert sha256(path.read_bytes()).hexdigest() == before

    viewer.showMinimized()
    flush(app)
    assert host.preview() is viewer
    flush(app)
    assert not viewer.isMinimized() and host.isEnabled()
    viewer.close()
    flush(app)
    assert host.inspector_window is None and not isValid(viewer)


def test_reused_viewer_tracks_selected_original_and_can_be_reopened(harness, app):
    host = harness()
    selected_image(host)
    first = host.preview()
    with Image.new("RGB", (72, 48), "#176534") as source:
        key, _created = host.vault.add(source)
    host.active_id = key
    second = host.preview()
    assert second is first and second._row["id"] == key
    second.close()
    flush(app)
    reopened = host.preview()
    assert reopened is not first and reopened._row["id"] == key
    reopened.close()
    flush(app)


def test_quitting_closes_modeless_preview_and_releases_its_image(harness, app):
    host = harness()
    selected_image(host)
    viewer = host.preview()
    flush(app)
    host.quit_app()
    flush(app)
    assert host.inspector_window is None
    assert not isValid(viewer)
