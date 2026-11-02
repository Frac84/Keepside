"""Long-lived OCR session: one model load per burst, idle exit, safe cancellation.

Fake processes stand in for the worker; no native model is loaded.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

from PIL import Image
import pytest

from snapvault import ocr
from snapvault import ocr_runtime as runtime


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "input.png"
    Image.new("RGB", (40, 20), "white").save(path)
    return path


class FakeWorker:
    """Answers request.json like serve_main would, driven by poll()."""

    def __init__(self, command, behaviour):
        self.channel = Path(command[command.index("--channel-dir") + 1])
        self.command = command
        self.behaviour = behaviour
        self.returncode = None
        self.terminated = self.killed = 0
        assert (self.channel / "owner.json").is_file()

    def poll(self):
        request = self.channel / "request.json"
        if self.returncode is None and request.is_file():
            payload = json.loads(request.read_text(encoding="utf-8"))
            action = self.behaviour(payload)
            if action == "idle-exit":
                self.returncode = 0
            elif action == "crash":
                request.unlink()
                self.returncode = 3
            elif action == "hang":
                pass
            else:
                request.unlink()
                (self.channel / "response.json").write_text(json.dumps(dict(action, request_id=payload["request_id"])), encoding="utf-8")
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = 2

    def kill(self):
        self.killed += 1
        self.returncode = 2

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def install(monkeypatch, behaviour):
    launched = []

    def popen(command, **options):
        assert options["stdout"] == subprocess.DEVNULL and options["stderr"] == subprocess.DEVNULL
        worker = FakeWorker(command, behaviour)
        launched.append(worker)
        return worker

    monkeypatch.setattr(ocr.subprocess, "Popen", popen)
    return launched


def test_one_worker_serves_several_images_and_returns_layout(monkeypatch, image):
    lines = [{"x": 1, "y": 2, "w": 10, "h": 5, "text": "中 文"}]
    launched = install(monkeypatch, lambda payload: {"ok": True, "text": "中 文\nEnglish", "lines": lines})
    with ocr.OcrSession(idle_seconds=7, max_jobs=9) as session:
        first = session.recognize(image)
        second = session.recognize(image)
        directory = session.directory
    assert first == second == {"text": "中文\nEnglish", "lines": lines, "codes": []}
    assert len(launched) == 1
    command = launched[0].command
    assert command[0] == sys.executable and Path(command[1]).name == "main.py"
    assert command[2:] == ["--ocr-worker", "--channel-dir", str(directory), "--idle-seconds", "7", "--max-jobs", "9"]
    assert not directory.exists() and launched[0].terminated == 1


def test_idle_exit_race_is_retried_with_a_fresh_worker(monkeypatch, image):
    answers = iter(["idle-exit", {"ok": True, "text": "第二个进程", "lines": []}])
    launched = install(monkeypatch, lambda payload: next(answers))
    with ocr.OcrSession() as session:
        assert session.recognize(image)["text"] == "第二个进程"
    assert len(launched) == 2


def test_crash_on_an_image_is_reported_and_not_retried(monkeypatch, image):
    launched = install(monkeypatch, lambda payload: "crash")
    with ocr.OcrSession() as session:
        with pytest.raises(RuntimeError, match="退出码 3"):
            session.recognize(image)
    assert len(launched) == 1


@pytest.mark.parametrize("answer", [{"ok": False, "error": "模型失败"}, {"ok": True, "text": None}, {"ok": True, "text": "x", "lines": "bad"}])
def test_failed_or_malformed_results_never_return_text(monkeypatch, image, answer):
    install(monkeypatch, lambda payload: answer)
    with ocr.OcrSession() as session:
        with pytest.raises(RuntimeError):
            session.recognize(image)


def test_cancel_and_timeout_stop_the_worker(monkeypatch, image):
    launched = install(monkeypatch, lambda payload: "hang")
    event = threading.Event()
    session = ocr.OcrSession()
    timer = threading.Timer(0.2, event.set)
    timer.start()
    with pytest.raises(ocr.OcrCancelled):
        session.recognize(image, stop_event=event)
    assert launched[0].terminated == 1 and session.directory is None
    ticks = iter((0.0, 10.0, 20.0))
    monkeypatch.setattr(ocr.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        session.recognize(image, timeout=1)
    assert launched[1].terminated == 1
    session.close()


def test_pre_cancelled_or_missing_input_never_starts(monkeypatch, image, tmp_path):
    launched = install(monkeypatch, lambda payload: {"ok": True, "text": "", "lines": []})
    event = threading.Event()
    event.set()
    with ocr.OcrSession() as session:
        with pytest.raises(ocr.OcrCancelled):
            session.recognize(image, stop_event=event)
        with pytest.raises(FileNotFoundError):
            session.recognize(tmp_path / "missing.png")
    assert launched == []


def fake_rapidocr(monkeypatch, created):
    class Engine:
        def __init__(self, params):
            created.append(params)

        def __call__(self, image):
            return SimpleNamespace(boxes=None, txts=[])

    package = ModuleType("rapidocr")
    package.RapidOCR = Engine
    package.ModelType = SimpleNamespace(SMALL="small")
    package.OCRVersion = SimpleNamespace(PPOCRV6="v6")
    download = ModuleType("rapidocr.utils.download_file")
    download.DownloadFile = type("DownloadFile", (), {})
    monkeypatch.setitem(sys.modules, "rapidocr", package)
    monkeypatch.setitem(sys.modules, "rapidocr.utils.download_file", download)
    monkeypatch.setattr(runtime, "validate_models", lambda: {name: Path(name) for name in runtime.MODEL_SHA256})


def test_engine_loads_the_model_once_for_many_images(monkeypatch, image):
    created = []
    fake_rapidocr(monkeypatch, created)
    engine = runtime.OcrEngine()
    assert engine.recognize(image) == ("", [])
    assert engine.recognize(image) == ("", [])
    assert len(created) == 1


def test_layout_matches_reading_order_of_text():
    lines = [{"x": 90, "y": 15, "top": 10, "w": 40, "h": 10, "text": "右"},
             {"x": 5, "y": 15, "top": 10, "w": 40, "h": 10, "text": "左"},
             {"x": 5, "y": 45, "top": 40, "w": 40, "h": 10, "text": "下"}]
    assert runtime.arrange(lines) == "左 右\n下"
    assert [line["text"] for line in runtime.layout(lines)] == ["左", "右", "下"]
    assert runtime.layout(lines)[0] == {"x": 5, "y": 10, "w": 40, "h": 10, "text": "左"}


def serve(tmp_path, monkeypatch, *extra, requests=(), engine=None):
    channel = tmp_path / "channel"
    channel.mkdir()
    runtime.write_worker_identity(channel / "owner.json")
    for request in requests:
        (channel / "request.json").write_text(json.dumps(request), encoding="utf-8")
    engine = engine or SimpleNamespace(recognize=lambda path: ("合成文字", [{"x": 1, "y": 1, "w": 2, "h": 2, "text": "合成文字"}]))
    monkeypatch.setattr(runtime, "OcrEngine", lambda: engine)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    code = runtime.serve_main(["--ocr-worker", "--channel-dir", str(channel), *extra])
    return code, channel


def test_worker_answers_one_request_then_exits_after_max_jobs(tmp_path, monkeypatch, image):
    code, channel = serve(tmp_path, monkeypatch, "--idle-seconds", "30", "--max-jobs", "1",
                          requests=[{"request_id": "abc", "input": str(image)}])
    assert code == 0
    result = json.loads((channel / "response.json").read_text(encoding="utf-8"))
    assert result["ok"] and result["request_id"] == "abc" and result["text"] == "合成文字"
    assert result["lines"][0]["text"] == "合成文字"
    assert (channel / "identity.json").is_file() and not (channel / "request.json").exists()


def test_worker_reports_failures_and_exits_when_idle(tmp_path, monkeypatch):
    def broken(path):
        raise RuntimeError("synthetic native failure")
    code, channel = serve(tmp_path, monkeypatch, "--idle-seconds", ".2",
                          requests=[{"request_id": "x", "input": str(tmp_path / "missing.png")}],
                          engine=SimpleNamespace(recognize=broken))
    assert code == 0
    result = json.loads((channel / "response.json").read_text(encoding="utf-8"))
    assert result["ok"] is False and "不存在" in result["error"]


def test_worker_rejects_malformed_requests(tmp_path, monkeypatch):
    code, channel = serve(tmp_path, monkeypatch, "--idle-seconds", "5", requests=[["not", "a", "request"]])
    assert code == 1 and not (channel / "response.json").exists()


def test_main_dispatches_persistent_worker_before_qt(monkeypatch):
    import builtins
    import runpy
    runtime_module = ModuleType("snapvault.ocr_runtime")
    seen = []
    runtime_module.serve_main = lambda argv: seen.append(argv) or 0
    monkeypatch.setitem(sys.modules, "snapvault.ocr_runtime", runtime_module)
    monkeypatch.setattr(sys, "argv", ["main.py", "--ocr-worker", "--channel-dir", "private"])
    original = builtins.__import__

    def reject_ui(name, *args, **kwargs):
        assert not name.startswith(("PySide6", "snapvault.ui", "snapvault.branding"))
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_ui)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(ocr.__file__).resolve().parents[1] / "main.py"), run_name="__main__")
    assert result.value.code == 0 and seen == [["--ocr-worker", "--channel-dir", "private"]]
