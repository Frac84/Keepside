"""Language switches affect UI only; local naming is separate from public files."""
import json
from pathlib import Path
from string import Formatter
import re

from snapvault import i18n, branding, ui
from snapvault.preferences import PreferencesPanel
from test_quick_privacy_ui import app, clipboard, flush, make_window  # noqa: F401


def test_catalog_templates_are_complete_and_english():
    for source, target in i18n.reload_catalog().items():
        assert not re.search('[\u4e00-\u9fff]', target), source
        fields=lambda s: sorted(f for _,f,_,_ in Formatter().parse(s) if f is not None)
        assert fields(source) == fields(target), source


def test_translation_round_trip_does_not_translate_content():
    with i18n.using_language('en_US'):
        assert i18n.tr('仅复制') == 'Copy only'
        assert i18n.tr('未命名文件') == 'Unnamed file'
        assert i18n.tr('用户文件.png') == '用户文件.png'
    with i18n.using_language('zh_CN'):
        assert i18n.tr('仅复制') == '仅复制'


def test_private_name_override_is_local_and_language_specific(tmp_path, monkeypatch):
    path=tmp_path/'local-settings.json'
    path.write_text(json.dumps({'display_names':{'zh_CN':'Local test name'}}),encoding='utf-8')
    monkeypatch.setenv('KEEPSIDE_LOCAL_SETTINGS',str(path))
    with i18n.using_language('zh_CN'): assert branding.app_name() == 'Local test name'
    with i18n.using_language('en_US'): assert branding.app_name() == 'Keepside'


def test_english_main_and_preferences_have_no_untranslated_labels(make_window, app):
    from PySide6.QtWidgets import QWidget
    with i18n.using_language('en_US'):
        window=make_window()
        window.show_page('preferences')
        panel=window.preferences_panel
        assert panel.ui_language_choice.findData('en_US') >= 0
        assert panel.ui_language_choice.findData('zh_CN') >= 0
        values=[]
        for child in window.findChildren(QWidget):
            for attr in ('text','toolTip','placeholderText','accessibleName'):
                method=getattr(child,attr,None)
                if callable(method):
                    value=method()
                    if isinstance(value,str) and re.search('[\u4e00-\u9fff]',value): values.append(value)
        assert values == []


def test_language_save_queues_restart_without_changing_records(make_window, monkeypatch, app):
    window=make_window()
    before=window.vault.query(limit=100)
    calls=[]
    with monkeypatch.context() as patch:
        patch.setattr(window,'quit_app',lambda:calls.append('quit'))
        window.set_language('en_US')
        assert window.vault.setting('ui_language') == 'en_US'
        assert not calls
        flush(app)
        assert calls == ['quit'] and window.language_restart_requested
        assert window.vault.query(limit=100) == before


def test_language_change_with_an_open_editor_preserves_work(make_window, monkeypatch, app):
    window=make_window()
    calls=[]
    with monkeypatch.context() as patch:
        patch.setattr(window,'quit_app',lambda:calls.append('quit'))
        window.image_tool_dialogs.append(object())
        window.set_language('en_US')
        flush(app)
        assert not calls and window.vault.setting('ui_language') == 'en_US'
        window.image_tool_dialogs.clear()
