"""Render material layers with synthetic widgets; no compositor/user data used."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QMainWindow,
                               QStackedWidget, QStyle, QStyleOption, QWidget)

from snapvault.theme import (THEMES, _contrast, apply_theme, inspector_style,
                            preferences_style, review_style, theme)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    return application


def surface(widget):
    """Exercise QSS matching and painting without a native top-level HWND."""
    widget.resize(100, 80)
    widget.ensurePolished()
    image = QImage(100, 80, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    option = QStyleOption()
    option.initFrom(widget)
    widget.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, widget)
    painter.end()
    return image.pixelColor(50, 40)


def main_widgets(app, native):
    apply_theme(app, "glass_day")
    window = QMainWindow()
    window.setProperty("appGlass", True)
    window.setProperty("nativeGlass", native)
    root = QWidget(window)
    root.setObjectName("appRoot")
    root.setProperty("nativeGlass", native)
    window.setCentralWidget(root)
    card = QFrame(root)
    card.setObjectName("details")
    return window, root, card


def test_material_is_enabled_by_confirmed_native_state_only(app):
    window, root, card = main_widgets(app, False)
    assert surface(root).alpha() == 255
    assert surface(card).alpha() == 255
    root.setProperty("nativeGlass", True)
    window.setProperty("nativeGlass", True)
    for widget in (window, root, card):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
    assert 0 < surface(root).alpha() < surface(card).alpha() < 255
    window.deleteLater()


@pytest.mark.parametrize("modal", [False, True])
def test_preferences_local_stylesheet_keeps_glass_layers(app, modal):
    if modal:
        apply_theme(app, "glass_day")
        window = QDialog()
        window.setProperty("appGlass", True)
        window.setProperty("nativeGlass", True)
        parent = window
    else:
        window, parent, _ = main_widgets(app, True)
    page = QWidget(parent)
    page.setObjectName("settingsPage")
    page.setStyleSheet(preferences_style("glass_day"))
    card = QFrame(page)
    card.setObjectName("settingCard")
    footer = QFrame(page)
    footer.setObjectName("settingsFooter")
    assert surface(page).alpha() == 0
    assert 150 < surface(card).alpha() < 255
    assert surface(footer).alpha() == surface(card).alpha()
    window.deleteLater()


def test_unavailable_compositor_keeps_preferences_opaque(app):
    window, root, _ = main_widgets(app, False)
    page = QWidget(root)
    page.setObjectName("settingsPage")
    page.setStyleSheet(preferences_style("glass_day"))
    card = QFrame(page)
    card.setObjectName("settingCard")
    assert surface(page).alpha() == 255
    assert surface(card).alpha() == 255
    window.deleteLater()


@pytest.mark.parametrize("theme_id", ["glass_day", "glass_night"])
def test_inspector_glass_never_transforms_its_image_canvas(app, theme_id):
    apply_theme(app, theme_id)
    dialog = QDialog()
    dialog.setObjectName("inspector")
    dialog.setProperty("nativeGlass", True)
    dialog.setStyleSheet(inspector_style(theme_id))
    header = QWidget(dialog)
    header.setObjectName("inspectorHeader")
    canvas = QWidget(dialog)
    canvas.setObjectName("inspectorCanvas")
    assert surface(dialog).alpha() == 0
    assert 0 < surface(header).alpha() < 255
    assert surface(canvas).alpha() == 255
    assert surface(canvas).name() == theme(theme_id)["viewer_canvas"]
    dialog.deleteLater()


def test_review_source_page_has_an_opaque_independent_plane(app):
    apply_theme(app, "glass_day")
    dialog = QDialog()
    dialog.setObjectName("reviewDialog")
    dialog.setProperty("nativeGlass", True)
    dialog.setStyleSheet(review_style("glass_day"))
    frame = QFrame(dialog)
    frame.setObjectName("reviewPreview")
    pages = QStackedWidget(frame)
    pages.setObjectName("reviewPages")
    assert 0 < surface(frame).alpha() < 255
    assert surface(pages).alpha() == 255
    assert surface(pages).name() == theme("glass_day")["preview"]
    dialog.deleteLater()


@pytest.mark.parametrize("theme_id", THEMES)
def test_all_palettes_keep_opaque_image_and_card_colors_and_readable_labels(theme_id):
    colors = theme(theme_id)
    for token in ("card", "canvas", "image_preview", "viewer_canvas"):
        assert QColor(colors[token]).alpha() == 255
    assert _contrast(colors["secondary"], colors["card"]) >= 4.5
    assert _contrast(colors["muted"], colors["card"]) >= 4.5
