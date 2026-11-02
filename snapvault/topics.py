"""Lightweight client for the private, disposable local topic process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


class ClassificationCancelled(RuntimeError):
    pass


class TopicClient:
    def __init__(self, *, idle_seconds=20):
        self.idle_seconds = idle_seconds
        self.process = None
        self.temporary = None
        self.directory = None
        # Optional validated personal head; None uses the shipped head.
        self.head_path = None

    def close(self):
        from .ocr import _stop_launcher
        from .ocr_runtime import stop_worker_identity
        if self.directory:
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
        self.temporary = tempfile.TemporaryDirectory(prefix="shiying-topics-")
        self.directory = Path(self.temporary.name)
        (self.directory / "owner.json").write_text(json.dumps(current_process_identity()), encoding="utf-8")
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).resolve().parents[1] / "main.py"))
        command += ["--classify-worker", "--channel-dir", str(self.directory), "--idle-seconds", str(self.idle_seconds)]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            self.close()
            raise

    def classify_batch(self, items, *, stop_event=None, timeout=180):
        if stop_event is not None and stop_event.is_set():
            raise ClassificationCancelled("分类已取消")
        if not items:
            return []
        if len(items) > 16:
            raise ValueError("分类批次过大")
        payload = {"items": items}
        if self.head_path:
            payload["head"] = str(self.head_path)
        result = self._exchange(payload, stop_event=stop_event, timeout=timeout)
        if not isinstance(result.get("results"), list):
            self.close()
            raise RuntimeError("本地分类结果格式无效，请重新分类。")
        return result["results"]

    def train(self, examples, anchors, *, stop_event=None, timeout=1800):
        """Fit a personal head in the private worker; returns (head, report)."""
        from .topic_runtime import MAX_TRAIN_CHARS
        def capped(items, with_tags):
            return [{"text": item["text"][:MAX_TRAIN_CHARS], "kind": item["kind"],
                     **({"tags": list(item["tags"])} if with_tags else {})} for item in items]
        result = self._exchange({"op": "train", "examples": capped(examples, True), "anchors": capped(anchors, False)},
                                stop_event=stop_event, timeout=timeout)
        if result.get("error"):
            raise ValueError(str(result["error"]))
        if not isinstance(result.get("head"), dict) or not isinstance(result.get("report"), dict):
            self.close()
            raise RuntimeError("本地训练结果格式无效，请稍后重试。")
        return result["head"], result["report"]

    def _exchange(self, payload, *, stop_event=None, timeout=180):
        # An idle worker can exit just as a new request is posted; retry that
        # transport once. Applying results remains guarded by storage snapshots.
        for attempt in range(2):
            if self.process is None or self.process.poll() is not None:
                self._start()
            request_id = uuid.uuid4().hex
            request = self.directory / "request.json"
            response = self.directory / "response.json"
            response.unlink(missing_ok=True)
            temporary = request.with_suffix(".tmp")
            temporary.write_text(json.dumps({"request_id": request_id, **payload}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, request)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    self.close()
                    raise ClassificationCancelled("分类已取消")
                if response.is_file():
                    result = json.loads(response.read_text(encoding="utf-8"))
                    response.unlink(missing_ok=True)
                    if not isinstance(result, dict) or result.get("request_id") != request_id:
                        self.close()
                        raise RuntimeError("本地分类结果格式无效，请重新分类。")
                    return result
                if self.process.poll() is not None:
                    break
                if stop_event is None:
                    time.sleep(0.05)
                else:
                    stop_event.wait(0.05)
            else:
                self.close()
                raise TimeoutError("本地分类超时，内容已保存，可稍后重新分类。")
            self.close()
        raise RuntimeError("本地分类进程未完成任务，请检查模型文件后重新分类。")
