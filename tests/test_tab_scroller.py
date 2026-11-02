"""Engine ownership and IPC tests. Never register input or operate a browser."""
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QProcess as RealProcess
from PySide6.QtWidgets import QApplication
import pytest
from snapvault import tab_scroller as tabs


class Signal:
    def __init__(self):
        self.slots = []
    def connect(self, slot):
        self.slots.append(slot)
    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class FakeProcess:
    ProcessState = RealProcess.ProcessState
    def __init__(self, parent):
        self.started, self.finished, self.errorOccurred = Signal(), Signal(), Signal()
        self.current = self.ProcessState.NotRunning
        self.starts = self.kills = self.waits = 0
        self.timeout = False
        self.stuck = self.fail_start = False
    def state(self):
        return self.current
    def processId(self):
        return 1234
    def setProgram(self, value):
        self.program = value
    def setArguments(self, value):
        self.arguments = value
    def start(self):
        self.starts += 1
        if self.fail_start:
            self.errorOccurred.emit(RealProcess.ProcessError.FailedToStart)
            return
        self.current = self.ProcessState.Running
        self.started.emit()
    def waitForFinished(self, timeout):
        self.waits += 1
        if self.stuck:
            return False
        if self.timeout and not self.kills:
            return False
        self.current = self.ProcessState.NotRunning
        self.finished.emit(0, RealProcess.ExitStatus.NormalExit)
        return True
    def kill(self):
        self.kills += 1
    def errorString(self):
        return "Synthetic engine error"


class FakeAPI:
    def __init__(self):
        self.guard = False
        self.ready = True
        self.reject_guard = self.reject_options = False
        self.options, self.exits = [], []
    def acquire_guard(self):
        if self.reject_guard:
            raise OSError("Old instance is running")
        self.guard = True
    def release_guard(self):
        self.guard = False
    def find_window(self, pid):
        assert pid == 1234
        return 5678 if self.ready else 0
    def set_options(self, hwnd, mask):
        assert hwnd == 5678
        if self.reject_options:
            raise OSError("No acknowledgement")
        self.options.append(mask)
    def request_exit(self, hwnd):
        self.exits.append(hwnd)


@pytest.fixture
def service(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    api = FakeAPI()
    executable = tmp_path / "engine.exe"
    executable.touch()
    monkeypatch.setattr(tabs, "QProcess", FakeProcess)
    monkeypatch.setattr(tabs, "_HostAPI", lambda: api)
    monkeypatch.setattr(tabs, "engine_executable", lambda: executable)
    instance = tabs.TabScroller(app, 999)
    yield instance, api, app
    instance.stop()
    app.processEvents()


@pytest.mark.parametrize("mode", [0, 1, 2])
@pytest.mark.parametrize("delay", tabs.FOCUS_DELAYS)
def test_all_original_focus_settings_encode(mode, delay):
    mask = tabs.encode_options(focus_mode=mode, focus_delay=delay)
    assert mask & 15 == 13
    assert (mask >> 4) & 3 == mode
    assert mask >> 8 == delay


@pytest.mark.parametrize("settings", [{"focus_mode": 3}, {"focus_delay": 6}])
def test_invalid_focus_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        tabs.encode_options(**settings)


def test_repeated_configuration_keeps_one_engine(service):
    scroller, api, _ = service
    assert scroller.configure()
    assert scroller.active and api.guard
    for _ in range(5):
        assert scroller.configure(reverse=True, focus_mode=1, focus_delay=50)
    assert scroller._process.starts == 1
    assert api.options[-1] == tabs.encode_options(reverse=True, focus_mode=1, focus_delay=50)
    assert scroller._process.arguments[:2] == ["--embedded-parent", str(tabs.os.getpid())]


def test_slow_start_does_not_spawn_duplicate_engine(service):
    scroller, api, _ = service
    api.ready = False
    assert scroller.configure()
    assert scroller.starting and not scroller.active
    assert scroller.configure(reverse=True)
    assert scroller._process.starts == 1
    api.ready = True
    scroller._check_ready()
    assert scroller.active
    assert api.options[-1] == tabs.encode_options(reverse=True)


def test_pause_exit_and_restart_are_owned_and_deterministic(service):
    scroller, api, _ = service
    scroller.configure()
    scroller.configure(enabled=False)
    assert not scroller.active and not api.guard
    assert api.exits == [5678]
    assert scroller._process.kills == 0
    scroller.configure()
    assert scroller.active and scroller._process.starts == 2


def test_shutdown_timeout_only_kills_our_child(service):
    scroller, api, _ = service
    scroller.configure()
    scroller._process.timeout = True
    scroller.stop()
    assert scroller._process.kills == 1
    assert not api.guard and not scroller.active


def test_failed_start_never_leaves_a_polling_timer(service):
    scroller, api, _ = service
    scroller._process.fail_start = True
    assert not scroller.configure()
    assert not scroller.active and not api.guard
    assert not scroller._timer.isActive()


def test_unfinished_shutdown_keeps_guard_and_blocks_restart(service):
    scroller, api, _ = service
    scroller.configure()
    scroller._process.stuck = True
    assert not scroller.stop()
    assert api.guard and scroller.error
    assert not scroller.configure()
    assert scroller._process.starts == 1 and api.guard
    scroller._process.stuck = False
    scroller.stop()


def test_guard_blocks_previous_python_instance(service):
    scroller, api, _ = service
    api.reject_guard = True
    assert not scroller.configure()
    assert scroller._process.starts == 0
    assert not scroller.active and scroller.error


def test_original_single_instance_conflict_is_visible(service):
    scroller, api, app = service
    errors = []
    scroller._error_callback = errors.append
    scroller.configure()
    scroller._process.current = RealProcess.ProcessState.NotRunning
    scroller._process.finished.emit(23, RealProcess.ExitStatus.NormalExit)
    app.processEvents()
    assert not scroller.active and not api.guard
    assert "独立滚轮工具" in scroller.error
    assert errors == [scroller.error]


def test_configuration_without_ack_stops_the_engine(service):
    scroller, api, _ = service
    scroller.configure()
    api.reject_options = True
    assert not scroller.configure(reverse=True)
    assert not scroller.active and not api.guard


def test_diagnostic_engine_never_enables_mouse_actions(service):
    scroller, api, _ = service
    scroller._diagnostic = True
    scroller.configure()
    assert scroller.engine_ready and not scroller.active
    assert all(mask & 1 == 0 for mask in api.options)
    assert int(scroller._process.arguments[-1]) & 1 == 0


def test_python_has_no_second_input_implementation():
    source = Path(tabs.__file__).read_text("utf-8")
    for forbidden in ("RegisterRawInputDevices", "GetRawInputData", "nativeEventFilter", "SendInput"):
        assert forbidden not in source


def test_all_original_autoit_functions_are_preserved():
    from browser_engine.audit_core import audit
    root = Path(tabs.__file__).resolve().parents[1] / "browser_engine"
    result = audit(root / "upstream/chrome-mouse-wheel-tab-scroller.au3", root / "SnapVaultBrowserEngine.au3")
    assert result["original_functions_verified"] == 48
    assert result["core_behavior"] == "unchanged"


def test_unexpected_exit_restarts_once_then_stays_stopped(service, monkeypatch):
    scroller, api, app = service
    monkeypatch.setattr(tabs, "RESTART_DELAY_MS", 0)
    scroller._restart_timer.setInterval(0)
    scroller.configure(reverse=True)
    assert scroller.active and scroller._process.starts == 1

    def crash(code=7):
        scroller._process.current = RealProcess.ProcessState.NotRunning
        scroller._process.finished.emit(code, RealProcess.ExitStatus.CrashExit)

    crash()
    assert not scroller.active and not api.guard and "自动重启" in scroller.error
    scroller._restart_timer.timeout.emit()
    assert scroller.active and scroller._process.starts == 2 and api.guard
    # Options of the restarted engine match the last configuration.
    assert api.options[-1] == tabs.encode_options(reverse=True)
    crash()
    assert not scroller._restart_timer.isActive()
    assert not scroller.active and "自动重启" not in scroller.error
    assert scroller._process.starts == 2


def test_user_pause_cancels_a_pending_restart(service):
    scroller, api, _ = service
    scroller.configure()
    scroller._process.current = RealProcess.ProcessState.NotRunning
    scroller._process.finished.emit(7, RealProcess.ExitStatus.CrashExit)
    assert scroller._restart_timer.isActive()
    scroller.configure(enabled=False)
    assert not scroller._restart_timer.isActive()
    scroller._auto_restart()
    assert scroller._process.starts == 1 and not scroller.active
