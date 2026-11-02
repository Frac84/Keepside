"""Queue integration protects previously recognized content on retry errors."""
from PIL import Image
import pytest

from snapvault import ocr
from snapvault.storage import Vault
from snapvault.workers import OcrWorker


@pytest.fixture
def job(tmp_path, monkeypatch):
    vault = Vault(tmp_path / "data")
    key, _ = vault.add(Image.new("RGB", (20, 10), "white"))
    vault.finish_ocr(key, "已有识别文字 English reference")
    worker = OcrWorker(vault)
    monkeypatch.setattr(ocr, "check_availability", lambda: None)
    return vault, key, worker


def use_session(monkeypatch, infer):
    """Route the worker's persistent OCR session to a synthetic recognizer."""
    closed = []

    class Session:
        def recognize(self, path, *, stop_event=None):
            result = infer(path, stop_event=stop_event)
            return result if isinstance(result, dict) else {"text": result, "lines": []}

        def close(self):
            closed.append(True)

    monkeypatch.setattr(ocr, "OcrSession", Session)
    return closed


@pytest.mark.parametrize("failure", ["exception", "empty"])
def test_failed_retry_preserves_text_title_filename_and_search(job, monkeypatch, failure):
    vault, key, worker = job
    original = vault.get(key)
    fail = vault.fail_ocr
    def record_failure(*args):
        fail(*args)
        worker.stopping.set()
    monkeypatch.setattr(vault, "fail_ocr", record_failure)
    def infer(path, language="auto", *, stop_event):
        assert stop_event is worker.stopping
        if failure == "exception": raise RuntimeError("synthetic native failure")
        return ""
    use_session(monkeypatch, infer)
    worker.enqueue(key)
    worker.run()
    result = vault.get(key)
    for name in ("ocr_text", "filename", "title", "search_text"):
        assert result[name] == original[name]
    assert result["ocr_status"] == "error" and result["ocr_error"]
    assert vault.query("English reference") and not worker.queued


def test_success_uses_single_queue_and_finishes_once(job, monkeypatch):
    vault, key, worker = job
    finish = vault.finish_ocr
    calls = []
    def complete(*args, **kwargs):
        finish(*args, **kwargs)
        worker.stopping.set()
    monkeypatch.setattr(vault, "finish_ocr", complete)
    def infer(path, language="auto", *, stop_event):
        calls.append(path)
        return "新的识别结果"
    use_session(monkeypatch, infer)
    worker.enqueue(key); worker.enqueue(key)
    worker.run()
    assert len(calls) == 1 and not worker.queued
    assert vault.get(key)["ocr_text"] == "新的识别结果"


def test_shutdown_cancellation_leaves_existing_content_unchanged(job, monkeypatch):
    vault, key, worker = job
    before = vault.get(key)
    def infer(path, language="auto", *, stop_event):
        stop_event.set()
        raise ocr.OcrCancelled("cancelled")
    use_session(monkeypatch, infer)
    worker.enqueue(key)
    worker.run()
    assert vault.get(key) == before and not worker.queued


def test_layout_is_saved_with_text_and_session_is_closed(job, monkeypatch):
    vault, key, worker = job
    lines = [{"x": 1, "y": 2, "w": 30, "h": 8, "text": "新的"}, {"x": 1, "y": 12, "w": 30, "h": 8, "text": "结果"}]
    finish = vault.finish_ocr
    def complete(*args, **kwargs):
        finish(*args, **kwargs)
        worker.stopping.set()
    monkeypatch.setattr(vault, "finish_ocr", complete)
    closed = use_session(monkeypatch, lambda path, *, stop_event: {"text": "新的\n结果", "lines": lines})
    worker.enqueue(key)
    worker.run()
    assert vault.get(key)["ocr_text"] == "新的\n结果"
    assert vault.ocr_lines(key) == [dict(line, x=float(line["x"]), y=float(line["y"]),
                                         w=float(line["w"]), h=float(line["h"])) for line in lines]
    assert closed == [True]
