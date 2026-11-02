"""Content-free screenshot preferences and repeat-region validation."""
from PySide6.QtGui import QColor

PREFERENCE_KEY = "screenshot_preferences"
REGION_KEY = "screenshot_last_region"
TOOLS = {"select", "rectangle", "ellipse", "arrow", "line", "pen", "highlight",
         "text", "number", "mosaic", "redact"}
DEFAULTS = dict(tool="select", color="#e81123", width=4, copy_save=True)


def normalize_preferences(value=None, base=None):
    result = dict(DEFAULTS)
    for values in (base, value):
        if not isinstance(values, dict):
            continue
        if values.get("tool") in TOOLS:
            result["tool"] = values["tool"]
        color = values.get("color")
        if isinstance(color, str) and QColor(color).isValid():
            result["color"] = QColor(color).name()
        width = values.get("width")
        if isinstance(width, (int, float)) and not isinstance(width, bool) and 1 <= width <= 24:
            result["width"] = round(width)
        if isinstance(values.get("copy_save"), bool):
            result["copy_save"] = values["copy_save"]
    return result


def normalize_region(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("geometry", "selection"):
        vector = value.get(key)
        if (not isinstance(vector, (list, tuple)) or len(vector) != 4 or
                any(not isinstance(v, int) or isinstance(v, bool) for v in vector)):
            return None
        if any(abs(v) > 100_000 for v in vector) or vector[2] < 2 or vector[3] < 2:
            return None
        result[key] = list(vector)
    _x, _y, width, height = result["geometry"]
    left, top, selected_width, selected_height = result["selection"]
    if left < 0 or top < 0 or left + selected_width > width or top + selected_height > height:
        return None
    return result
