"""Read Windows Recent Items through the documented Shell interfaces.

This module never executes a shortcut, invokes Resolve, parses Jump Lists, or
starts a helper process. A resolver belongs to one scanning thread and releases
its COM interfaces and apartment on that same thread.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import heapq
import os
import stat
import threading
import time
import uuid


MAX_LINKS = 200
_HRESULT = ctypes.c_long
_CALLTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


FOLDERID_RECENT = GUID.parse("AE50C081-EBD2-438A-8655-8A092E34987A")
CLSID_SHELL_LINK = GUID.parse("00021401-0000-0000-C000-000000000046")
IID_SHELL_LINK_W = GUID.parse("000214F9-0000-0000-C000-000000000046")
IID_PERSIST_FILE = GUID.parse("0000010B-0000-0000-C000-000000000046")


def _method(interface, slot, result_type, *argument_types):
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return _CALLTYPE(result_type, ctypes.c_void_p, *argument_types)(table[slot])


def recent_directory():
    """Return the current user's Known Folder Recent, without creating it."""
    if os.name != "nt":
        return ""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                         wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = _HRESULT
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    result = ctypes.c_void_p()
    # KF_FLAG_DONT_VERIFY: discover the registered location without creating or
    # validating it (a redirected network location will be rejected below).
    hr = shell.SHGetKnownFolderPath(ctypes.byref(FOLDERID_RECENT), 0x4000, None, ctypes.byref(result))
    if hr < 0:
        return ""
    try:
        return local_path(ctypes.wstring_at(result)) if result.value else ""
    finally:
        if result.value:
            ole.CoTaskMemFree(result)


def local_path(value):
    """Reject remote/device/relative targets before touching their filesystem."""
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        return ""
    path = os.path.expandvars(value.strip())
    if path.startswith(("\\\\", "//")) or "://" in path or not os.path.isabs(path):
        return ""
    path = os.path.normpath(path)
    if os.name == "nt":
        drive, _tail = os.path.splitdrive(path)
        if len(drive) != 2 or drive[1] != ":":
            return ""
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetDriveTypeW.restype = wintypes.UINT
        # Mapped network drives are remote too, even though they have a letter.
        if kernel.GetDriveTypeW(drive + "\\") in (0, 1, 4):
            return ""
    return path


def _ordinary_local_file(path):
    try:
        info = os.stat(path, follow_symlinks=False)
        # Avoid following reparse points or recalling cloud/offline content.
        attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISREG(info.st_mode) and not attributes & (0x400 | 0x1000 | 0x40000 | 0x400000)
    except (OSError, ValueError):
        return False


class ShellLinkResolver:
    """Reusable IShellLinkW/IPersistFile; read-only and thread-affine."""

    def __init__(self):
        self._shell = ctypes.c_void_p()
        self._persist = ctypes.c_void_p()
        self._ole = None
        self._thread_id = None
        self._initialized = False

    def __enter__(self):
        if os.name != "nt":
            return self
        self._thread_id = threading.get_ident()
        self._ole = ctypes.WinDLL("ole32", use_last_error=True)
        self._ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self._ole.CoInitializeEx.restype = _HRESULT
        self._ole.CoUninitialize.argtypes = []
        self._ole.CoUninitialize.restype = None
        self._ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self._ole.CoCreateInstance.restype = _HRESULT
        hr = self._ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        if hr < 0:
            raise OSError(f"COM initialization failed: 0x{hr & 0xffffffff:08x}")
        self._initialized = True
        try:
            hr = self._ole.CoCreateInstance(ctypes.byref(CLSID_SHELL_LINK), None, 1,
                                           ctypes.byref(IID_SHELL_LINK_W), ctypes.byref(self._shell))
            if hr < 0:
                raise OSError(f"Shell Link creation failed: 0x{hr & 0xffffffff:08x}")
            hr = _method(self._shell, 0, _HRESULT, ctypes.POINTER(GUID),
                         ctypes.POINTER(ctypes.c_void_p))(self._shell, ctypes.byref(IID_PERSIST_FILE), ctypes.byref(self._persist))
            if hr < 0:
                raise OSError(f"IPersistFile unavailable: 0x{hr & 0xffffffff:08x}")
            return self
        except Exception:
            self.close()
            raise

    def __call__(self, shortcut):
        if not self._persist.value or threading.get_ident() != self._thread_id:
            return ""
        # IPersistFile::Load, STGM_READ | STGM_SHARE_DENY_NONE. Load does not
        # execute a link. GetPath(SLGP_RAWPATH) avoids target resolution/search.
        hr = _method(self._persist, 5, _HRESULT, wintypes.LPCWSTR, wintypes.DWORD)(
            self._persist, os.fspath(shortcut), 0x40)
        if hr < 0:
            return ""
        buffer = ctypes.create_unicode_buffer(32768)
        hr = _method(self._shell, 3, _HRESULT, wintypes.LPWSTR, ctypes.c_int,
                     ctypes.c_void_p, wintypes.DWORD)(self._shell, buffer, len(buffer), None, 4)
        return buffer.value if hr == 0 else ""

    def close(self):
        if self._thread_id is not None and threading.get_ident() != self._thread_id:
            raise RuntimeError("Shell Link interfaces must be released on their owning thread")
        for pointer in (self._persist, self._shell):
            if pointer.value:
                _method(pointer, 2, wintypes.ULONG)(pointer)
                pointer.value = None
        if self._initialized:
            self._ole.CoUninitialize()
            self._initialized = False

    def __exit__(self, *_args):
        self.close()


class RecentItems:
    """Bounded, incremental shortcut metadata cache used by one worker."""

    def __init__(self, resolver=None):
        self._injected = resolver
        self._native = None
        self._cache = {}
        self.metrics = {}

    def scan(self, directory, stopping=None, limit=MAX_LINKS):
        started = time.perf_counter()
        self.metrics = dict(links_considered=0, links_parsed=0, recent_targets=0, recent_scan_ms=0.0)
        stop = stopping or threading.Event()
        directory = local_path(directory)
        if not directory:
            return []
        links = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if stop.is_set():
                        return []
                    if not entry.name.lower().endswith(".lnk") or not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    value = (info.st_mtime_ns, entry.path, info.st_size, info.st_mtime)
                    if len(links) < max(1, min(limit, MAX_LINKS)):
                        heapq.heappush(links, value)
                    elif value[:2] > links[0][:2]:
                        heapq.heapreplace(links, value)
        except OSError:
            return []
        active = {path for _, path, _, _ in links}
        self._cache = {path: value for path, value in self._cache.items() if path in active}
        rows, parsed = {}, 0
        resolve = self._injected
        if resolve is None and links:
            if self._native is None:
                try:
                    self._native = ShellLinkResolver().__enter__()
                except OSError:
                    self._native = None
                    return []
            resolve = self._native
        for stamp_ns, shortcut, size, used_at in sorted(links, reverse=True):
            if stop.is_set():
                break
            signature = (stamp_ns, size)
            cached = self._cache.get(shortcut)
            if cached and cached[0] == signature:
                target = cached[1]
            else:
                parsed += 1
                try:
                    target = local_path(resolve(shortcut))
                except (OSError, ValueError, RuntimeError):
                    target = ""
                self._cache[shortcut] = (signature, target)
            if target and _ordinary_local_file(target):
                key = os.path.normcase(target)
                if key not in rows:
                    rows[key] = dict(path=target, used_at=used_at, recent_mtime_ns=stamp_ns)
        self.metrics = dict(links_considered=len(links), links_parsed=parsed,
                            recent_targets=len(rows), recent_scan_ms=round((time.perf_counter() - started) * 1000, 3))
        return list(rows.values())

    def close(self):
        if self._native is not None:
            self._native.close()
            self._native = None
        self._cache.clear()
