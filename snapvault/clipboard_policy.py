"""Clipboard privacy rules: Windows no-collect markers, excluded programs and likely secrets."""
from __future__ import annotations

import ctypes
import re
from functools import lru_cache
import os

EXCLUDE_FORMAT = "ExcludeClipboardContentFromMonitorProcessing"
HISTORY_FORMAT = "CanIncludeInClipboardHistory"


@lru_cache(maxsize=1)
def _native_api():
    if os.name != "nt":
        return None
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("RegisterClipboardFormatW", [wintypes.LPCWSTR], wintypes.UINT),
        ("IsClipboardFormatAvailable", [wintypes.UINT], wintypes.BOOL),
        ("OpenClipboard", [wintypes.HWND], wintypes.BOOL),
        ("CloseClipboard", [], wintypes.BOOL),
        ("GetClipboardData", [wintypes.UINT], wintypes.HANDLE),
    ):
        function = getattr(user, name)
        function.argtypes, function.restype = args, result
    for name, args, result in (
        ("GlobalLock", [wintypes.HGLOBAL], ctypes.c_void_p),
        ("GlobalUnlock", [wintypes.HGLOBAL], wintypes.BOOL),
        ("GlobalSize", [wintypes.HGLOBAL], ctypes.c_size_t),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return user, kernel, user.RegisterClipboardFormatW(EXCLUDE_FORMAT), user.RegisterClipboardFormatW(HISTORY_FORMAT)


def clipboard_excluded():
    """Honor Windows no-history markers; unreadable marked items are skipped."""
    api = _native_api()
    if api is None:
        return False
    user, kernel, exclude, history = api
    if exclude and user.IsClipboardFormatAvailable(exclude):
        return True
    if not history or not user.IsClipboardFormatAvailable(history):
        return False
    if not user.OpenClipboard(None):
        return True
    try:
        handle = user.GetClipboardData(history)
        if not handle or kernel.GlobalSize(handle) < 4:
            return True
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            return True
        try:
            return ctypes.c_uint32.from_address(pointer).value == 0
        finally:
            kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()


def clipboard_has_files(mime=None):
    """File Explorer copy is not a request to collect file paths or contents."""
    if mime is not None:
        for format_name in ('application/x-qt-windows-mime;value="FileNameW"',
                            'application/x-qt-windows-mime;value="FileName"'):
            if mime.hasFormat(format_name):
                return True
        if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            return True
    api = _native_api()
    return bool(api and api[0].IsClipboardFormatAvailable(15))  # CF_HDROP


def should_exclude_clipboard(mime=None):
    if mime is not None:
        if mime.hasFormat(EXCLUDE_FORMAT) or mime.hasFormat(
                f'application/x-qt-windows-mime;value="{EXCLUDE_FORMAT}"'):
            return True
        for name in (HISTORY_FORMAT, f'application/x-qt-windows-mime;value="{HISTORY_FORMAT}"'):
            if mime.hasFormat(name):
                marker = bytes(mime.data(name))
                if len(marker) < 4 or int.from_bytes(marker[:4], 'little') == 0:
                    return True
    return clipboard_has_files(mime) or clipboard_excluded()


# Password managers that may not set the Windows no-history markers on every
# copy. Users can extend the list in preferences; names are compared casefolded.
DEFAULT_EXCLUDED_APPS = (
    "keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "enpass.exe",
    "roboform.exe", "nordpass.exe", "dashlane.exe", "lastpass.exe", "proton pass.exe",
)


@lru_cache(maxsize=1)
def _process_api():
    if os.name != "nt":
        return None
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("GetClipboardOwner", [], wintypes.HWND),
        ("GetForegroundWindow", [], wintypes.HWND),
        ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
    ):
        function = getattr(user, name)
        function.argtypes, function.restype = args, result
    for name, args, result in (
        ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        ("QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return user, kernel


def _process_name(api, hwnd):
    from ctypes import wintypes
    user, kernel = api
    pid = wintypes.DWORD()
    if not hwnd or not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
        return ""
    handle = kernel.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value).casefold()
    finally:
        kernel.CloseHandle(handle)


def clipboard_source_app(*, foreground_fallback=True):
    """Executable name of the program that put the current item on the clipboard.

    Many programs copy through a hidden owner window; some copy without one.
    For automatic collection the foreground program is then the best guess,
    because a copy follows a user action there. Manual paste passes
    foreground_fallback=False, since the foreground program is 拾影 itself.
    """
    api = _process_api()
    if api is None:
        return ""
    name = _process_name(api, api[0].GetClipboardOwner())
    if not name and foreground_fallback:
        name = _process_name(api, api[0].GetForegroundWindow())
    return name


def normalize_app_list(value):
    """Accept a list or newline/comma separated text; keep plain executable names."""
    if isinstance(value, str):
        value = re.split(r"[\r\n,，;；]+", value)
    names = []
    for item in value or ():
        name = os.path.basename(str(item).strip().strip('"')).casefold()
        if name and name not in names:
            if not name.endswith(".exe"):
                name += ".exe"
            names.append(name)
    return names


def app_excluded(name, excluded):
    return bool(name) and name.casefold() in {item.casefold() for item in excluded}


_SECRET_PATTERNS = (
    ("私钥", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("访问令牌", re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
                            r"|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_\-]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}"
                            r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35})")),
    ("访问令牌", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("密码", re.compile(r"(?i)(?:密码|口令|支付密码|password|passwd|pwd)\s*[:：=]\s*\S{4,}")),
)
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_NUMBER = re.compile(r"(?<![0-9A-Za-z])([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9A-Za-z])")
_CARD_GROUPED = re.compile(r"(?<![\d-])(\d{4}(?:[ -]\d{4}){3}(?:[ -]?\d{1,3})?)(?![\d-])")
_CARD_PLAIN = re.compile(r"(?<!\d)(\d{15,19})(?!\d)")
_CARD_CONTEXT = re.compile(r"(?i)银行卡|信用卡|储蓄卡|借记卡|卡号|card\s*(?:no|number|#)|credit\s*card")


def _valid_id(number):
    total = sum(int(digit) * weight for digit, weight in zip(number[:17], _ID_WEIGHTS))
    return "10X98765432"[total % 11] == number[17].upper()


def _luhn(digits):
    total = 0
    for index, digit in enumerate(reversed(digits)):
        value = int(digit)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def sensitive_text_reason(text):
    """Name of a likely secret in copied text, or "" when nothing matched.

    Deliberately narrow: an 18-digit ID must pass its checksum, and a card
    number must pass Luhn and either be written in groups of four or appear
    next to a card keyword, so ordinary order and tracking numbers still save.
    """
    if not isinstance(text, str) or not text:
        return ""
    sample = text[:200_000]
    for reason, pattern in _SECRET_PATTERNS:
        if pattern.search(sample):
            from .i18n import tr
            return tr(reason)
    if any(_valid_id(match) for match in _ID_NUMBER.findall(sample)):
        from .i18n import tr
        return tr("身份证号")
    for match in _CARD_GROUPED.finditer(sample):
        digits = re.sub(r"\D", "", match.group(1))
        if 15 <= len(digits) <= 19 and _luhn(digits):
            from .i18n import tr
            return tr("银行卡号")
    for match in _CARD_PLAIN.finditer(sample):
        nearby = sample[max(0, match.start() - 24):match.end() + 8]
        if _luhn(match.group(1)) and _CARD_CONTEXT.search(nearby):
            from .i18n import tr
            return tr("银行卡号")
    return ""
