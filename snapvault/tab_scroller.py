"""Manage one original AutoIt engine. Python never handles or sends input."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import sys
from time import monotonic
from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .i18n import tr

OPTIONS_MESSAGE, EXIT_MESSAGE, IPC_MAGIC = 0x802A, 0x802B, 0x53565442
ENGINE_WINDOW_TITLE = "Chrome Mouse Wheel Tab Scroller Raw Input Handler Window"
READY_PROPERTY, OPTIONS_PROPERTY = "SnapVault.EngineReady", "SnapVault.EngineOptions"
FOCUS_DELAYS = (3, 4, 5, 8, 11, 15, 20, 25, 30, 35, 40, 50, 75, 100)
# After an unexpected engine exit, start it again once; a second exit within
# this window stays stopped and visible so a crash loop never repeats input.
RESTART_DELAY_MS, RESTART_WINDOW_SECONDS = 1500, 600


def encode_options(enabled=True, reverse=False, double_click=True, autofocus=True,
                   focus_mode=2, focus_delay=25):
    if focus_mode not in (0, 1, 2) or focus_delay not in FOCUS_DELAYS:
        raise ValueError(tr('浏览器焦点模式或等待时间无效'))
    return (int(bool(enabled)) | int(bool(reverse)) << 1 |
            int(bool(double_click)) << 2 | int(bool(autofocus)) << 3 |
            int(focus_mode) << 4 | int(focus_delay) << 8)


def engine_executable():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "browser_engine" / "SnapVaultBrowserEngine.exe"
    return Path(__file__).resolve().parents[1] / "browser_engine/bin/SnapVaultBrowserEngine.exe"


class _HostAPI:
    """Private IPC and a process guard only; deliberately no input APIs."""
    def __init__(self):
        if sys.platform != "win32":
            raise OSError(tr('浏览器标签工具仅支持 Windows。'))
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
        signatures = {
            "EnumWindows": ([self._enum_proc, ctypes.c_ssize_t], ctypes.c_int),
            "GetWindowThreadProcessId": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_uint32),
            "GetWindowTextW": ([ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
            "GetPropW": ([ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
            "SendMessageTimeoutW": ([ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t,
                ctypes.c_ssize_t, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t),
            "PostMessageW": ([ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = arguments, result
        self.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self.kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel32.CloseHandle.restype = ctypes.c_int
        self._guard = None

    def acquire_guard(self):
        if self._guard:
            return
        ctypes.set_last_error(0)
        handle = self.kernel32.CreateMutexW(None, False, "Local\\SnapVault.TabScroller")
        if not handle:
            raise OSError(tr('无法创建浏览器工具的单实例锁。'))
        if ctypes.get_last_error() == 183:
            self.kernel32.CloseHandle(handle)
            raise OSError(tr('另一份整合版正在处理标签操作，请先退出它，再开启新版。'))
        self._guard = handle

    def release_guard(self):
        if self._guard:
            self.kernel32.CloseHandle(self._guard)
            self._guard = None

    def find_window(self, process_id):
        found = []
        def inspect(hwnd, _):
            pid = ctypes.c_uint32()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == process_id:
                title = ctypes.create_unicode_buffer(256)
                self.user32.GetWindowTextW(hwnd, title, len(title))
                if title.value == ENGINE_WINDOW_TITLE and self.property(hwnd, READY_PROPERTY) == 1:
                    found.append(int(hwnd))
                    return False
            return True
        callback = self._enum_proc(inspect)
        self.user32.EnumWindows(callback, 0)
        return found[0] if found else 0

    def property(self, hwnd, name):
        return int(self.user32.GetPropW(hwnd, name) or 0)

    def set_options(self, hwnd, mask):
        reply = ctypes.c_size_t()
        sent = self.user32.SendMessageTimeoutW(hwnd, OPTIONS_MESSAGE, mask, IPC_MAGIC,
                                              0x0002, 2000, ctypes.byref(reply))
        if not sent or self.property(hwnd, OPTIONS_PROPERTY) != mask + 1:
            raise OSError(tr('原版标签引擎未确认配置，已停止，避免重复或错误切换。'))

    def request_exit(self, hwnd):
        self.user32.PostMessageW(hwnd, EXIT_MESSAGE, 0, IPC_MAGIC)


class TabScroller(QObject):
    state_changed = Signal()

    def __init__(self, app, hwnd, error_callback=None, *, diagnostic=False):
        super().__init__()
        self._error_callback, self._diagnostic = error_callback, diagnostic
        self._enabled = self._stopping = False
        self._error, self._api, self._window = None, None, 0
        self._mask, self._deadline = 0, 0
        self._process = QProcess(self)
        self._process.started.connect(self._started)
        self._process.finished.connect(self._finished)
        self._process.errorOccurred.connect(self._process_error)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._check_ready)
        self._settings = {}
        self._last_restart = None
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.setInterval(RESTART_DELAY_MS)
        self._restart_timer.timeout.connect(self._auto_restart)

    @property
    def engine_ready(self):
        return bool(self._window and self._process.state() == QProcess.ProcessState.Running)

    @property
    def active(self):
        return self._enabled and self.engine_ready and not self._diagnostic

    @property
    def starting(self):
        return self._enabled and not self._window and self._process.state() != QProcess.ProcessState.NotRunning

    @property
    def error(self):
        return self._error

    @property
    def engine_pid(self):
        return int(self._process.processId())

    def configure(self, enabled=True, reverse=False, double_click=True, autofocus=True,
                  focus_mode=2, focus_delay=25):
        self._error = None
        self._restart_timer.stop()
        try:
            self._mask = encode_options(enabled and not self._diagnostic, reverse,
                double_click, autofocus, focus_mode, focus_delay)
            self._settings = dict(enabled=enabled, reverse=reverse, double_click=double_click,
                                  autofocus=autofocus, focus_mode=focus_mode, focus_delay=focus_delay)
            if not enabled:
                return self.stop()
            self._enabled = True
            if self._process.state() != QProcess.ProcessState.NotRunning:
                if self._stopping:
                    raise OSError(tr('上一份标签引擎尚未退出，已阻止重复启动。'))
                if self._window:
                    self._apply_options()
                return True
            executable = engine_executable()
            if not executable.is_file():
                raise OSError(tr('原版标签引擎缺失，请保留完整的新版程序文件夹。'))
            if self._api is None:
                self._api = _HostAPI()
            self._api.acquire_guard()
            self._stopping, self._window = False, 0
            self._deadline = monotonic() + 10
            self._process.setProgram(str(executable))
            self._process.setArguments(["--embedded-parent", str(os.getpid()),
                                        "--embedded-options", str(self._mask)])
            self._process.start()
            if not self._enabled or self._error:
                return False
            self._timer.start()
            self.state_changed.emit()
            return True
        except Exception as exc:
            self._fail(str(exc))
            return False

    def _started(self):
        self._check_ready()

    def _check_ready(self):
        if not self._enabled or self._process.state() != QProcess.ProcessState.Running:
            return
        try:
            self._window = self._api.find_window(self.engine_pid)
            if self._window:
                self._apply_options()
                self._timer.stop()
                self.state_changed.emit()
            elif monotonic() > self._deadline:
                self._fail(tr('原版标签引擎启动超时。'))
        except Exception as exc:
            self._fail(str(exc))

    def _apply_options(self):
        self._api.set_options(self._window, self._mask)

    def _process_error(self, error):
        if not self._stopping and self._enabled:
            self._fail(tr('原版标签引擎运行失败：') + self._process.errorString())

    def _finished(self, code, status):
        expected = self._stopping or not self._enabled
        self._timer.stop()
        self._window = 0
        if self._api:
            self._api.release_guard()
        self.state_changed.emit()
        if not expected:
            message = {
                23: tr('独立滚轮工具已经在运行。请退出它，再开启新版的标签操作。'),
                24: tr('原版标签引擎未能注册鼠标监听或控制接口。'),
            }.get(code, tr('原版标签引擎已退出（代码 {p0}）。', p0=code))
            self._enabled = False
            if code != 23 and not self._diagnostic and self._may_restart():
                self._restart_timer.start()
                message += tr('正在自动重启一次。')
            self._record_error(message)

    def _may_restart(self):
        return self._last_restart is None or monotonic() - self._last_restart >= RESTART_WINDOW_SECONDS

    def _auto_restart(self):
        if self._enabled or self._stopping or self._process.state() != QProcess.ProcessState.NotRunning:
            return
        self._last_restart = monotonic()
        self.configure(**self._settings)

    def _record_error(self, message):
        self._error = message
        self.state_changed.emit()
        if self._error_callback:
            QTimer.singleShot(0, lambda: self._error_callback(message))

    def _fail(self, message):
        self.stop()
        self._record_error(message)

    def stop(self):
        self._enabled, self._stopping = False, True
        self._timer.stop()
        self._restart_timer.stop()
        if self._process.state() != QProcess.ProcessState.NotRunning:
            if self._window and self._api:
                self._api.request_exit(self._window)
            # Only the child owned by this QProcess can be killed on timeout.
            if not self._process.waitForFinished(2000):
                self._process.kill()
                self._process.waitForFinished(1000)
            if self._process.state() != QProcess.ProcessState.NotRunning:
                self._record_error(tr('标签引擎尚未完成退出，单实例锁仍保留，已阻止重复启动。'))
                return False
        self._window = 0
        if self._api:
            self._api.release_guard()
        self.state_changed.emit()
        return True
