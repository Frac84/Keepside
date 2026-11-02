"""Explicit UI translations, with content and storage identifiers left untouched."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

LANGUAGES = (("en_US", "English"), ("zh_CN", "简体中文"))
_language = "en_US"
_catalog = None
_missing = set()
_qt_translator = None


def normalize_language(value):
    return "zh_CN" if str(value).lower().replace("-", "_") in {"zh", "zh_cn", "zh_hans"} else "en_US"


def local_settings_path():
    explicit = os.environ.get("KEEPSIDE_LOCAL_SETTINGS")
    if explicit:
        return Path(explicit)
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    # Keep the original installation binding namespace across product renames.
    return local / "SnapVault" / "local-settings.json"


def local_settings():
    try:
        value = json.loads(local_settings_path().read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def preferred_language():
    return normalize_language(os.environ.get("KEEPSIDE_LANGUAGE") or local_settings().get("language", "en_US"))


def language():
    return _language


def set_language(value):
    global _language
    _language = normalize_language(value)
    os.environ["KEEPSIDE_LANGUAGE"] = _language
    return _language


def catalog():
    global _catalog
    if _catalog is None:
        _catalog = {}
        for path in sorted(Path(__file__).with_name("locales").glob("en_*.json")):
            values = json.loads(path.read_text(encoding="utf-8-sig"))
            for source, target in values.items():
                if source in _catalog and _catalog[source] != target:
                    raise ValueError("Conflicting UI translation: " + source)
                _catalog[source] = target
    return _catalog


def reload_catalog():
    global _catalog
    _catalog = None
    return catalog()


def tr(source, **kwargs):
    text = str(source)
    if _language == "en_US":
        translated = catalog().get(text)
        if translated is None:
            if any("\u4e00" <= char <= "\u9fff" for char in text):
                _missing.add(text)
            translated = text
        text = translated
    return text.format(**kwargs) if kwargs else text


def untranslated_keys():
    return sorted(_missing)


@contextmanager
def using_language(value):
    previous = language()
    try:
        set_language(value)
        yield
    finally:
        set_language(previous)


def configure_language(value, app=None):
    global _qt_translator
    code = set_language(value)
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    QLocale.setDefault(QLocale(code))
    if app is not None:
        if _qt_translator is not None:
            app.removeTranslator(_qt_translator)
            _qt_translator = None
        if code == "zh_CN":
            translator = QTranslator(app)
            if translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
                app.installTranslator(translator)
                _qt_translator = translator
    return code


set_language(preferred_language())
