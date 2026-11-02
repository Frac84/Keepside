"""The glass family has one entry and two reversible, persisted modes."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from snapvault.preferences import PreferencesPanel
from snapvault.theme import THEMES, inspector_style, review_style, style_for, theme
from test_theme_preferences import app, panel


def test_glass_is_one_family_with_day_and_night_choices(panel):
    assert sum(panel.theme_selector.itemText(i) == "磨砂玻璃"
               for i in range(panel.theme_selector.count())) == 1
    assert panel.theme_selector.findData("glass_night") == -1
    assert panel.glass_mode_row.isHidden()
    panel.select_theme("glass_day")
    assert not panel.glass_mode_row.isHidden()
    assert panel.selected_theme_id() == "glass_day"
    assert panel.glass_mode_buttons["glass_day"].isChecked()
    for button in panel.glass_mode_buttons.values():
        assert button.minimumHeight() >= 44
        assert button.accessibleName().startswith("磨砂玻璃")


def test_mode_toggle_previews_immediately_without_saving_or_changing_other_drafts(panel):
    panel.retention_group.button(30).setChecked(True)
    panel.select_theme("glass_day")
    panel.host.theme_calls.clear()
    QTest.keyClick(panel.glass_mode_buttons["glass_night"], Qt.Key.Key_Space)
    assert panel.host.theme_calls == [("glass_night", False)]
    assert panel.selected_theme_id() == panel.host.theme_id == "glass_night"
    assert panel.glass_mode_buttons["glass_night"].isChecked()
    assert not panel.glass_mode_buttons["glass_day"].isChecked()
    assert panel.retention_group.checkedId() == 30
    assert panel.host.vault.setting("theme", "light") == "light"
    assert panel.host.vault.setting("retention_days", 7) == 7


def test_leaving_and_returning_to_glass_keeps_the_mode_in_this_draft(panel):
    panel.select_theme("glass_night")
    panel.select_theme("paper")
    assert panel.glass_mode_row.isHidden()
    panel.theme_selector.setCurrentIndex(panel.theme_selector.findData("glass_day"))
    assert panel.host.theme_id == panel.selected_theme_id() == "glass_night"
    assert not panel.glass_mode_row.isHidden()


def test_saving_night_mode_reopens_the_single_family_with_night_selected(panel):
    panel.select_theme("glass_night")
    assert panel.apply()
    assert panel.host.vault.setting("theme") == "glass_night"
    reopened = PreferencesPanel(panel.host)
    assert reopened.theme_selector.currentText() == "磨砂玻璃"
    assert reopened.selected_theme_id() == "glass_night"
    assert reopened.glass_mode_buttons["glass_night"].isChecked()
    reopened.deleteLater()


def test_cancel_reverts_a_day_preview_to_saved_night_mode(panel):
    panel.host.vault.set_setting("theme", "glass_night")
    panel.host.set_theme("glass_night", persist=False)
    panel.glass_mode_buttons["glass_day"].click()
    assert panel.host.theme_id == "glass_day"
    panel.cancel()
    assert panel.host.theme_id == panel.selected_theme_id() == "glass_night"
    assert panel.glass_mode_buttons["glass_night"].isChecked()
    assert panel.host.vault.setting("theme") == "glass_night"


@pytest.mark.parametrize("theme_id", THEMES)
def test_native_material_layers_belong_only_to_the_glass_family(theme_id):
    glass = theme_id in ("glass_day", "glass_night")
    assert theme(theme_id)["glass"] is glass
    for stylesheet in (style_for(theme_id), inspector_style(theme_id), review_style(theme_id)):
        assert ('nativeGlass="true"' in stylesheet) is glass
    assert theme(theme_id)["card"].startswith("#")
    assert theme(theme_id)["viewer_canvas"] == "#181a20"


def test_glass_modes_share_identity_but_have_clear_day_night_contrast():
    assert theme("glass_day")["dark"] is False
    assert theme("glass_night")["dark"] is True
    assert theme("glass_day")["accent"] == theme("windows_light")["accent"]
    assert theme("glass_night")["accent"] == theme("windows_dark")["accent"]
    assert theme("glass_day")["material_canvas"].endswith(", 80)")
    assert theme("glass_night")["material_canvas"].endswith(", 104)")
