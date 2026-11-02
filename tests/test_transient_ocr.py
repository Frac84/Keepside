"""Transient screenshot OCR never reads or changes a vault and removes temp files."""
from pathlib import Path
from types import SimpleNamespace

from snapvault import ocr
from snapvault.workers import OcrWorker
from test_annotation import app  # noqa: F401
from test_screenshot_outputs import picture


def test_transient_job_recognizes_final_pixels_and_removes_file(monkeypatch, app):
    def forbidden(*_a, **_k):
        raise AssertionError("Transient OCR touched the library")
    worker = OcrWorker(SimpleNamespace(get=forbidden, finish_ocr=forbidden, fail_ocr=forbidden))
    observed, results, closed = [], [], []
    monkeypatch.setattr(ocr, "check_availability", lambda: None)
    monkeypatch.setattr(ocr, "languages", lambda: ["auto"])
    class Session:
        def recognize(self, path, *, stop_event):
            path = Path(path)
            assert path.is_file()
            observed.append(path)
            return {"text": "final screenshot", "lines": []}
        def close(self):
            closed.append(True)
    monkeypatch.setattr(ocr, "OcrSession", Session)
    worker.transient.connect(lambda tag, result, error: (results.append((tag, result, error)), worker.stopping.set()))
    assert worker.enqueue_image(picture(), "job")
    assert not worker.enqueue_image(picture(), "job")
    worker.run()
    assert results == [("job", {"text": "final screenshot", "lines": []}, "")]
    assert closed == [True] and not worker.queued
    assert all(not path.exists() and not path.parent.exists() for path in observed)


def test_transient_failure_cleans_up_and_reports_without_record(monkeypatch, app):
    observed, results = [], []
    worker = OcrWorker(None)
    monkeypatch.setattr(ocr, "check_availability", lambda: None)
    monkeypatch.setattr(ocr, "languages", lambda: ["auto"])
    class Session:
        def recognize(self, path, *, stop_event):
            observed.append(Path(path))
            raise RuntimeError("synthetic failure")
        def close(self):
            pass
    monkeypatch.setattr(ocr, "OcrSession", Session)
    worker.transient.connect(lambda tag, result, error: (results.append((tag, result, error)), worker.stopping.set()))
    assert worker.enqueue_image(picture(), 2)
    worker.run()
    assert results[0][2] == "synthetic failure" and not worker.queued
    assert all(not path.exists() and not path.parent.exists() for path in observed)
