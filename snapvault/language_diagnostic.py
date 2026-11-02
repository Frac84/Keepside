"""Render both UI languages against an isolated synthetic library."""
import json
import os
from pathlib import Path
import traceback
import uuid

from PySide6.QtCore import QEvent, QCoreApplication
from PySide6.QtGui import QFont, QFontDatabase

from . import ui
from .basket_diagnostic import _replacements
from .i18n import configure_language, language, untranslated_keys, reload_catalog
from .shelf_diagnostic import _Capture, _Ocr, _Clipboard
from .storage import Vault


def run_language_diagnostic(app, destination):
    root=Path(destination).resolve();root.mkdir(parents=True,exist_ok=True)
    fixture=root/('language-run-'+uuid.uuid4().hex[:8])
    fixture.mkdir()
    report=dict(ok=False,synthetic_only=True,system_clipboard_accessed=False,real_vault_accessed=False,checks=[])
    previous=language();previous_settings=os.environ.get('KEEPSIDE_LOCAL_SETTINGS')
    os.environ['KEEPSIDE_LOCAL_SETTINGS']=str(fixture/'no-local-settings.json')
    font_ids=[];window=None;clipboard=_Clipboard()
    if app.platformName()=='offscreen':
        for name in ('segoeui.ttf','msyh.ttc'):
            path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
            if path.is_file():font_ids.append(QFontDatabase.addApplicationFont(str(path)))
        app.setFont(QFont('Microsoft YaHei UI',9))
    class Application:
        clipboard=staticmethod(lambda:clipboard)
        instance=staticmethod(lambda:app)
        quit=staticmethod(lambda:None)
    replacements=[(ui,'QApplication',Application),(ui,'CaptureWorker',_Capture),(ui,'OcrWorker',_Ocr),
        (ui.MainWindow,'sequence',staticmethod(lambda:0))]
    try:
        reload_catalog()
        vault=Vault(fixture/'data')
        vault.set_setting('retention_days',0)
        vault.set_setting('shelf_ball_enabled',False)
        key,_=vault.add_text('用户原文不会被语言切换改写')
        before=vault.get(key)
        with _replacements(replacements):
            for code in ('en_US','zh_CN'):
                configure_language(code,app)
                window=ui.MainWindow(vault,listen=False,use_tray=False,browser_tools=False,classify=False,hotkeys=False)
                window.resize(1440,900);window.show();app.processEvents()
                assert window.grab().save(str(root/(code+'-library.png')))
                window.show_page('preferences');window.preferences_panel.pages.setCurrentIndex(2);app.processEvents()
                assert window.grab().save(str(root/(code+'-settings.png')))
                assert window.preferences_panel.ui_language_choice.findData(code)>=0
                assert vault.get(key)==before
                report['checks'].append(code+': library, settings and language selector render without altering user content')
                window.quit_app();window.finish_quit();window.hide();window.deleteLater()
                QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents();window=None
        missing=[key for key in untranslated_keys() if 'font-family' not in key and '用户原文' not in key]
        report['untranslated_keys']=missing
        assert not missing, 'Untranslated application labels: '+repr(missing)
        report['ok']=True
    except Exception:
        report['error']=traceback.format_exc()
    finally:
        if window is not None:
            window.quit_app();window.finish_quit();window.hide()
        configure_language(previous,app)
        if previous_settings is None:os.environ.pop('KEEPSIDE_LOCAL_SETTINGS',None)
        else:os.environ['KEEPSIDE_LOCAL_SETTINGS']=previous_settings
        for ident in font_ids:
            if ident>=0:QFontDatabase.removeApplicationFont(ident)
        (root/'language-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['ok'] else 1
