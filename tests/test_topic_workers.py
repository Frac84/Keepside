"""Queue/CAS/cancellation regressions using synthetic vaults and a fake client."""
import threading
import time

from PIL import Image
import pytest

from snapvault import topics
from snapvault.storage import Vault
from snapvault.workers import TopicWorker


class FakeClient:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []
        self.closed = False

    def classify_batch(self, items, *, stop_event=None):
        self.calls.append(items)
        return self.respond(items, stop_event, len(self.calls))

    def close(self):
        self.closed = True


def result_for(items, tags):
    return [{"key": item["key"], "result": {"tags": tags, "scores": {}, "version": "setfit-bge-v1"}}
            for item in items]


def wait_until(condition, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.01)
    assert condition(), "background worker did not reach expected state"


def start_worker(monkeypatch, vault, respond, keys):
    client = FakeClient(respond)
    monkeypatch.setattr(topics, "TopicClient", lambda: client)
    worker = TopicWorker(vault)
    for key in keys:
        worker.enqueue(key)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    return worker, client, thread


def stop_worker(worker, client, thread):
    worker.stopping.set()
    thread.join(timeout=4)
    assert not thread.is_alive()
    assert client.closed


def old_auto_tag(vault, key, tags=("tech",)):
    snapshot = vault.classification_input(key)
    assert vault.apply_classification(key, list(tags), {}, snapshot["fingerprint"])
    vault.request_reclassification([key])


def test_duplicate_queue_requests_do_not_reclassify_completed_content(monkeypatch, tmp_path):
    vault = Vault(tmp_path)
    key, _ = vault.add_text("Python functions and exceptions")
    worker, client, thread = start_worker(monkeypatch, vault, lambda items, stop, n: result_for(items, ["tech"]), [key] * 5)
    try:
        wait_until(lambda: vault.get(key)["classification_status"] == "done")
        wait_until(lambda: worker.jobs.empty() and not worker.queued)
        assert len(client.calls) == 1
        assert vault.get(key)["tags"] == ["tech"]
    finally:
        stop_worker(worker, client, thread)


def test_manual_edit_during_inference_is_never_overwritten(monkeypatch, tmp_path):
    vault = Vault(tmp_path)
    key, _ = vault.add_text("A screenshot receipt and some source code")

    def respond(items, stop, number):
        vault.set_tags(key, ["receipts"])
        return result_for(items, ["tech"])

    worker, client, thread = start_worker(monkeypatch, vault, respond, [key])
    try:
        wait_until(lambda: worker.jobs.empty() and not worker.queued)
        row = vault.get(key)
        assert row["tags"] == ["receipts"]
        assert row["tags_manual"] and row["classification_status"] == "manual"
        assert len(client.calls) == 1
    finally:
        stop_worker(worker, client, thread)


def test_new_ocr_snapshot_invalidates_old_result_and_is_requeued(monkeypatch, tmp_path):
    vault = Vault(tmp_path)
    with Image.new("RGB", (16, 16), "white") as image:
        key, _ = vault.add(image)
    vault.finish_ocr(key, "old Python source code")

    def respond(items, stop, number):
        if number == 1:
            vault.finish_ocr(key, "new purchase receipt and payment amount")
            return result_for(items, ["tech"])
        return result_for(items, ["receipts"])

    worker, client, thread = start_worker(monkeypatch, vault, respond, [key])
    try:
        wait_until(lambda: vault.get(key)["classification_status"] == "done")
        assert len(client.calls) == 2
        assert client.calls[0][0]["text"] == "old Python source code"
        assert client.calls[1][0]["text"] == "new purchase receipt and payment amount"
        row = vault.get(key)
        assert row["tags"] == ["receipts"]
        assert row["ocr_text"] == "new purchase receipt and payment amount"
    finally:
        stop_worker(worker, client, thread)


@pytest.mark.parametrize("raise_after_stop", [False, True])
def test_cancellation_after_client_return_keeps_pending_and_prior_tags(monkeypatch, tmp_path, raise_after_stop):
    vault = Vault(tmp_path)
    key, _ = vault.add_text("Synthetic saved reference with an existing automatic tag")
    old_auto_tag(vault, key)

    def respond(items, stop, number):
        stop.set()
        if raise_after_stop:
            raise RuntimeError("native exception quoting private-content")
        return result_for(items, [])

    worker, client, thread = start_worker(monkeypatch, vault, respond, [key])
    try:
        thread.join(timeout=4)
        assert not thread.is_alive()
        row = vault.get(key)
        assert row["tags"] == ["tech"]
        assert row["classification_status"] == "pending"
        assert row["classification_error"] == ""
    finally:
        stop_worker(worker, client, thread)


def test_model_failure_keeps_saved_content_and_existing_automatic_tags(monkeypatch, tmp_path):
    vault = Vault(tmp_path)
    content = "Synthetic existing content retained when local models fail"
    key, _ = vault.add_text(content)
    old_auto_tag(vault, key)

    def respond(items, stop, number):
        raise RuntimeError("private-content C:/private/model.onnx")

    worker, client, thread = start_worker(monkeypatch, vault, respond, [key])
    try:
        wait_until(lambda: vault.get(key)["classification_status"] == "error")
        row = vault.get(key)
        assert row["tags"] == ["tech"] and row["content"] == content
        assert "private-content" not in row["classification_error"]
        assert "model.onnx" not in row["classification_error"]
        assert len(client.calls) == 1
    finally:
        stop_worker(worker, client, thread)


def test_deletion_during_inference_does_not_recreate_or_retry_item(monkeypatch, tmp_path):
    vault = Vault(tmp_path)
    key, _ = vault.add_text("Temporary synthetic content")

    def respond(items, stop, number):
        vault.delete(key)
        return result_for(items, ["learning"])

    worker, client, thread = start_worker(monkeypatch, vault, respond, [key])
    try:
        wait_until(lambda: worker.jobs.empty() and not worker.queued)
        assert vault.get(key) is None and len(client.calls) == 1
    finally:
        stop_worker(worker, client, thread)


def test_malformed_partial_batch_preserves_success_and_marks_missing_item_error(monkeypatch, tmp_path):
    vault = Vault(tmp_path)
    first, _ = vault.add_text("First synthetic reference")
    second, _ = vault.add_text("Second synthetic reference")

    def respond(items, stop, number):
        return result_for(items[:1], ["learning"])

    worker, client, thread = start_worker(monkeypatch, vault, respond, [first, second])
    try:
        wait_until(lambda: vault.get(second)["classification_status"] == "error")
        assert vault.get(first)["tags"] == ["learning"]
        assert vault.get(first)["classification_status"] == "done"
        assert vault.get(second)["content"] == "Second synthetic reference"
    finally:
        stop_worker(worker, client, thread)
