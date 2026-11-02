"""Text/link queue jobs must save in order and never enter model inference."""
import threading
from types import SimpleNamespace

from snapvault import workers


def test_capture_text_jobs_drain_before_stop_and_preserve_original_text():
    accepted, saved = [], []
    vault = SimpleNamespace(add_text=lambda value: (accepted.append(value) or str(len(accepted)), True))
    worker = workers.CaptureWorker(vault)
    worker.saved.connect(lambda key, created: saved.append((key, created)))
    payloads = ['def demo():\r\n    return "中文"\r\n', 'https://example.com/?a=1&token=unchanged#part']
    for value in payloads:
        assert worker.submit('text', value)
    assert worker.submit('stop')
    worker.run()
    assert accepted == payloads
    assert saved == [('1', True), ('2', True)]


def test_non_image_queue_entry_does_not_start_ocr(monkeypatch):
    calls = []
    vault = SimpleNamespace(get=lambda _: {'item_type': 'text', 'ocr_text': 'preserved'},
                            fail_ocr=lambda *_: calls.append('unexpected failure'))
    monkeypatch.setattr(workers.ocr, 'check_availability', lambda: None)
    class Session:
        def recognize(self, *_args, **_kwargs):
            calls.append('unexpected model')

        def close(self):
            calls.append('closed')

    monkeypatch.setattr(workers.ocr, 'OcrSession', Session)
    worker = workers.OcrWorker(vault)
    worker.changed.connect(lambda _: worker.stopping.set())
    worker.enqueue('text-entry')
    worker.run()
    assert calls == ['closed']
    assert not worker.queued
