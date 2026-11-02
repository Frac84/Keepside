"""Lightweight OCR client lifecycle; fake jobs never load a native model."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
import threading
from types import ModuleType

import pytest

from snapvault import ocr


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "input.png"
    path.write_bytes(b"synthetic job input")
    return path


class FakeProcess:
    def __init__(self, job, result=None, code=0, running=False, stubborn=False):
        self.job = job
        self.returncode = None if running else code
        self.terminated = self.killed = 0
        self.stubborn = stubborn
        if result is not None:
            Path(job["output"]).write_text(json.dumps(result), encoding="utf-8")

    def poll(self):
        return self.returncode

    def terminate(self):
        assert not Path(self.job["lifetime"]).exists(), "Cancel the real worker before its launcher"
        self.terminated += 1
        if not self.stubborn:
            self.returncode = 2

    def kill(self):
        self.killed += 1
        self.returncode = 2

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def install_process(monkeypatch, **kwargs):
    calls = []

    def launch(command, **options):
        job_path = Path(command[-1])
        job = json.loads(job_path.read_text(encoding="utf-8"))
        assert Path(job["lifetime"]).is_file()
        process = FakeProcess(job, **kwargs)
        calls.append((command, options, process, job_path.parent))
        return process

    monkeypatch.setattr(ocr.subprocess, "Popen", launch)
    return calls


def test_source_job_is_private_and_result_is_cleaned(monkeypatch, image):
    calls = install_process(monkeypatch, result={"ok": True, "text": "中 文\nEnglish words", "extra": "ignored"})
    assert ocr.recognize(image, "zh-CN") == "中文\nEnglish words"
    command, options, _, directory = calls[0]
    assert command[0] == sys.executable and Path(command[1]).name == "main.py"
    assert command[-2] == "--ocr-job"
    assert options["stdout"] == subprocess.DEVNULL and options["stderr"] == subprocess.DEVNULL
    assert not directory.exists()


def test_frozen_job_reuses_executable_without_main(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert ocr._worker_command(tmp_path / "job.json") == [sys.executable, "--ocr-job", str(tmp_path / "job.json")]


@pytest.mark.parametrize("result", [{"ok": False, "error": "model failed"}, {"ok": True, "text": None}, ["bad schema"]])
def test_failed_or_malformed_job_never_returns_text(monkeypatch, image, result):
    calls = install_process(monkeypatch, result=result)
    with pytest.raises(RuntimeError):
        ocr.recognize(image)
    assert not calls[0][3].exists()


def test_native_crash_without_json_is_reported(monkeypatch, image):
    install_process(monkeypatch, code=17)
    with pytest.raises(RuntimeError, match="17"):
        ocr.recognize(image)


def test_cancel_marker_precedes_launcher_stop(monkeypatch, image):
    event = threading.Event()
    calls = install_process(monkeypatch, running=True)
    original = ocr.subprocess.Popen

    def cancel_on_launch(*args, **kwargs):
        result = original(*args, **kwargs)
        event.set()
        return result

    monkeypatch.setattr(ocr.subprocess, "Popen", cancel_on_launch)
    with pytest.raises(ocr.OcrCancelled):
        ocr.recognize(image, stop_event=event)
    assert calls[0][2].terminated == 1 and not calls[0][3].exists()


def test_timeout_stops_stubborn_owned_job(monkeypatch, image):
    calls = install_process(monkeypatch, running=True, stubborn=True)
    ticks = iter((0.0, 10.0))
    monkeypatch.setattr(ocr.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        ocr.recognize(image, timeout=1)
    assert calls[0][2].terminated == 1 and calls[0][2].killed == 1
    assert not calls[0][3].exists()


def test_pre_cancelled_job_never_starts(monkeypatch, image):
    calls = install_process(monkeypatch)
    event = threading.Event()
    event.set()
    with pytest.raises(ocr.OcrCancelled):
        ocr.recognize(image, stop_event=event)
    assert not calls


def test_main_dispatches_worker_before_any_qt_import(monkeypatch):
    runtime = ModuleType("snapvault.ocr_runtime")
    seen = []
    runtime.worker_main = lambda argv: seen.append(argv) or 0
    monkeypatch.setitem(sys.modules, "snapvault.ocr_runtime", runtime)
    monkeypatch.setattr(sys, "argv", ["main.py", "--ocr-job", "private.json"])
    import builtins
    original = builtins.__import__

    def reject_ui(name, *args, **kwargs):
        assert not name.startswith(("PySide6", "snapvault.ui", "snapvault.branding"))
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_ui)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(ocr.__file__).resolve().parents[1] / "main.py"), run_name="__main__")
    assert result.value.code == 0 and seen == [["--ocr-job", "private.json"]]


def test_availability_is_a_lightweight_model_check(monkeypatch):
    from snapvault import ocr_runtime
    seen = []
    monkeypatch.setattr(ocr_runtime, "validate_models", lambda: seen.append(True))
    assert ocr.check_availability() is None and seen == [True]
    assert ocr.languages() == [("auto", "自动识别（中英文混合）")]
