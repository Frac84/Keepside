"""Settings text and editors stay separate at narrow and enlarged sizes."""
import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QBoxLayout, QLabel

from snapvault.preferences import PreferencesPanel
from test_themes import app, flush, harness


def editors(panel):
    return (panel.full_hotkey, panel.shot_hotkey, panel.pin_hotkey, panel.quick_hotkey)


def column_labels(card):
    column = card.header.itemAt(0).layout()
    return [column.itemAt(index).widget() for index in range(column.count())
            if isinstance(column.itemAt(index).widget(), QLabel)]


def within(widget, ancestor):
    return QRect(widget.mapTo(ancestor, QPoint()), widget.size())


@pytest.mark.parametrize("size", [(1080, 700), (1440, 900)])
@pytest.mark.parametrize("theme_id", ["glass_day", "glass_night"])
def test_embedded_shortcut_cards_keep_text_out_of_editor(harness, app, size, theme_id):
    host = harness(saved_theme=theme_id)
    host.resize(*size)
    host.show_page("preferences")
    panel = host.preferences_panel
    panel.pages.setCurrentIndex(3)
    flush(app)
    for editor in editors(panel):
        card = editor.parentWidget()
        box = within(editor, card)
        assert card.rect().contains(box)
        for label in column_labels(card):
            bounds = within(label, card)
            assert card.rect().contains(bounds)
            assert not bounds.intersects(box)
            if label.wordWrap():
                assert label.hasHeightForWidth()
                assert label.height() >= label.heightForWidth(label.width())
    scroll = panel.pages.currentWidget()
    assert scroll.horizontalScrollBar().maximum() == 0


def test_small_card_stacks_editor_and_can_return_to_two_columns(harness, app):
    host = harness()
    panel = PreferencesPanel(host)
    editor = panel.full_hotkey
    card = editor.parentWidget()
    card.setParent(None)
    card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    card.show()
    try:
        card.resize(590, 200)
        flush(app)
        assert card.header.direction() == QBoxLayout.Direction.TopToBottom
        assert card.property("stackedEditor") is True
        text_bounds = column_labels(card)[-1]
        assert within(editor, card).top() > within(text_bounds, card).bottom()
        card.resize(1100, 200)
        flush(app)
        assert card.header.direction() == QBoxLayout.Direction.LeftToRight
        assert card.property("stackedEditor") is False
        assert within(editor, card).left() > within(text_bounds, card).right()
    finally:
        card.hide()
        card.deleteLater()
        panel.deleteLater()
        flush(app)


def test_larger_setting_text_stays_inside_its_card(harness, app):
    host = harness()
    host.resize(1080, 700)
    host.show_page("preferences")
    panel = host.preferences_panel
    panel.pages.setCurrentIndex(3)
    panel.setStyleSheet(panel.styleSheet() + "\nQWidget#settingsPage QLabel#settingDescription { font-size: 20px; }"
                       "\nQWidget#settingsPage QLabel#settingTitle { font-size: 22px; }")
    panel.full_hotkey.registration_status.setText("这是一条很长的快捷键状态说明，用于确认放大文字时仍可完整换行且不覆盖相邻说明。")
    flush(app)
    for editor in editors(panel):
        card = editor.parentWidget()
        box = within(editor, card)
        assert card.rect().contains(box)
        for label in column_labels(card):
            assert not within(label, card).intersects(box)
            if label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width())


def test_fullscreen_explanation_is_short_and_printscreen_note_remains_available(harness):
    host = harness()
    panel = PreferencesPanel(host)
    text = column_labels(panel.full_hotkey.parentWidget())[-1]
    assert len(text.text()) < 40 and "Windows 11" not in text.text()
    assert "PrtSc" in panel.full_hotkey.field.toolTip()
    assert "中转篮" in column_labels(panel.quick_hotkey.parentWidget())[-1].text()
    panel.deleteLater()
