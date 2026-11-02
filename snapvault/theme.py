"""Local theme palettes shared by Qt widgets and custom-painted surfaces."""

from functools import lru_cache
from pathlib import Path
import re
import sys


DEFAULT_THEME = "light"
_SPECS = {
    "light": dict(name="浅色", description="原有浅灰与白色卡片，靛蓝强调。", dark=False,
                  canvas="#f5f6f8", surface="#f0f1f5", card="#ffffff", border="#e5e7eb",
                  text="#202332", secondary="#747987", accent="#5b5bd6", success="#10b981", warning="#aa7b25"),
    "terminal": dict(name="赛博终端", description="深碳曜黑与磷光琥珀，薄荷绿状态指示。", dark=True,
                     canvas="#0b0c10", surface="#121318", card="#1a1b23", border="#2a2d3a",
                     text="#f5f6f8", secondary="#7a7f92", accent="#f59e0b", success="#10b981", warning="#f59e0b"),
    "charcoal": dict(name="烟熏炭灰", description="微暖炭灰、香槟米白与柔和赤陶铜色。", dark=True,
                     canvas="#1c1b1a", surface="#252422", card="#2f2d2a", border="#3e3c39",
                     text="#ede8e1", secondary="#918b82", accent="#e07a5f", success="#9bb6a0", warning="#d4a373"),
    "paper": dict(name="和纸清寂", description="未漂白和纸米灰与朱红印泥，温润克制。", dark=False,
                  canvas="#efece6", surface="#e5e0d8", card="#f8f6f2", border="#d6d0c5",
                  text="#2c2a29", secondary="#7d7771", accent="#c84b31", success="#3d5a58", warning="#98732d"),
    "blueberry": dict(name="午夜蓝莓", description="夜幕紫黑、霓虹薰衣草与橙色提醒。", dark=True,
                      canvas="#0f0e17", surface="#1a1829", card="#232038", border="#37304f",
                      text="#fffffe", secondary="#a7a9be", accent="#a78bfa", success="#78c4aa", warning="#ff8906"),
    "blueprint": dict(name="航天蓝图", description="Solarized 深海青灰、工控蓝与就绪青。", dark=True,
                      canvas="#002b36", surface="#073642", card="#094352", border="#22515d",
                      text="#fdf6e3", secondary="#839496", accent="#268bd2", success="#2aa198", warning="#b58900"),
    "morandi": dict(name="雾气莫兰迪", description="薄雾象牙白与桉树灰绿，柔和中性。", dark=False,
                    canvas="#f4f3f0", surface="#eae8e3", card="#ffffff", border="#ddd9d2",
                    text="#34312d", secondary="#7e7a73", accent="#52796f", success="#52796f", warning="#9b7a2c"),
    "aurora": dict(name="暗夜极光", description="深邃蓝黑与极光绿，青蓝链接点缀。", dark=True,
                   canvas="#121824", surface="#182135", card="#1e2640", border="#303b56",
                   text="#e2e8f0", secondary="#9baac1", accent="#00e676", support="#00b0ff",
                   link="#00b0ff", success="#00e676", warning="#e9b65c"),
    "nordic": dict(name="冬日北欧", description="浅霜灰与迷雾蓝，冰川蓝过渡、暖橘主按钮。", dark=False,
                   canvas="#f0f4f8", surface="#e6eef4", card="#ffffff", border="#d8e2ec",
                   text="#293241", secondary="#60748a", accent="#3d5a80", support="#98c1d9",
                   action="#ee6c4d", soft="#98c1d9", highlight="#f8d6cf", success="#4c806c", warning="#bb583f"),
    "matcha": dict(name="春日抹茶", description="燕麦白与抹茶绿，浅奶绿层次、奶油黄高亮。", dark=False,
                   canvas="#faf8f5", surface="#edf1e7", card="#fffdf9", border="#dce4d9",
                   text="#2c3531", secondary="#6b7c70", accent="#4a7c59", support="#a3b899",
                   soft="#a3b899", highlight="#f5e6cc", success="#4a7c59", warning="#96752e"),
    "windows_light": dict(name="Windows 浅色", description="浅灰背景与白色卡片，经典 Windows 蓝强调。", dark=False,
                          canvas="#f3f3f3", surface="#f9f9f9", card="#ffffff", border="#e5e5e5",
                          subtle_border="#f0f0f0", text="#1b1b1b", secondary="#5d5d5d", muted="#8d8d8d",
                          accent="#0067c0", hover="#f5f5f5", success="#16815d", warning="#99711a"),
    "windows_dark": dict(name="Windows 深色", description="深灰背景与微光描边，明亮 Windows 蓝强调。", dark=True,
                         canvas="#202020", surface="#2c2c2c", card="#2b2b2b", border="#3c3c3c",
                         subtle_border="#363636", text="#ffffff", secondary="#c5c5c5", muted="#7f7f7f",
                         accent="#4cc2ff", hover="#383838", success="#6cce9f", warning="#e6be67"),
}

# Keep the frosted family aligned with the existing Windows palettes instead
# of tinting every reading surface blue. Blue remains a semantic accent.
_SPECS["glass_day"] = dict(_SPECS["windows_light"], name="磨砂玻璃 · 日间", glass=True,
                          description="Windows 风格浅灰磨砂，白色内容区与蓝色重点操作。")
_SPECS["glass_night"] = dict(_SPECS["windows_dark"], name="磨砂玻璃 · 夜间", glass=True,
                            description="Windows 风格深灰磨砂，中性灰色层次与蓝色重点操作。")


def _rgb(value):
    return tuple(int(value[index:index+2], 16) for index in (1, 3, 5))


def _rgba(value, alpha):
    """A QSS surface tint, kept separate from opaque custom-painting colors."""
    red, green, blue = _rgb(value)
    return f"rgba({red}, {green}, {blue}, {alpha})"


def blend(first, second, amount):
    return "#" + "".join(f"{round(a*(1-amount)+b*amount):02x}" for a, b in zip(_rgb(first), _rgb(second)))


def _luminance(value):
    channels = [channel/255 for channel in _rgb(value)]
    linear = [channel/12.92 if channel <= .04045 else ((channel+.055)/1.055)**2.4 for channel in channels]
    return sum(a*b for a, b in zip(linear, (.2126, .7152, .0722)))


def _contrast(first, second):
    a, b = sorted((_luminance(first), _luminance(second)))
    return (b+.05)/(a+.05)


def _readable(foreground, background, target, minimum=4.5):
    # Keep the supplied color where possible, gently lift only small UI labels
    # that would disappear against a dark card or sidebar.
    for step in range(21):
        candidate = blend(foreground, target, step/20)
        if _contrast(candidate, background) >= minimum:
            return candidate
    return target


def _tokens(spec):
    p = dict(spec)
    p["glass"] = bool(spec.get("glass", False))
    dark = p["dark"]
    p["secondary"] = _readable(p["secondary"], p["card"], p["text"])
    p["muted"] = _readable(spec.get("muted", blend(p["secondary"], p["card"], .12)), p["card"], p["text"])
    p["on_accent"] = max(("#ffffff", "#14161c"), key=lambda color: _contrast(color, p["accent"]))
    p["accent_hover"] = blend(p["accent"], "#ffffff" if dark else "#000000", .12)
    p["accent_pressed"] = blend(p["accent"], "#000000", .20)
    p["accent_soft"] = blend(p["card"], spec.get("soft", p["accent"]), .28 if "soft" in spec else .16 if dark else .09)
    p["accent_border"] = blend(p["border"], p["accent"], .55)
    p["accent_text"] = _readable(p["accent"], p["accent_soft"], p["text"])
    p["hover"] = spec.get("hover", blend(p["card"], p["text"], .055 if dark else .035))
    p["subtle_border"] = spec.get("subtle_border", blend(p["border"], p["card"], .55))
    p["support"] = spec.get("support", p["accent"])
    p["action"] = spec.get("action", p["accent"])
    p["on_action"] = max(("#ffffff", "#14161c"), key=lambda color: _contrast(color, p["action"]))
    p["action_hover"] = blend(p["action"], "#ffffff" if dark else "#000000", .12)
    p["action_pressed"] = blend(p["action"], "#000000", .20)
    p["pressed"] = blend(p["card"], p["accent"], .12)
    p["border_hover"] = blend(p["border"], p["secondary"], .40)
    p["input"] = blend(p["card"], p["canvas"], .68)
    p["preview"] = blend(p["card"], p["canvas"], .55)
    p["image_preview"] = p["preview"]
    p["link_preview"] = blend(p["preview"], p["accent"], .025)
    p["link_text"] = _readable(spec["link"], p["link_preview"], p["text"]) if "link" in spec else p["accent_text"]
    p["badge_bg"] = blend(p["preview"], p["card"], .75)
    p["badge_text"] = p["secondary"]
    p["disabled_bg"] = blend(p["canvas"], p["card"], .50)
    p["disabled_fg"] = blend(p["secondary"], p["disabled_bg"], .38)
    p["danger"] = "#ed8a94" if dark else "#a64759"
    p["danger_bg"] = blend(p["card"], p["danger"], .12 if dark else .065)
    p["danger_border"] = blend(p["border"], p["danger"], .38)
    p["warning"] = _readable(p["warning"], p["card"], p["text"])
    p["warning_bg"] = blend(p["card"], p["warning"], .10)
    p["warning_border"] = blend(p["border"], p["warning"], .30)
    p["favorite"] = "#e2bb75" if dark else "#b08127"
    p["switch_off"] = blend(p["border"], p["secondary"], .40)
    p["switch_knob"] = p["on_accent"]
    p["scrollbar"] = blend(p["canvas"], p["secondary"], .30)
    p["scrollbar_hover"] = blend(p["canvas"], p["secondary"], .60)
    p["selection"] = blend(p["card"], p["accent"], .33)
    p["highlight"] = spec.get("highlight", blend(p["card"], p["warning"], .35))
    p["highlight_text"] = p["text"]
    # Image viewing stays on a neutral dark canvas; the surrounding controls
    # and OCR panel use the selected theme, never filtering the source image.
    p["viewer_canvas"] = "#181a20"
    p["viewer_text"] = "#e7e9ef"
    p["viewer_secondary"] = "#a6adbc"
    p["viewer_mark"] = "#464b58"
    # Native Acrylic belongs to the window. These are its foreground material
    # layers, rather than whole-window opacity or a filter over actual images.
    # Opaque tokens above remain intact for delegates, canvases and fallbacks.
    p["material_canvas"] = _rgba(p["canvas"], 104 if dark else 96)
    p["material_header"] = _rgba(p["card"], 152 if dark else 160)
    p["material_sidebar"] = _rgba(p["surface"], 94 if dark else 90)
    p["material_panel"] = _rgba(p["card"], 194 if dark else 208)
    p["material_inner"] = _rgba(p["card"], 225 if dark else 232)
    p["material_field"] = _rgba(p["input"], 238 if dark else 242)
    p["material_border"] = _rgba(p["border"], 195 if dark else 205)
    p["material_glint"] = _rgba("#ffffff", 30 if dark else 185)
    if p["glass"]:
        # A quiet translucent frame surrounds more substantial reading panes.
        # Native blur belongs behind these tints; image pixels remain opaque.
        for token, source, alpha in (
                ("canvas", "canvas", 104 if dark else 80),
                ("header", "card", 94 if dark else 80),
                ("sidebar", "surface", 82 if dark else 66),
                ("panel", "card", 180 if dark else 190),
                ("inner", "card", 218 if dark else 226),
                ("field", "input", 228 if dark else 236),
                ("border", "border", 170 if dark else 186)):
            p["material_" + token] = _rgba(p[source], alpha)
        p["material_glint"] = _rgba("#ffffff", 54 if dark else 205)
    return p


THEMES = {key: _tokens(spec) for key, spec in _SPECS.items()}


def normalize_theme(theme_id):
    return theme_id if isinstance(theme_id, str) and theme_id in THEMES else DEFAULT_THEME


def theme(theme_id=None):
    if theme_id is None:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        theme_id = app.property("snapvaultTheme") if app else DEFAULT_THEME
    return THEMES[normalize_theme(theme_id)]


_COLOR_ROLES = {
    "#202332": "text", "#f5f6f8": "canvas", "#ffffff": "card", "#e5e7eb": "border",
    "#888d9a": "muted", "#303444": "text", "#747987": "secondary", "#858a97": "muted",
    "#f0f1f5": "surface", "#565c6b": "secondary", "#e6e7ee": "hover", "#e6e6f9": "accent_soft",
    "#5050c7": "accent_text", "#969ba7": "muted", "#6a6acb": "accent_text", "#5b5bd6": "accent",
    "#f8f9fc": "input", "#e1e3eb": "border", "#4d5263": "secondary", "#e1e3ea": "border",
    "#f6f6fb": "hover", "#c9cbd8": "border_hover", "#ededf6": "pressed", "#bbbdd0": "border_hover",
    "#8c8ce5": "accent_border", "#b0b4bf": "disabled_fg", "#f7f8fa": "disabled_bg", "#edf0f3": "border",
    "#4c4cc3": "accent_hover", "#dcdcf0": "disabled_bg", "#ebdfe2": "danger_border", "#b25663": "danger",
    "#fff1f3": "danger_bg", "#e9bdc5": "danger_border", "#a33549": "danger", "#f5f5fa": "hover",
    "#f0f0fc": "accent_soft", "#f3f4f8": "hover", "#fff8e8": "warning_bg", "#aa7b25": "warning",
    "#e8eaf0": "border", "#deddf8": "selection", "#9898e8": "accent_border", "#f8f9fb": "preview",
    "#454b5b": "text", "#edf0f4": "border", "#f3f4f7": "preview", "#e9ebf0": "border",
    "#fffbef": "warning_bg", "#f4e9c7": "warning_border", "#a58239": "warning", "#d7dae3": "scrollbar",
    "#b8bdcc": "scrollbar_hover", "#e8e8f7": "accent_soft", "#bbc0cf": "border_hover",
    "#232535": "card", "#eef0f7": "text", "#4a4f5d": "secondary", "#eceef2": "hover",
    "#d5d7df": "border_hover", "#a4a8b3": "disabled_fg", "#c8c8ef": "accent_border",
    "#c1c4cf": "border_hover", "#9696e5": "accent_border", "#8b909d": "disabled_fg",
    "#4f4fc5": "accent_hover", "#4444b6": "accent_pressed",
}


def _recolor(stylesheet, palette):
    return re.sub(r"#[0-9a-fA-F]{6}", lambda match: palette[_COLOR_ROLES[match[0].lower()]], stylesheet)


def _material_style(p):
    """Native-only layers shared by app and locally styled preferences.

    A nativeGlass property is set by the compositor helper after its policy
    checks. No matching property means the complete opaque stylesheet wins.
    """
    if not p["glass"]:
        return ""
    root = 'QWidget#appRoot[nativeGlass="true"]'
    settings = (f'{root} QWidget#settingsPage',
                'QDialog[appGlass="true"][nativeGlass="true"] QWidget#settingsPage')
    settings_roots = ",\n".join(settings)
    settings_cards = ",\n".join(f"{scope} QFrame#settingCard, {scope} QFrame#settingsFooter" for scope in settings)
    settings_strips = ",\n".join(f"{scope} QFrame#retentionStrip, {scope} QFrame[focusOption=\"true\"]" for scope in settings)
    settings_inputs = ",\n".join(f"{scope} QLineEdit, {scope} QPlainTextEdit, {scope} QComboBox" for scope in settings)
    settings_selected = ",\n".join(f"{scope} QFrame[focusOption=\"true\"][selected=\"true\"]" for scope in settings)
    return f"""
/* Only the window chrome receives native material; source images stay opaque. */
QMainWindow[appGlass="true"][nativeGlass="true"],
QDialog[appGlass="true"][nativeGlass="true"] {{ background: transparent; }}
{root} {{ background: {p['material_canvas']}; }}
{root} QFrame#appHeader {{
    background: {p['material_header']}; border-bottom: 1px solid {p['material_border']};
}}
{root} QWidget#sidebar {{
    background: {p['material_sidebar']}; border-right: 1px solid {p['material_border']};
}}
{root} QFrame#storageCard {{
    background: {p['material_panel']}; border: 1px solid {p['material_border']};
    border-top-color: {p['material_glint']}; border-radius: 12px;
}}
{root} QFrame#toolbar, {root} QFrame#details, {root} QFrame#statusCard {{
    background: {p['material_panel']}; border: 1px solid {p['material_border']};
    border-top-color: {p['material_glint']}; border-radius: 15px;
}}
{root} QLineEdit#gallerySearch {{
    background: {p['material_field']}; border: 1px solid {p['material_border']}; border-radius: 10px;
}}
{root} QLineEdit#gallerySearch:focus {{ background: {p['card']}; border-color: {p['accent_border']}; }}
{root} QFrame#details QTextEdit, {root} QListWidget#detectedLinks {{
    background: {p['material_field']}; border-color: {p['material_border']}; border-radius: 10px;
}}
{root} QLabel#previewSurface {{ background: {p['preview']}; border-color: {p['material_border']}; }}
QMainWindow[appGlass="true"][nativeGlass="true"] QStatusBar {{
    background: {p['material_sidebar']}; border-top: 1px solid {p['material_border']};
}}
{settings_roots} {{ background: transparent; }}
{settings_cards} {{
    background: {p['material_panel']}; border: 1px solid {p['material_border']};
    border-top-color: {p['material_glint']}; border-radius: 15px;
}}
{settings_strips} {{ background: {p['material_inner']}; border-color: {p['material_border']}; }}
{settings_selected} {{ background: {p['accent_soft']}; border-color: {p['accent_border']}; }}
{settings_inputs} {{ background: {p['material_field']}; border-color: {p['material_border']}; }}
"""


@lru_cache(maxsize=len(_SPECS))
def style_for(theme_id=DEFAULT_THEME):
    p = theme(theme_id)
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    chevron = (root / "assets" / ("chevron-dark.png" if p["dark"] else "chevron-light.png")).as_posix()
    return _recolor(_BASE_STYLE, p) + f"""
    QPushButton#primary, QPushButton#pinToScreen, QPushButton#copyTextAction[primaryAction="true"] {{ background: {p['action']}; border-color: {p['action']}; color: {p['on_action']}; }}
    QPushButton#primary:hover, QPushButton#pinToScreen:hover, QPushButton#copyTextAction[primaryAction="true"]:hover {{ background: {p['action_hover']}; border-color: {p['action_hover']}; }}
    QPushButton#primary:disabled, QPushButton#pinToScreen:disabled {{ color: {p['disabled_fg']}; }}
    QWidget#sidebar {{ background: {p['surface']}; }}
    QLineEdit#gallerySearch, QLabel#previewSurface {{ border-color: {p['subtle_border']}; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 8px; }}
    QListWidget#detectedLinks {{ background: {p['input']}; border: 1px solid {p['subtle_border']}; border-radius: 8px; padding: 3px; font-size: 12px; }}
    QListWidget#detectedLinks::item {{ color: {p['link_text']}; padding: 4px 6px; border-radius: 6px; }}
    QListWidget#detectedLinks::item:selected, QListWidget#detectedLinks::item:hover {{ background: {p['accent_soft']}; color: {p['accent_text']}; }}
    QFrame#similarBox {{ background: transparent; border: none; }}
    """ + _material_style(p)


def preferences_style(theme_id=None):
    p = theme(theme_id)
    return _recolor(_PREFERENCES_STYLE, p).replace("color: white;", "color: " + p["on_action"] + ";") + f"""
    QPushButton#primary {{ background: {p['action']}; border-color: {p['action']}; color: {p['on_action']}; }}
    QPushButton#primary:hover {{ background: {p['action_hover']}; border-color: {p['action_hover']}; }}
    QPushButton#primary:pressed {{ background: {p['action_pressed']}; border-color: {p['action_pressed']}; }}
    QPlainTextEdit#excludedApps {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 8px; padding: 8px; font-family: Consolas, 'Microsoft YaHei UI'; font-size: 12px; selection-background-color: {p['selection']}; }}
    QPlainTextEdit#excludedApps:focus {{ border-color: {p['accent_border']}; }}
    QLabel#hotkeyError {{ color: {p['danger']}; font-size: 12px; }}
    QLineEdit#hotkeyField {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 8px; padding: 8px 10px; font-weight: 600; }}
    QLineEdit#hotkeyField:focus {{ border-color: {p['accent']}; background: {p['accent_soft']}; color: {p['accent_text']}; }}
    QFrame#glassModeRow {{ background: transparent; border: none; }}
    QPushButton[glassMode="true"] {{ background: {p['input']}; color: {p['secondary']}; border: 1px solid {p['border']}; border-radius: 9px; padding: 0 24px; font-weight: 600; }}
    QPushButton[glassMode="true"]:hover {{ background: {p['hover']}; color: {p['text']}; border-color: {p['border_hover']}; }}
    QPushButton[glassMode="true"]:checked {{ background: {p['accent_soft']}; color: {p['accent_text']}; border-color: {p['accent_border']}; }}
    QPushButton[glassMode="true"]:focus {{ border: 2px solid {p['accent']}; }}
    """ + _material_style(p)


def apply_theme(app, theme_id):
    from PySide6.QtGui import QColor, QPalette
    theme_id = normalize_theme(theme_id)
    p = theme(theme_id)
    app.setProperty("snapvaultTheme", theme_id)
    _apply_app_color_scheme(app, p["dark"])
    palette = QPalette()
    for role, token in (("Window", "canvas"), ("WindowText", "text"), ("Base", "card"),
                        ("AlternateBase", "input"), ("Text", "text"), ("Button", "card"),
                        ("ButtonText", "text"), ("Highlight", "selection"), ("HighlightedText", "text"),
                        ("ToolTipBase", "card"), ("ToolTipText", "text"), ("Link", "accent")):
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(p[token]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p["disabled_fg"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p["disabled_fg"]))
    app.setPalette(palette)
    app.setStyleSheet(style_for(theme_id))
    return theme_id


def _apply_app_color_scheme(app, dark):
    """Keep Qt's Windows caption renderer on the selected app appearance.

    ExpandedClientAreaHint uses a native titlebar child whose glyphs follow
    QStyleHints.colorScheme(), independently of QPalette and DWM text color.
    This override belongs only to this application; it changes no OS setting.
    https://doc.qt.io/qt-6/qstylehints.html#colorScheme-prop
    """
    from PySide6.QtCore import Qt
    desired = Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light
    requested = "dark" if dark else "light"
    try:
        hints = app.styleHints()
        # Set an explicit override even when the initial system appearance
        # happens to match. Cache the *request* before Qt synchronously emits
        # its theme notifications, avoiding repeated palette/theme resets.
        # QWindowsTheme::requestColorScheme() always refreshes those settings.
        if app.property("snapvaultColorSchemeOverride") != requested:
            app.setProperty("snapvaultColorSchemeOverride", requested)
            try:
                hints.setColorScheme(desired)
            except (AttributeError, RuntimeError):
                app.setProperty("snapvaultColorSchemeOverride", None)
                raise
        return hints.colorScheme()
    except (AttributeError, RuntimeError):
        # Older/platform-limited Qt versions can still use the explicit app
        # palette and the native DWM settings below as a readable fallback.
        return None


def native_titlebar(widget, theme_id=None):
    import ctypes
    import os
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if os.name != "nt" or not app or app.platformName() != "windows":
        return
    p = theme(theme_id)
    scheme = _apply_app_color_scheme(app, p["dark"])
    state = dict(dark=bool(p["dark"]), qt_scheme=getattr(scheme, "name", "unavailable"),
                 attributes={}, redrawn=False)
    try:
        from ctypes import wintypes
        hwnd = wintypes.HWND(int(widget.winId()))
        dark = ctypes.c_int(int(p["dark"]))
        dwm = ctypes.WinDLL("dwmapi.dll", winmode=0x00000800)
        setter = dwm.DwmSetWindowAttribute
        setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        setter.restype = ctypes.c_long
        state["attributes"]["20"] = int(setter(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark)))
        for attribute, color in ((35, p["surface"]), (36, p["text"])):
            red, green, blue = _rgb(color)
            value = ctypes.c_uint(red | green << 8 | blue << 16)
            state["attributes"][str(attribute)] = int(setter(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)))
        # Repaint this window's real caption and Qt's native titlebar child.
        # No style flags, HWND, geometry or system preferences are changed.
        user = ctypes.WinDLL("user32.dll", winmode=0x00000800)
        user.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.HRGN, wintypes.UINT]
        user.RedrawWindow.restype = wintypes.BOOL
        state["redrawn"] = bool(user.RedrawWindow(hwnd, None, None, 0x0001 | 0x0080 | 0x0400))
    except (OSError, AttributeError, RuntimeError, TypeError, ValueError, ctypes.ArgumentError) as exc:
        state["error"] = type(exc).__name__
    # DWM caption/text colors are set-only attributes on supported Windows;
    # retain their HRESULTs for diagnostics rather than inventing a readback.
    return state


def inspector_style(theme_id=None):
    p = theme(theme_id)
    stylesheet = f"""
QDialog#inspector {{ background: {p['viewer_canvas']}; }}
QWidget#inspectorHeader {{ background: {p['card']}; border-bottom: 1px solid {p['border']}; }}
QWidget#inspectorHeader QLabel {{ color: {p['text']}; background: transparent; }}
QWidget#inspectorHeader QLabel#muted {{ color: {p['secondary']}; font-size: 12px; }}
QWidget#inspectorHeader QPushButton {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 8px; padding: 8px 12px; }}
QWidget#inspectorHeader QPushButton:hover {{ background: {p['hover']}; }}
QWidget#inspectorHeader QPushButton:checked {{ background: {p['accent_soft']}; color: {p['accent_text']}; border-color: {p['accent_border']}; }}
QWidget#inspectorCanvas, QWidget#inspectorFooter {{ background: {p['viewer_canvas']}; }}
QWidget#inspectorFooter QLabel {{ color: {p['viewer_secondary']}; background: transparent; font-size: 11px; }}
QFrame#inspectorTools {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 18px; }}
QFrame#inspectorTools QPushButton {{ background: transparent; color: {p['text']}; border: none; border-radius: 9px; padding: 8px 10px; min-height: 18px; }}
QFrame#inspectorTools QPushButton:hover {{ background: {p['hover']}; }}
QFrame#inspectorTools QPushButton:disabled {{ color: {p['disabled_fg']}; }}
QFrame#inspectorTools QPushButton#primary {{ background: {p['action']}; color: {p['on_action']}; padding: 8px 14px; }}
QFrame#inspectorTools QPushButton#primary:hover {{ background: {p['action_hover']}; }}
QFrame#inspectorTools QPushButton#primary:disabled {{ background: {p['disabled_bg']}; color: {p['disabled_fg']}; }}
QFrame#inspectorTools QLabel {{ color: {p['secondary']}; background: transparent; font-size: 12px; }}
QFrame#inspectorTools QLabel#inspectorZoom {{ color: {p['accent_text']}; background: {p['accent_soft']}; border-radius: 7px; padding: 6px; font-weight: 600; }}
QFrame#inspectorOcr {{ background: {p['card']}; border-left: 1px solid {p['border']}; }}
QFrame#inspectorOcr QLabel {{ color: {p['text']}; background: transparent; }}
QFrame#inspectorOcr QLabel#muted {{ color: {p['secondary']}; font-size: 11px; }}
QFrame#inspectorOcr QLabel#detailtitle {{ font-size: 17px; font-weight: 600; }}
QFrame#inspectorOcr QTextEdit {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 10px; padding: 13px; font-size: 13px; selection-background-color: {p['selection']}; }}
QFrame#inspectorOcr QPushButton {{ color: {p['accent_text']}; background: {p['accent_soft']}; border: 1px solid {p['border']}; border-radius: 8px; padding: 8px 12px; }}
QFrame#inspectorOcr QPushButton:hover {{ background: {p['hover']}; }}
QFrame#inspectorOcr QPushButton:disabled {{ color: {p['disabled_fg']}; background: {p['disabled_bg']}; }}
QSplitter#inspectorSplit::handle {{ background: {p['border']}; width: 1px; }}
QFrame#inspectorSeparator {{ background: {p['border']}; max-width: 1px; border: none; }}
QLabel#inspectorMark {{ color: {p['accent']}; font-size: 25px; }}
QLabel#inspectorEmpty {{ color: {p['viewer_secondary']}; padding: 16px; }}
QDialog#inspector[nativeGlass="true"] {{ background: transparent; }}
QDialog#inspector[nativeGlass="true"] QWidget#inspectorHeader {{
    background: {p['material_header']}; border-bottom-color: {p['material_border']};
}}
QDialog#inspector[nativeGlass="true"] QWidget#inspectorFooter {{ background: {p['material_canvas']}; }}
QDialog#inspector[nativeGlass="true"] QFrame#inspectorTools {{
    background: {p['material_panel']}; border: 1px solid {p['material_border']};
    border-top-color: {p['material_glint']};
}}
QDialog#inspector[nativeGlass="true"] QFrame#inspectorOcr {{
    background: {p['material_panel']}; border-left-color: {p['material_border']};
}}
QDialog#inspector[nativeGlass="true"] QFrame#inspectorOcr QTextEdit {{ background: {p['material_field']}; }}
/* Image canvas is intentionally opaque, including transparent source pixels. */
QDialog#inspector[nativeGlass="true"] QWidget#inspectorCanvas {{ background: {p['viewer_canvas']}; }}
"""
    return stylesheet if p["glass"] else stylesheet.split('QDialog#inspector[nativeGlass="true"]')[0]


def pin_style(theme_id=None):
    p = theme(theme_id)
    red, green, blue = _rgb(p["card"])
    return f"""
QFrame#pinToolbar {{ background: rgba({red}, {green}, {blue}, 245); border: 1px solid {p['border']}; border-radius: 9px; }}
QLabel {{ color: {p['text']}; background: transparent; border: none; font-size: 11px; }}
QPushButton {{ color: {p['secondary']}; background: transparent; border: none; border-radius: 5px; padding: 0; font-size: 19px; }}
QPushButton:hover {{ color: {p['danger']}; background: {p['danger_bg']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p['border']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p['accent']}; width: 12px; margin: -4px 0; border-radius: 6px; }}
"""

def quick_style(theme_id=None):
    p = theme(theme_id)
    return f"""
QWidget#quickPaste {{ background: {p['card']}; }}
QFrame#quickFrame {{ background: {p['card']}; border: 1px solid {p['accent_border']}; border-radius: 12px; }}
QLineEdit#quickSearch {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 9px; padding: 11px 12px; font-size: 15px; selection-background-color: {p['selection']}; }}
QLineEdit#quickSearch:focus {{ border-color: {p['accent_border']}; }}
QListWidget#quickResults {{ background: transparent; border: none; outline: none; }}
QListWidget#quickResults::item {{ color: {p['text']}; border-radius: 8px; padding: 6px 8px; margin: 1px 0; }}
QListWidget#quickResults::item:selected {{ background: {p['accent_soft']}; color: {p['accent_text']}; }}
QListWidget#quickResults::item:hover:!selected {{ background: {p['hover']}; }}
QListWidget#recentResults {{ background: transparent; border: none; outline: none; font-size: 13px; }}
QListWidget#recentResults::item {{ color: {p['text']}; border: 1px solid {p['border']}; border-radius: 9px; padding: 4px 10px; margin: 3px 0; }}
QListWidget#recentResults::item:selected {{ background: {p['accent_soft']}; border-color: {p['accent_border']}; color: {p['accent_text']}; }}
QListWidget#recentResults::item:hover:!selected {{ background: {p['hover']}; }}
QLabel#quickTitle {{ color: {p['text']}; font-size: 16px; font-weight: 600; background: transparent; }}
QLabel#quickStatus {{ color: {p['accent_text']}; font-size: 11px; background: transparent; }}
QPushButton#quickTool {{ color: {p['secondary']}; background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 6px 10px; font-size: 11px; }}
QPushButton#quickTool:hover {{ background: {p['hover']}; color: {p['text']}; }}
QPushButton#quickTool:checked {{ background: {p['accent_soft']}; border-color: {p['accent_border']}; color: {p['accent_text']}; }}
QPushButton#quickTool:disabled {{ color: {p['muted']}; background: transparent; }}
QPushButton#quickClose {{ color: {p['secondary']}; background: transparent; border: none; font-size: 19px; padding: 1px 8px; }}
QPushButton#quickClose:hover {{ color: {p['danger']}; background: {p['danger_bg']}; border-radius: 5px; }}
QTabBar#quickTabs::tab {{ color: {p['secondary']}; background: {p['surface']}; padding: 10px 20px; border: 1px solid {p['border']}; }}
QTabBar#quickTabs::tab:selected {{ color: {p['accent_text']}; background: {p['accent_soft']}; border-bottom: 2px solid {p['accent']}; }}
QTabBar#quickTabs::tab:first {{ border-top-left-radius: 7px; border-bottom-left-radius: 7px; }}
QTabBar#quickTabs::tab:last {{ border-top-right-radius: 7px; border-bottom-right-radius: 7px; }}
QLabel#quickHint {{ color: {p['muted']}; font-size: 11px; background: transparent; }}
QLabel#quickEmpty {{ color: {p['secondary']}; font-size: 12px; background: transparent; padding: 18px; }}
"""


def screenshot_style(theme_id=None):
    p = theme(theme_id)
    red, green, blue = _rgb(p["card"])
    return f"""
QWidget#screenshotTools {{ background: rgba({red}, {green}, {blue}, 245); border: 1px solid {p['border']}; border-radius: 9px; }}
QWidget#screenshotTools QPushButton {{ color: {p['text']}; background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 6px 12px; font-size: 13px; }}
QWidget#screenshotTools QPushButton:hover {{ background: {p['hover']}; border-color: {p['border']}; }}
QWidget#screenshotTools QPushButton#primary {{ background: {p['action']}; color: {p['on_action']}; font-weight: 600; }}
QWidget#screenshotTools QPushButton#primary:hover {{ background: {p['action_hover']}; }}
"""


def review_style(theme_id=None):
    p = theme(theme_id)
    stylesheet = f"""
QFrame#reviewPreview {{ background: {p['preview']}; border: 1px solid {p['border']}; border-radius: 10px; }}
QLabel#reviewImage {{ background: transparent; color: {p['secondary']}; }}
QTextEdit#reviewText {{ background: {p['preview']}; border: none; color: {p['text']}; font-size: 13px; }}
QFrame#reviewTags {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 10px; }}
QCheckBox#reviewTag {{ font-size: 13px; padding: 6px 4px; }}
QLabel#reviewAuto {{ color: {p['accent_text']}; background: {p['accent_soft']}; border-radius: 7px; padding: 6px 9px; font-size: 12px; }}
QLabel#reviewProgress {{ color: {p['secondary']}; font-size: 12px; }}
QLabel#reviewHint {{ color: {p['muted']}; font-size: 11px; }}
QStackedWidget#reviewPages {{ background: {p['preview']}; }}
QDialog#reviewDialog[nativeGlass="true"] {{ background: transparent; }}
QDialog#reviewDialog[nativeGlass="true"] QFrame#reviewPreview,
QDialog#reviewDialog[nativeGlass="true"] QFrame#reviewTags {{
    background: {p['material_panel']}; border: 1px solid {p['material_border']};
    border-top-color: {p['material_glint']}; border-radius: 14px;
}}
QDialog#reviewDialog[nativeGlass="true"] QTextEdit#reviewText {{ background: {p['material_field']}; }}
"""
    return stylesheet if p["glass"] else stylesheet.split('QDialog#reviewDialog[nativeGlass="true"]')[0]


_BASE_STYLE = """
* { font-family: 'Segoe UI Variable', 'Segoe UI', 'Microsoft YaHei UI'; font-size: 13px; color: #202332; }
QMainWindow, QDialog, QWidget#appRoot { background: #f5f6f8; }
QLabel { background: transparent; }
QFrame#appHeader { background: #ffffff; border-bottom: 1px solid #e5e7eb; }
QLabel#brand { font-size: 19px; font-weight: 700; }
QLabel#brandSubtitle { font-size: 10px; color: #888d9a; }
QLabel#title { font-size: 23px; font-weight: 600; }
QLabel#detailtitle, QLabel#sectionTitle { font-size: 15px; font-weight: 600; }
QLabel#galleryHeading { font-size: 18px; font-weight: 600; }
QLabel#selectionTitle { font-size: 14px; font-weight: 600; color: #303444; }
QLabel#muted, QLabel#settingDescription { color: #747987; font-size: 12px; }
QLabel#eyebrow { color: #858a97; font-size: 11px; font-weight: 500; }
QLabel#settingTitle { font-size: 14px; font-weight: 600; }
QWidget#sidebar { background: #f0f1f5; border-right: 1px solid #e5e7eb; }
QWidget#sidebar QPushButton { text-align: left; background: transparent; border: 0; border-radius: 8px; padding: 9px 12px; color: #565c6b; }
QWidget#sidebar QPushButton:hover { background: #e6e7ee; }
QWidget#sidebar QPushButton:checked { background: #e6e6f9; color: #5050c7; font-weight: 600; }
QWidget#sidebar QPushButton#navItem { padding: 0; }
QWidget#sidebar QPushButton#navItem QLabel { color: #565c6b; background: transparent; font-weight: 400; }
QWidget#sidebar QPushButton#navItem QLabel#navCount { color: #969ba7; font-size: 11px; }
QWidget#sidebar QPushButton#navItem QLabel[navSelected="true"] { color: #5050c7; font-weight: 600; }
QWidget#sidebar QPushButton#navItem QLabel#navCount[navSelected="true"] { color: #6a6acb; }
QWidget#sidebar QPushButton#folderButton { color: #5b5bd6; padding: 5px 0; font-size: 12px; }
QWidget#sidebar QPushButton#categoryClear { color: #858a97; padding: 2px 7px; font-size: 11px; }
QFrame#storageCard { background: #f8f9fc; border: 1px solid #e1e3eb; border-radius: 10px; }
QFrame#toolbar, QFrame#details, QFrame#settingCard, QFrame#statusCard { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; }
QLabel#storageSize { color: #4d5263; font-size: 20px; font-weight: 600; }
QPushButton { background: #ffffff; border: 1px solid #e1e3ea; border-radius: 7px; padding: 7px 11px; color: #565c6b; }
QPushButton:hover { background: #f6f6fb; border-color: #c9cbd8; }
QPushButton:pressed { background: #ededf6; border-color: #bbbdd0; }
QPushButton:focus { border-color: #8c8ce5; }
QPushButton:disabled { color: #b0b4bf; background: #f7f8fa; border-color: #edf0f3; }
QPushButton#primary, QPushButton#pinToScreen, QPushButton#copyTextAction[primaryAction="true"] { background: #5b5bd6; color: #ffffff; border-color: #5b5bd6; font-weight: 600; }
QPushButton#primary:hover, QPushButton#pinToScreen:hover, QPushButton#copyTextAction[primaryAction="true"]:hover { background: #4c4cc3; border-color: #4c4cc3; }
QPushButton#primary:disabled, QPushButton#pinToScreen:disabled { background: #dcdcf0; border-color: #dcdcf0; color: #ffffff; }
QPushButton#danger { background: #ffffff; border-color: #ebdfe2; color: #b25663; }
QPushButton#danger:hover { background: #fff1f3; border-color: #e9bdc5; color: #a33549; }
QPushButton#headerNav { background: transparent; border-color: transparent; padding: 8px 13px; color: #747987; font-size: 12px; }
QPushButton#headerNav:hover { background: #f5f5fa; }
QPushButton#headerNav:checked { background: #f0f0fc; color: #5050c7; font-weight: 600; }
QPushButton#statusPill, QPushButton#textCaptureToggle { background: transparent; border-color: transparent; border-radius: 7px; font-size: 11px; padding: 6px 8px; color: #747987; }
QPushButton#statusPill:hover, QPushButton#textCaptureToggle:hover { background: #f3f4f8; }
QPushButton#statusPill[paused="true"] { background: #fff8e8; color: #aa7b25; }
QPushButton#filterChip { background: transparent; border-color: transparent; border-radius: 6px; padding: 6px 9px; font-size: 11px; color: #858a97; }
QPushButton#filterChip:hover { background: #f5f5fa; }
QPushButton#filterChip:checked { background: #f0f0fc; color: #5b5bd6; font-weight: 600; }
QPushButton#iconButton { background: transparent; border-color: transparent; padding: 6px; color: #858a97; }
QPushButton#iconButton:hover { background: #f0f0fc; }
QLineEdit, QComboBox { background: #f7f8fa; border: 1px solid #e8eaf0; border-radius: 7px; padding: 8px 10px; color: #565c6b; selection-background-color: #deddf8; }
QLineEdit:focus, QComboBox:focus { border-color: #9898e8; background: #ffffff; }
QLineEdit#gallerySearch { background: #f7f8fa; padding: 10px; border-color: #f0f1f5; }
QLineEdit#gallerySearch:focus { background: #ffffff; border-color: #9898e8; }
QComboBox#itemTypeFilter, QComboBox#gallerySort { background: transparent; border-color: transparent; padding: 6px 9px; font-size: 11px; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: #ffffff; color: #565c6b; selection-background-color: #f0f0fc; selection-color: #5050c7; padding: 6px; border: 1px solid #e1e3eb; }
QListWidget { background: transparent; border: none; outline: none; }
QTextEdit { background: #f8f9fb; color: #454b5b; border: 1px solid #edf0f4; border-radius: 8px; padding: 11px; font-size: 13px; selection-background-color: #deddf8; }
QLabel#previewSurface { background: #f3f4f7; border: 1px solid #e9ebf0; border-radius: 9px; color: #969ba7; }
QLabel#metadata { background: transparent; color: #858a97; padding: 2px 0; font-size: 11px; }
QLabel#retentionBanner { background: transparent; color: #969ba7; padding: 4px 2px; font-size: 11px; }
QFrame#pauseBanner { background: #fffbef; border: 1px solid #f4e9c7; border-radius: 9px; }
QFrame#pauseBanner QLabel { color: #a58239; font-size: 12px; }
QFrame#pauseBanner QPushButton { color: #a58239; border-color: transparent; background: transparent; font-size: 12px; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 2px 0; }
QScrollBar::handle:vertical { background: #d7dae3; border-radius: 3px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: #b8bdcc; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollArea { border: none; background: transparent; }
QSplitter::handle { background: transparent; }
QSplitter::handle:hover { background: #e8e8f7; }
QStatusBar { background: #f5f6f8; color: #969ba7; border-top: 1px solid #e5e7eb; padding: 2px 6px; font-size: 11px; }
QStatusBar::item { border: none; }
QMenu { background: #ffffff; border: 1px solid #e1e3eb; border-radius: 9px; padding: 6px; }
QMenu::item { padding: 9px 26px; border-radius: 6px; }
QMenu::item:selected { background: #f0f0fc; color: #5050c7; }
QMenu::separator { height: 1px; background: #e9ebf0; margin: 5px 8px; }
QToolTip { color: #565c6b; background: #ffffff; border: 1px solid #e1e3eb; padding: 6px; }
QTabWidget::pane { background: transparent; border: none; }
QTabBar::tab { background: transparent; color: #858a97; padding: 10px 17px; margin-right: 4px; border-radius: 7px; }
QTabBar::tab:selected { background: #ffffff; color: #5b5bd6; font-weight: 600; }
QWidget#settingsPage { background: #f5f6f8; }
QCheckBox { spacing: 9px; padding: 4px 0; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #bbc0cf; background: #ffffff; }
QCheckBox::indicator:checked { background: #5b5bd6; border-color: #5b5bd6; image: none; }
QRadioButton { spacing: 8px; padding: 5px; }
QDialogButtonBox QPushButton { min-width: 75px; }
QFrame#inspectorHeader, QFrame#inspectorTools { background: #232535; border: 0; }
QFrame#inspectorHeader QLabel { color: #eef0f7; }
"""


_PREFERENCES_STYLE = """
            QWidget#settingsPage { background: #f5f6f8; }
            QFrame#settingsIntro { background: transparent; border: none; }
            QLabel#settingsTitle { color: #202332; font-size: 22px; font-weight: 600; }
            QFrame#settingCard { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; }
            QLabel#settingTitle { color: #202332; font-size: 13px; font-weight: 600; }
            QLabel#settingDescription, QLabel#browserEngineStatus { color: #747987; font-size: 12px; }
            QLabel#sectionTitle { color: #4a4f5d; font-size: 12px; font-weight: 600; padding: 4px 2px; }
            QLabel#settingBadge { color: #5b5bd6; background: #f0f0fc; border-radius: 7px; padding: 5px 9px; font-size: 11px; }
            QTabWidget#preferencesTabs::pane { border: none; background: transparent; }
            QTabWidget#preferencesTabs QTabBar::tab { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 9px 18px; margin: 0 6px 10px 0; color: #747987; }
            QTabWidget#preferencesTabs QTabBar::tab:hover { background: #eceef2; color: #202332; }
            QTabWidget#preferencesTabs QTabBar::tab:selected { background: #ffffff; border-color: #e5e7eb; color: #5b5bd6; font-weight: 600; }
            QScrollArea#preferencesScroll { border: none; background: transparent; }
            QWidget#preferencesContent { background: transparent; }
            QPushButton { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; color: #4a4f5d; padding: 8px 13px; }
            QPushButton:hover { background: #f5f6f8; border-color: #d5d7df; color: #202332; }
            QPushButton:pressed { background: #eceef2; }
            QPushButton:disabled { color: #a4a8b3; background: #f7f8fa; border-color: #eceef2; }
            QFrame#retentionStrip { background: #f5f6f8; border: 1px solid #eceef2; border-radius: 9px; }
            QPushButton[retentionOption="true"] { border: 1px solid transparent; border-radius: 7px; color: #747987; background: transparent; padding: 9px 5px; }
            QPushButton[retentionOption="true"]:hover { background: #eceef2; }
            QPushButton[retentionOption="true"]:checked { background: #ffffff; border-color: #e5e7eb; color: #5b5bd6; font-weight: 600; }
            QFrame[focusOption="true"] { background: #f8f9fb; border: 1px solid #eceef2; border-radius: 9px; }
            QFrame[focusOption="true"][selected="true"] { background: #f0f0fc; border-color: #c8c8ef; }
            QFrame[focusOption="true"] QRadioButton { color: #202332; font-size: 12px; font-weight: 600; background: transparent; padding: 0; }
            QFrame[focusOption="true"] QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid #c1c4cf; border-radius: 7px; background: #ffffff; }
            QFrame[focusOption="true"] QRadioButton::indicator:checked { width: 8px; height: 8px; border: 4px solid #5b5bd6; background: #ffffff; }
            QFrame[focusOption="true"] QRadioButton::indicator:disabled { border-color: #d5d7df; background: #f5f6f8; }
            QLineEdit#storagePath { background: #f8f9fb; border: 1px solid #e5e7eb; color: #747987; border-radius: 8px; padding: 10px; }
            QComboBox { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 9px 12px; color: #4a4f5d; }
            QComboBox:focus { border-color: #9696e5; }
            QComboBox:disabled { color: #8b909d; background: #f8f9fb; }
            QFrame#settingsFooter { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; }
            QPushButton#primary { background: #5b5bd6; color: white; border: 1px solid #5b5bd6; border-radius: 8px; padding: 9px 20px; font-weight: 600; }
            QPushButton#primary:hover { background: #4f4fc5; border-color: #4f4fc5; }
            QPushButton#primary:pressed { background: #4444b6; }
        """


# Compatibility for existing entrypoints and isolated diagnostics.
STYLE = style_for(DEFAULT_THEME)
