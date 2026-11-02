"""Material window chrome must leave image pixels and editing behavior intact."""
from hashlib import sha256

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from snapvault import glass, inspector, review
from snapvault.inspector import ImageInspector
from snapvault.review import ReviewDialog
from snapvault.theme import theme
from test_themes import app, flush, harness


def selected_image(window):
    for index in range(window.gallery.count()):
        row = window.gallery.item(index).data(Qt.ItemDataRole.UserRole)
        if row["item_type"] == "image":
            window.gallery.setCurrentRow(index)
            window.active_id = row["id"]
            return window.vault.get(row["id"])
    raise AssertionError("Synthetic image was not created")


def test_inspector_glass_shell_preserves_source_pixels_and_zoom(harness, app, monkeypatch):
    requests = []
    original_refresh = inspector.refresh_window_glass

    def record_refresh(widget, dark=False, **kwargs):
        requests.append((dark, kwargs.get("enabled")))
        return original_refresh(widget, dark, **kwargs)

    monkeypatch.setattr(inspector, "refresh_window_glass", record_refresh)
    host = harness()
    row = selected_image(host)
    original = host.vault.images / row["filename"]
    before = sha256(original.read_bytes()).hexdigest()
    dialog = ImageInspector(host)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    flush(app)
    pixmap_before = dialog.view.item.pixmap().toImage()
    dialog.view.actual_size()
    scale_before = dialog.view.transform().m11()
    for theme_id in ("glass_night", "glass_day", "windows_dark", "windows_light", "light"):
        host.set_theme(theme_id, persist=False)
        dialog.refresh_theme()
        flush(app)
        assert requests[-1] == (theme()["dark"], theme_id.startswith("glass_"))
        assert dialog.view.item.pixmap().toImage() == pixmap_before
        assert dialog.view.backgroundBrush().color().alpha() == 255
        assert dialog.view.backgroundBrush().color().name() == theme()["viewer_canvas"]
        assert dialog.view.transform().m11() == scale_before
        assert dialog.windowOpacity() == 1.0
        assert dialog.view.graphicsEffect() is None
    assert sha256(original.read_bytes()).hexdigest() == before
    dialog.reject()
    flush(app)


def test_inspector_symbol_controls_have_descriptive_accessible_names(harness, app):
    host = harness()
    selected_image(host)
    dialog = ImageInspector(host)
    assert not dialog.header_mark.pixmap().isNull()
    for control in dialog.findChildren(QPushButton):
        if control.text() in ("×", "←", "→", "−", "+"):
            assert control.accessibleName() not in ("", control.text())
    dialog.reject()
    flush(app)


def test_review_material_keeps_pending_topics_and_raw_text(harness, app, monkeypatch):
    requests = []
    original_refresh = review.refresh_window_glass

    def record_refresh(widget, dark=False, **kwargs):
        requests.append((dark, kwargs.get("enabled")))
        return original_refresh(widget, dark, **kwargs)

    monkeypatch.setattr(review, "refresh_window_glass", record_refresh)
    host = harness()
    text = "Synthetic review text\nKeep this second line unchanged."
    key, _ = host.vault.add_text(text)
    host.vault.apply_classification(
        key, [], {}, host.vault.classification_input(key)["fingerprint"])
    dialog = ReviewDialog(host)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    flush(app)
    assert dialog.row["id"] == key
    dialog.toggle("tech")
    before_queue = list(dialog.queue)
    for theme_id in ("glass_night", "glass_day", "windows_dark", "windows_light"):
        host.set_theme(theme_id, persist=False)
        dialog.refresh_theme()
        flush(app)
        assert requests[-1] == (theme()["dark"], theme_id.startswith("glass_"))
        assert dialog.text.toPlainText() == text
        assert dialog.queue == before_queue and dialog._selected() == ["tech"]
        assert dialog.windowOpacity() == 1.0
        assert dialog.text.graphicsEffect() is None
    assert not host.vault.get(key)["tags_manual"]
    dialog.reject()
    flush(app)


@pytest.mark.parametrize("dialog_class", [ImageInspector, ReviewDialog])
def test_auxiliary_material_clears_when_leaving_glass_theme(harness, app, monkeypatch, dialog_class):
    # Model an available compositor so offscreen tests can catch stale native
    # material flags after selecting an ordinary palette.
    monkeypatch.setattr(glass, "apply_glass", lambda *_: dict(enabled=True, method="test-material"))
    host = harness()
    selected_image(host)
    host.set_theme("glass_day", persist=False)
    dialog = dialog_class(host)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    flush(app)
    assert dialog.property("nativeGlass") is True
    assert dialog._window_material_controller.requested is True
    host.set_theme("windows_light", persist=False)
    dialog.refresh_theme()
    flush(app)
    assert dialog.property("nativeGlass") is False
    assert dialog.glass_state["reason"] == "theme-disabled"
    assert dialog._window_material_controller.requested is False
    host.set_theme("glass_night", persist=False)
    dialog.refresh_theme()
    flush(app)
    assert dialog.property("nativeGlass") is True
    assert dialog._window_material_controller.dark is True
    assert dialog.windowOpacity() == 1.0
    dialog.reject()
    flush(app)
