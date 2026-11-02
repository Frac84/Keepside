"""Exercise hidden-window IPC with wheel/click handling disabled throughout."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "bin" / "SnapVaultBrowserEngine.exe"
TITLE = "Chrome Mouse Wheel Tab Scroller Raw Input Handler Window"
MAGIC = 0x53565442
DISABLED_OPTIONS = 0x192C
user32 = ctypes.WinDLL("user32", use_last_error=True)
CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [CALLBACK, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.GetPropW.restype = wintypes.HANDLE
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM


def find_ready_window(pid: int) -> int | None:
    found = []

    @CALLBACK
    def inspect(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value == TITLE and user32.GetPropW(hwnd, "SnapVault.EngineReady") == 1:
                found.append(hwnd)
        return True

    user32.EnumWindows(inspect, 0)
    return found[0] if found else None


def launch(parent_pid: int):
    process = subprocess.Popen(
        [str(ENGINE), "--embedded-parent", str(parent_pid), "--embedded-options", str(DISABLED_OPTIONS)],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Engine exited before readiness: {process.returncode} (23 means original engine already running)")
            hwnd = find_ready_window(process.pid)
            if hwnd:
                assert user32.GetPropW(hwnd, "SnapVault.EngineOptions") == DISABLED_OPTIONS + 1
                return process, hwnd
            time.sleep(0.025)
        raise TimeoutError("Engine did not announce readiness")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        raise


def send(hwnd: int, message: int, mask: int = 0):
    result = ctypes.c_size_t()
    if not user32.SendMessageTimeoutW(hwnd, message, mask, MAGIC, 3, 2000, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())


def run():
    process, hwnd = launch(os.getpid())
    try:
        duplicate = subprocess.Popen(
            [str(ENGINE), "--embedded-parent", str(os.getpid()), "--embedded-options", str(DISABLED_OPTIONS)],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            assert duplicate.wait(timeout=3) == 23, "Duplicate engine must exit without starting another input handler"
        finally:
            if duplicate.poll() is None:
                duplicate.terminate()
                duplicate.wait(timeout=3)
        assert process.poll() is None, "The owning engine must remain running after collision"
        send(hwnd, 0x802A, 0x6402)  # disabled; reverse; no double click/autofocus; keep; 100 ms
        assert user32.GetPropW(hwnd, "SnapVault.EngineOptions") == 0x6402 + 1
        send(hwnd, 0x802A, 0x6532)  # invalid mode/delay preserve the last valid values
        assert user32.GetPropW(hwnd, "SnapVault.EngineOptions") == 0x6402 + 1
        send(hwnd, 0x802B)
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)

    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], creationflags=subprocess.CREATE_NO_WINDOW)
    orphan = None
    try:
        orphan, _ = launch(parent.pid)
        parent.terminate()
        parent.wait(timeout=3)
        assert orphan.wait(timeout=3) == 0
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=3)
        if orphan is not None and orphan.poll() is None:
            orphan.terminate()
            orphan.wait(timeout=3)
    print("PASS: readiness, duplicate Exit23, mask acknowledgement, invalid-field normalization, graceful exit, parent-death exit")


if __name__ == "__main__":
    run()

