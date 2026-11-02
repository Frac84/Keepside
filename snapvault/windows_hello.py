"""Windows Hello consent for the recent-files UI, using desktop WinRT interop.

No credentials, biometric templates or verification results are persisted here.
This is an application access check, not encryption or a key-storage facility.
``verify`` must be called on a worker thread, only after an explicit user action.
``availability`` never requests verification or opens an authentication dialog.

ABI sources:
https://learn.microsoft.com/windows/win32/api/userconsentverifierinterop/
https://github.com/microsoft/windows-rs/blob/master/crates/libs/windows/src/Windows/Security/Credentials/UI/mod.rs
https://github.com/microsoft/windows-rs/blob/master/crates/libs/future/src/bindings.rs
https://learn.microsoft.com/uwp/winrt-cref/winrt-type-system
"""
from __future__ import annotations
from .i18n import tr
from .branding import app_name

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import time
import uuid


AVAILABILITY_TIMEOUT = 2.0
VERIFICATION_TIMEOUT = 120.0
POLL_INTERVAL = 0.05
_CLASS = "Windows.Security.Credentials.UI.UserConsentVerifier"
_HRESULT = ctypes.c_int32
_CALLTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_RPC_E_CHANGED_MODE = 0x80010106


@dataclass(frozen=True)
class Availability:
    usable: bool
    status: str
    message: str

    @property
    def reason(self):
        return self.message

    def __bool__(self):
        return self.usable


@dataclass(frozen=True)
class Verification:
    success: bool
    status: str
    message: str

    def __bool__(self):
        return self.success


_AVAILABILITY = {
    0: ("available", "可使用 Windows Hello 解锁；验证窗口中可选择已设置的 PIN 或生物识别。"),
    1: ("device_not_present", "此电脑暂时没有可用的 Windows Hello 验证方式。"),
    2: ("not_configured", "请先在 Windows 登录选项中设置 Windows Hello PIN。"),
    3: ("disabled_by_policy", "Windows Hello 已被系统策略禁用。"),
    4: ("device_busy", "Windows Hello 正忙，请稍后重试。"),
}
_VERIFICATION = {
    0: ("verified", "已通过 Windows Hello 验证。"),
    1: ("device_not_present", "Windows Hello 验证方式不可用，最近文件仍保持锁定。"),
    2: ("not_configured", "尚未设置 Windows Hello PIN，最近文件仍保持锁定。"),
    3: ("disabled_by_policy", "Windows Hello 已被系统策略禁用，最近文件仍保持锁定。"),
    4: ("device_busy", "Windows Hello 正忙，请稍后重试。"),
    5: ("retries_exhausted", "验证尝试次数已用完，请稍后重试；最近文件仍保持锁定。"),
    6: ("cancelled", "已取消验证，最近文件仍保持锁定。"),
}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


def _operation_iid(enum_name):
    """The documented WinRT UUID-v5 signature for IAsyncOperation<enum>."""
    signature = ("pinterface({9fc2b0bb-e446-44e2-aa61-9cab8f636af2};"
                 f"enum(Windows.Security.Credentials.UI.{enum_name};i4))")
    return _GUID.parse(str(uuid.uuid5(uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee"), signature)))


_IID_STATICS = _GUID.parse("af4f3f91-564c-4ddc-b8b5-973447627c65")
_IID_INTEROP = _GUID.parse("39e050c3-4e74-441a-8dc0-b81104df949c")
_IID_ASYNC_INFO = _GUID.parse("00000036-0000-0000-c000-000000000046")
_IID_VERIFY_OPERATION = _operation_iid("UserConsentVerificationResult")


def _supported():
    return os.name == "nt"


class _NativeError(Exception):
    def __init__(self, hresult):
        self.hresult = int(hresult) & 0xffffffff
        super().__init__(f"Windows Hello 0x{self.hresult:08X}")


class _Cancelled(Exception):
    pass


class _TimedOut(Exception):
    pass


def _check(hr):
    # HRESULT is always a signed 32-bit value, including on 64-bit Python.
    if ctypes.c_int32(int(hr)).value < 0:
        raise _NativeError(hr)


def _method(pointer, slot, result, *arguments):
    if not pointer or not pointer.value:
        raise _NativeError(0x80004003)  # E_POINTER
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return _CALLTYPE(result, ctypes.c_void_p, *arguments)(table[slot])


class _ComPtr:
    def __init__(self, pointer):
        self.pointer = pointer

    def call(self, slot, *signature):
        return _method(self.pointer, slot, _HRESULT, *signature)

    def release(self):
        if self.pointer.value:
            _method(self.pointer, 2, ctypes.c_uint32)(self.pointer)
            self.pointer.value = None


class _Operation:
    """One enum-valued WinRT operation; all methods stay on its calling thread."""
    def __init__(self, runtime, pointer):
        self.operation = runtime.own(pointer)
        self.info = runtime.query(self.operation, _IID_ASYNC_INFO)
        self.last_status = 0

    def status(self):
        status = ctypes.c_int32()
        _check(self.info.call(7, ctypes.POINTER(ctypes.c_int32))(
            self.info.pointer, ctypes.byref(status)))
        self.last_status = status.value
        return status.value

    def result(self):
        result = ctypes.c_int32()
        _check(self.operation.call(8, ctypes.POINTER(ctypes.c_int32))(
            self.operation.pointer, ctypes.byref(result)))
        return result.value

    def error(self):
        error = _HRESULT()
        _check(self.info.call(8, ctypes.POINTER(_HRESULT))(self.info.pointer, ctypes.byref(error)))
        return error.value or 0x80004005

    def cancel(self):
        self.info.call(9)(self.info.pointer)

    def close(self):
        # Close is valid only once the async operation is terminal. Release
        # still runs for operations whose cancellation is handled asynchronously.
        if self.last_status in (1, 2, 3):
            self.info.call(10)(self.info.pointer)


class _Runtime:
    def __init__(self):
        self.references = []
        self.strings = []
        self.initialized = False
        self.combase = None

    def __enter__(self):
        try:
            self.combase = ctypes.WinDLL("combase", use_last_error=True)
            self.combase.RoInitialize.argtypes = [ctypes.c_uint32]
            self.combase.RoInitialize.restype = _HRESULT
            self.combase.RoUninitialize.argtypes = []
            self.combase.RoUninitialize.restype = None
            self.combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, ctypes.c_uint32,
                                                       ctypes.POINTER(ctypes.c_void_p)]
            self.combase.WindowsCreateString.restype = _HRESULT
            self.combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
            self.combase.WindowsDeleteString.restype = _HRESULT
            self.combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GUID),
                                                         ctypes.POINTER(ctypes.c_void_p)]
            self.combase.RoGetActivationFactory.restype = _HRESULT
            hr = self.combase.RoInitialize(1)  # RO_INIT_MULTITHREADED, for the worker.
            if (int(hr) & 0xffffffff) != _RPC_E_CHANGED_MODE:
                _check(hr)
                self.initialized = True  # S_OK and S_FALSE must both be balanced.
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        for reference in reversed(self.references):
            reference.release()
        self.references.clear()
        if self.combase is not None:
            for value in reversed(self.strings):
                self.combase.WindowsDeleteString(value)
            self.strings.clear()
            if self.initialized:
                self.combase.RoUninitialize()
                self.initialized = False

    def string(self, value):
        pointer = ctypes.c_void_p()
        units = len(value.encode("utf-16-le")) // 2
        _check(self.combase.WindowsCreateString(value, units, ctypes.byref(pointer)))
        self.strings.append(pointer)
        return pointer

    def own(self, pointer):
        if not pointer.value:
            raise _NativeError(0x80004003)
        value = _ComPtr(pointer)
        self.references.append(value)
        return value

    def query(self, reference, iid):
        pointer = ctypes.c_void_p()
        _check(reference.call(0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            reference.pointer, ctypes.byref(iid), ctypes.byref(pointer)))
        return self.own(pointer)

    def factory(self, iid):
        pointer = ctypes.c_void_p()
        _check(self.combase.RoGetActivationFactory(self.string(_CLASS), ctypes.byref(iid),
                                                 ctypes.byref(pointer)))
        return self.own(pointer)

    def availability_operation(self):
        factory = self.factory(_IID_STATICS)
        operation = ctypes.c_void_p()
        _check(factory.call(6, ctypes.POINTER(ctypes.c_void_p))(
            factory.pointer, ctypes.byref(operation)))
        return _Operation(self, operation)

    def verification_operation(self, hwnd, message):
        factory = self.factory(_IID_INTEROP)
        operation = ctypes.c_void_p()
        _check(factory.call(6, wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(_GUID),
                            ctypes.POINTER(ctypes.c_void_p))(
            factory.pointer, hwnd, self.string(message), ctypes.byref(_IID_VERIFY_OPERATION),
            ctypes.byref(operation)))
        return _Operation(self, operation)


def _wait(operation, timeout, cancel_event=None):
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                operation.cancel()
                raise _Cancelled()
            status = operation.status()
            if status == 1:
                result = operation.result()
                # A late completion must not unlock after an explicit cancel.
                if cancel_event is not None and cancel_event.is_set():
                    raise _Cancelled()
                return result
            if status == 2:
                raise _Cancelled()
            if status == 3:
                raise _NativeError(operation.error())
            if status != 0:
                raise _NativeError(0x8000FFFF)  # Unexpected status, never success.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                operation.cancel()
                raise _TimedOut()
            pause = min(POLL_INTERVAL, remaining)
            if cancel_event is not None:
                cancel_event.wait(pause)
            else:
                time.sleep(pause)
    finally:
        operation.close()


def _valid_owner_window(hwnd):
    if (not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0
            or hwnd >= 1 << (ctypes.sizeof(ctypes.c_void_p) * 8)):
        return False
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    owner = wintypes.DWORD()
    return bool(user.IsWindow(hwnd) and user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                and owner.value == os.getpid())


def availability(timeout=AVAILABILITY_TIMEOUT):
    """Check Windows Hello without displaying a verification or settings dialog."""
    if not _supported():
        return Availability(False, "unsupported", tr('Windows Hello 仅适用于 Windows。'))
    try:
        with _Runtime() as runtime:
            # The desktop verification interface was added in Windows 11.
            # Checking it here prevents enabling a lock that cannot be unlocked
            # on an older Windows version, without displaying any consent UI.
            runtime.factory(_IID_INTEROP)
            value = _wait(runtime.availability_operation(), min(max(float(timeout), 0.1), 3.0))
        status, message = _AVAILABILITY.get(value, ("unknown", tr('无法确认 Windows Hello 是否可用。')))
        return Availability(type(value) is int and value == 0, status, tr(message))
    except _TimedOut:
        return Availability(False, "timeout", tr('检查 Windows Hello 超时，请稍后重试。'))
    except _Cancelled:
        return Availability(False, "cancelled", tr('Windows Hello 可用性检查已取消，请重试。'))
    except (OSError, _NativeError) as exc:
        return Availability(False, "error", tr('Windows Hello 暂不可用（{v0}）。', v0=exc))


_DEFAULT_MESSAGE = object()


def verify(hwnd, message=_DEFAULT_MESSAGE, cancel_event=None):
    """Request an explicit consent check on a worker; only Verified can succeed.

    Windows chooses its configured verification method. This API cannot force
    PIN instead of a camera; the user can choose other sign-in options in the
    system dialog. No code here reads or supplies a password or biometric input.
    """
    if message is _DEFAULT_MESSAGE:
        message = tr('解锁{app}的最近文件', app=app_name())
    if not _supported():
        return Verification(False, "unsupported", tr('Windows Hello 仅适用于 Windows。'))
    if cancel_event is not None and cancel_event.is_set():
        return Verification(False, "cancelled", tr('已取消验证，最近文件仍保持锁定。'))
    try:
        if not _valid_owner_window(hwnd):
            return Verification(False, "invalid_window", tr('解锁窗口已关闭，请重新展开面板后重试。'))
        if not isinstance(message, str) or not message.strip() or "\0" in message or len(message) > 512:
            return Verification(False, "invalid_request", tr('验证请求无效，最近文件仍保持锁定。'))
        with _Runtime() as runtime:
            value = _wait(runtime.verification_operation(hwnd, message), VERIFICATION_TIMEOUT, cancel_event)
        status, text = _VERIFICATION.get(value, ("unknown", tr('未能确认验证结果，最近文件仍保持锁定。')))
        return Verification(type(value) is int and value == 0, status, tr(text))
    except _Cancelled:
        return Verification(False, "cancelled", tr('已取消验证，最近文件仍保持锁定。'))
    except _TimedOut:
        return Verification(False, "timeout", tr('验证超时，最近文件仍保持锁定。'))
    except (OSError, _NativeError) as exc:
        return Verification(False, "error", tr('Windows Hello 验证未完成（{v0}），最近文件仍保持锁定。', v0=exc))
