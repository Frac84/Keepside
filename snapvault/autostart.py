"""Optional start with Windows through the current user's Run key.

Only the packaged program registers itself; it starts hidden in the tray.
Nothing is changed unless the user turns the option on in preferences.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Keep the original registry value as the upgrade-compatible identity.
# Changing the product's display name must not create a second startup entry.
VALUE_NAME = "拾影"


def supported():
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def command(executable=None):
    executable = Path(executable or sys.executable).resolve()
    return f'"{executable}" --minimized'


def _read():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except FileNotFoundError:
        return None


def _write(value):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if value is None:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)


def enabled():
    """Recognize this program and its co-located old/new release alias."""
    if not supported():
        return False
    try:
        registered = _read()
        if registered == command():
            return True
        executable = Path(sys.executable).resolve()
        aliases = ("Keepside.exe", "捕风捉影.exe", "拾影.exe")
        if not getattr(sys, "frozen", False) or executable.name.casefold() not in {
                name.casefold() for name in aliases}:
            return False
        # The installer ships identical binaries under these two names in one
        # directory. Do not accept an old entry from a different installation.
        return any(registered == command(executable.with_name(name)) for name in aliases)
    except OSError:
        return False


def set_enabled(value):
    """Add or remove the entry; returns the resulting state."""
    if not supported():
        return False
    _write(command() if value else None)
    return enabled()
