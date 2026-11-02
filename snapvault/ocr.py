"""Lightweight OCR clients. Native model libraries live only in OCR worker processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid


class OcrCancelled(RuntimeError):
    """The owning application stopped an unfinished OCR job."""


def clean_text(text):
    cjk = r"\u3400-\u9fff\u3000-\u303f\uff00-\uffef"
    return re.sub(rf"(?<=[{cjk}])[ \t]+(?=[{cjk}])", "", text)


def languages():
    return [("auto", "自动识别（中英文混合）")]


def check_availability():
    """Validate models without importing RapidOCR, ONNX, WinRT or Qt."""
    from .ocr_runtime import validate_models
    validate_models()


def _worker_command(job_path):
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).resolve().parents[1] / "main.py"))
    return command + ["--ocr-job", str(job_path)]


def _stop_launcher(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=0.4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.5)
    except OSError:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.5)


def recognize(path, language="auto", *, stop_event=None, timeout=180):
    """Run one disposable job; legacy language settings use mixed recognition."""
    from .ocr_runtime import current_process_identity, stop_worker_identity
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError("待识别图片不存在。")
    if stop_event is not None and stop_event.is_set():
        raise OcrCancelled("识别任务已取消。")
    if timeout <= 0:
        raise TimeoutError("文字识别超时，原图和已有文字均已保留。")
    with tempfile.TemporaryDirectory(prefix="shiying-ocr-") as temporary:
        directory = Path(temporary)
        output, lifetime = directory / "result.json", directory / "alive"
        identity, job_path = directory / "worker.json", directory / "job.json"
        lifetime.write_text("active", encoding="ascii")
        owner = current_process_identity()
        job_path.write_text(json.dumps({
            "input": str(path), "output": str(output), "lifetime": str(lifetime),
            "identity": str(identity), "parent_pid": owner["pid"], "parent_created": owner["created"],
        }, ensure_ascii=False), encoding="utf-8")
        process = None
        try:
            process = subprocess.Popen(_worker_command(job_path), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    raise OcrCancelled("识别任务已取消。")
                if time.monotonic() >= deadline:
                    raise TimeoutError("文字识别超过限定时间，原图和已有文字均已保留。")
                if stop_event is None:
                    time.sleep(0.05)
                else:
                    stop_event.wait(0.05)
            if stop_event is not None and stop_event.is_set():
                raise OcrCancelled("识别任务已取消。")
            if not output.is_file():
                raise RuntimeError(f"文字识别进程未生成结果（退出码 {process.returncode}）；原图和已有文字均已保留。")
            result = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or not result.get("ok") or process.returncode != 0:
                error = result.get("error") if isinstance(result, dict) else "结果格式无效"
                raise RuntimeError("文字识别失败：" + str(error or f"进程退出码 {process.returncode}"))
            if not isinstance(result.get("text"), str):
                raise RuntimeError("文字识别结果格式无效，已有文字已保留。")
            return clean_text(result["text"])
        finally:
            # The real interpreter behind a Windows venv launcher watches this.
            lifetime.unlink(missing_ok=True)
            try:
                _stop_launcher(process)
            finally:
                stop_worker_identity(identity)


class OcrSession:
    """Client for one long-lived local OCR process.

    The model is loaded once for a queue of images; the process exits by itself
    after idle_seconds or max_jobs, and the next request starts a fresh one.
    Cancellation and timeouts stop only this session's verified worker.
    """

    def __init__(self, *, idle_seconds=20, max_jobs=40):
        self.idle_seconds, self.max_jobs = idle_seconds, max_jobs
        self.process = None
        self.temporary = None
        self.directory = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        from .ocr_runtime import stop_worker_identity
        if self.directory:
            # The worker leaves on its own once the owner marker disappears.
            (self.directory / "owner.json").unlink(missing_ok=True)
        try:
            _stop_launcher(self.process)
        finally:
            if self.directory:
                stop_worker_identity(self.directory / "identity.json")
            self.process = None
            self.directory = None
            if self.temporary:
                self.temporary.cleanup()
                self.temporary = None

    def _start(self):
        from .ocr_runtime import current_process_identity
        self.close()
        self.temporary = tempfile.TemporaryDirectory(prefix="shiying-ocr-")
        self.directory = Path(self.temporary.name)
        (self.directory / "owner.json").write_text(json.dumps(current_process_identity()), encoding="utf-8")
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).resolve().parents[1] / "main.py"))
        command += ["--ocr-worker", "--channel-dir", str(self.directory),
                    "--idle-seconds", str(self.idle_seconds), "--max-jobs", str(self.max_jobs)]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            self.close()
            raise

    def recognize(self, path, *, stop_event=None, timeout=180):
        """Return {"text": str, "lines": [...], "codes": [...]} for one image."""
        path = Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError("待识别图片不存在。")
        if stop_event is not None and stop_event.is_set():
            raise OcrCancelled("识别任务已取消。")
        if timeout <= 0:
            raise TimeoutError("文字识别超时，原图和已有文字均已保留。")
        # A worker can exit for idleness just as a request is posted. Retry that
        # transport once; a real crash on the image itself is reported.
        for _attempt in range(2):
            if self.process is None or self.process.poll() is not None:
                self._start()
            request_id = uuid.uuid4().hex
            request = self.directory / "request.json"
            response = self.directory / "response.json"
            response.unlink(missing_ok=True)
            temporary = request.with_suffix(".tmp")
            temporary.write_text(json.dumps({"request_id": request_id, "input": str(path)}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, request)
            deadline = time.monotonic() + timeout
            while True:
                if stop_event is not None and stop_event.is_set():
                    self.close()
                    raise OcrCancelled("识别任务已取消。")
                if response.is_file():
                    try:
                        result = json.loads(response.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        self.close()
                        raise RuntimeError("文字识别结果格式无效，已有文字已保留。")
                    response.unlink(missing_ok=True)
                    return self._result(result, request_id)
                if self.process.poll() is not None:
                    code = self.process.returncode
                    break
                if time.monotonic() >= deadline:
                    self.close()
                    raise TimeoutError("文字识别超过限定时间，原图和已有文字均已保留。")
                if stop_event is None:
                    time.sleep(0.05)
                else:
                    stop_event.wait(0.05)
            started_for_this_request = request.exists()
            self.close()
            if not started_for_this_request:
                # The worker read the request and then died: the image itself
                # crashed the native runtime. Do not try the same image again.
                raise RuntimeError(f"文字识别进程意外退出（退出码 {code}）；原图和已有文字均已保留。")
        raise RuntimeError("文字识别进程未能启动，请检查模型文件后重试。")

    def _result(self, result, request_id):
        if not isinstance(result, dict) or result.get("request_id") != request_id:
            self.close()
            raise RuntimeError("文字识别结果格式无效，已有文字已保留。")
        if not result.get("ok"):
            raise RuntimeError("文字识别失败：" + str(result.get("error") or "未知错误"))
        text, lines, codes = result.get("text"), result.get("lines", []), result.get("codes", [])
        if not isinstance(text, str) or not isinstance(lines, list) or not isinstance(codes, list):
            raise RuntimeError("文字识别结果格式无效，已有文字已保留。")
        return {"text": clean_text(text), "lines": lines, "codes": [code for code in codes if isinstance(code, str)]}
