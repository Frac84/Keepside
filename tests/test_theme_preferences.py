"""Appearance drafts use temporary settings and never touch the clipboard."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Signal
from PySide6.QtWidgets import QApplication, QWidget

from snapvault.preferences import PreferencesPanel
from snapvault.storage import Vault
from snapvault.theme import THEMES, normalize_theme, preferences_style


NEW_THEME_NAMES = {
    "aurora": "暗夜极光",
    "nordic": "冬日北欧",
    "matcha": "春日抹茶",
    "windows_light": "Windows 浅色",
    "windows_dark": "Windows 深色",
}


class ThemeHost(QWidget):
    theme_changed = Signal(str)

    def __init__(self, vault):
        super().__init__()
        self.vault = vault
        self.theme_id = normalize_theme(vault.setting("theme", "light"))
        self.theme_calls = []
        self.listening = False
        self.collect_text = False
        self.clip_generation = 0
        self.jobs = []
        self.errors = []
        self.capture = SimpleNamespace(submit=lambda kind: self.jobs.append(kind))

    def set_theme(self, theme_id, persist=False):
        self.theme_id = normalize_theme(theme_id)
        self.theme_calls.append((self.theme_id, persist))
        if persist:
            self.vault.set_setting("theme", self.theme_id)
        self.theme_changed.emit(self.theme_id)

    def set_text_capture_enabled(self, enabled, persist=True):
        self.collect_text = enabled

    def update_capture_state(self):
        pass

    def apply_tabs_settings(self):
        pass

    def refresh(self):
        pass

    def report_error(self, message):
        self.errors.append(message)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app, tmp_path):
    vault = Vault(tmp_path / "appearance-vault")
    host = ThemeHost(vault)
    preferences = PreferencesPanel(host)
    yield preferences
    host.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_appearance_page_lists_the_named_themes_with_description_and_preview(panel):
    assert panel.pages.tabText(2) == "外观"
    assert panel.theme_selector.objectName() == "themeSelector"
    choices = [key for key in THEMES if key != "glass_night"]
    assert panel.theme_selector.count() == 13
    assert [panel.theme_selector.itemData(index) for index in range(13)] == choices
    assert [panel.theme_selector.itemText(index) for index in range(13)] == [
        "磨砂玻璃" if THEMES[key]["glass"] else THEMES[key]["name"] for key in choices
    ]
    assert all(not panel.theme_selector.itemIcon(index).isNull() for index in range(13))
    for theme_id, name in NEW_THEME_NAMES.items():
        index = panel.theme_selector.findData(theme_id)
        assert index >= 0, f"Missing selectable theme: {theme_id}"
        assert panel.theme_selector.itemText(index) == name
    assert panel.theme_selector.currentData() == "light"
    assert panel.theme_description.text() == THEMES["light"]["description"]
    assert panel.theme_preview.theme_id == "light"
    assert panel.host.theme_calls == []


@pytest.mark.parametrize("theme_id", list(THEMES))
def test_each_theme_previews_without_persisting_or_losing_other_drafts(panel, theme_id):
    panel.retention_group.button(30).setChecked(True)
    different = next(key for key in THEMES if key != theme_id)
    panel.select_theme(different)
    panel.select_theme(theme_id)
    assert panel.host.theme_id == theme_id
    assert panel.host.theme_calls[-1] == (theme_id, False)
    assert panel.host.vault.setting("theme", "light") == "light"
    assert panel.host.vault.setting("retention_days", 7) == 7
    assert panel.retention_group.checkedId() == 30
    assert panel.theme_description.text() == THEMES[theme_id]["description"]
    assert panel.theme_preview.theme_id == theme_id
    assert panel.styleSheet() == preferences_style(theme_id)


def test_save_persists_previewed_theme_and_existing_preferences(panel):
    theme_id = next(key for key in THEMES if key != "light")
    panel.theme_selector.setCurrentIndex(panel.theme_selector.findData(theme_id))
    panel.retention_group.button(14).setChecked(True)
    panel.text_capture_enabled.setChecked(True)
    assert panel.apply()
    assert panel.host.vault.setting("theme") == theme_id
    assert panel.host.theme_id == theme_id
    assert panel.host.vault.setting("retention_days") == 14
    assert panel.host.vault.setting("collect_text") is True
    assert panel.host.jobs == ["cleanup"]
    assert not panel.host.errors


def test_cancel_restores_saved_theme_and_discards_other_drafts(panel):
    saved_theme, preview_theme = list(THEMES)[-2:]
    panel.host.vault.set_setting("theme", saved_theme)
    panel.host.set_theme(saved_theme, persist=False)
    panel.select_theme(preview_theme)
    panel.retention_group.button(30).setChecked(True)
    panel.cancel()
    assert panel.host.theme_id == saved_theme
    assert panel.selected_theme_id() == saved_theme
    assert panel.theme_preview.theme_id == saved_theme
    assert panel.host.vault.setting("theme") == saved_theme
    assert panel.retention_group.checkedId() == 7
    assert panel.host.vault.setting("retention_days", 7) == 7


def test_external_theme_change_updates_only_appearance_draft_without_recursion(panel):
    theme_id = list(THEMES)[-1]
    panel.retention_group.button(90).setChecked(True)
    panel.host.set_theme(theme_id, persist=False)
    assert panel.host.theme_calls == [(theme_id, False)]
    assert panel.selected_theme_id() == theme_id
    assert panel.theme_preview.theme_id == theme_id
    assert panel.retention_group.checkedId() == 90
    assert panel.host.vault.setting("theme", "light") == "light"


def test_cancel_handles_an_invalid_stored_theme_using_the_default(panel):
    panel.host.vault.set_setting("theme", "unknown-theme")
    panel.theme_selector.setCurrentIndex(panel.theme_selector.count() - 1)
    panel.cancel()
    assert panel.host.theme_id == "light"
    assert panel.theme_selector.currentData() == "light"

