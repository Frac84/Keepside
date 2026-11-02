"""Shared product name and local icon resources for source and frozen builds."""
from pathlib import Path
import sys

from PySide6.QtGui import QIcon


APP_NAME = "Keepside"
APP_TAGLINE = "Capture, collect and keep things close"


def app_name():
    from .i18n import language, local_settings
    names = local_settings().get("display_names", {})
    value = names.get(language()) if isinstance(names, dict) else None
    if isinstance(value, str) and 0 < len(value.strip()) <= 80 and not any(ord(c) < 32 for c in value):
        return value.strip()
    return APP_NAME


def app_tagline():
    from .i18n import tr
    return tr("截图、内容收集与文件随手用")


def asset_path(filename: str) -> Path:
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return root / "assets" / filename


def app_icon() -> QIcon:
    for filename in ("shiying.ico", "shiying.png"):
        path = asset_path(filename)
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()
