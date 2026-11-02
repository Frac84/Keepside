"""Review history, agreement, personal head training and the keyboard review dialog."""
import json
import math
from types import SimpleNamespace

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QWidget
import pytest

from snapvault import topic_runtime as runtime
from snapvault import topics, workers
from snapvault.review import ReviewDialog
from snapvault.storage import Vault


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault")


def auto(vault, key, tags):
    snapshot = vault.classification_input(key)
    assert vault.apply_classification(key, tags, {}, snapshot["fingerprint"])


def test_corrections_record_what_the_automatic_layer_showed(vault):
    a, _ = vault.add_text("Python 接口文档")
    b, _ = vault.add_text("周五下午三点开会")
    c, _ = vault.add_text("随便记一句")
    auto(vault, a, ["tech"])
    auto(vault, b, ["tech"])
    auto(vault, c, [])
    vault.set_tags(a, ["tech"], source="review")       # confirmed
    vault.set_tags(b, ["tasks"])                        # corrected
    vault.set_tags(b, ["tasks", "chat"])                # later edit of a manual decision
    vault.set_tags(c, [], source="review")             # confirmed 待整理
    with vault.connect() as db:
        rows = db.execute("SELECT item_id,previous_tags_json,previous_status,source FROM classification_corrections ORDER BY id").fetchall()
    assert [(row["item_id"], json.loads(row["previous_tags_json"]), row["previous_status"], row["source"]) for row in rows] == [
        (a, ["tech"], "done", "review"), (b, ["tech"], "done", "edit"), (b, ["tasks"], "manual", "edit"), (c, [], "done", "review")]
    stats = vault.classification_agreement()
    assert stats["reviewed"] == 3 and stats["agree"] == 2
    assert stats["added"] == 1 and stats["removed"] == 1 and stats["manual"] == 3


def test_training_examples_use_current_manual_topics_only(vault):
    a, _ = vault.add_text("Figma 配色方案")
    b, _ = vault.add_text("旧分类")
    c, _ = vault.add_text("没有主题")
    image, _ = vault.add(Image.new("RGB", (8, 8), "red"))
    vault.set_tags(a, ["design"])
    vault.set_category(b, "read")            # legacy label is not a training target
    vault.set_tags(c, [])
    vault.set_tags(image, ["media"])          # no OCR text, nothing to learn from
    examples = vault.training_examples()
    assert [(item["key"], item["tags"]) for item in examples] == [(c, []), (a, ["design"])]
    d, _ = vault.add_text("自动内容")
    auto(vault, d, ["learning"])
    assert [item["key"] for item in vault.anchor_examples()] == [d]


def test_review_queue_scopes_skip_manual_items(vault):
    a, _ = vault.add_text("甲")
    b, _ = vault.add_text("乙")
    c, _ = vault.add_text("丙")
    auto(vault, a, [])
    auto(vault, b, ["tech"])
    vault.set_tags(c, [])
    assert vault.review_queue("unknown") == [a]
    assert set(vault.review_queue("auto")) == {a, b}
    with pytest.raises(ValueError):
        vault.review_queue("everything")


def test_personal_model_candidate_activation_and_toggle(vault):
    assert vault.active_personal_head() is None
    vault.write_personal_head({"kind": "x"})
    assert vault.personal_head_path(candidate=True).is_file()
    assert vault.active_personal_head() is None
    vault.activate_personal_head({"examples": 12})
    assert vault.active_personal_head() == vault.personal_head_path()
    assert vault.personal_model()["report"] == {"examples": 12}
    assert vault.set_personal_model_enabled(False) is False
    assert vault.active_personal_head() is None and vault.personal_model()["available"]
    vault.set_personal_model_enabled(True)
    vault.personal_head_path().unlink()
    assert vault.active_personal_head() is None and not vault.set_personal_model_enabled(True)


def unit(np, index, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    vector = np.zeros(512)
    vector[index] = 1.0
    vector += noise * rng.standard_normal(512)
    return vector / np.linalg.norm(vector)


def test_fit_learns_a_repeated_correction_without_drifting_elsewhere():
    np = pytest.importorskip("numpy")
    coef = np.zeros((len(runtime.LABELS), 512))
    intercept = np.full(len(runtime.LABELS), -3.0)
    chat = runtime.LABELS.index("chat")
    examples = [(unit(np, 0, 0.05, seed)[None, :], {"chat"}) for seed in range(12)]
    anchors = [unit(np, 7, 0.05, seed + 50)[None, :] for seed in range(30)]
    new_coef, new_intercept = runtime.fit_personal_head(np, coef, intercept, examples, anchors)
    similar = unit(np, 0, 0.05, 999)[None, :]
    unrelated = unit(np, 7, 0.05, 998)[None, :]
    assert "chat" in runtime.window_tags(np, similar, new_coef, new_intercept)
    assert runtime.window_tags(np, unrelated, new_coef, new_intercept) == set()
    # Labels without evidence keep their shipped behaviour on anchors.
    tech = runtime.LABELS.index("tech")
    assert abs(float((unrelated @ new_coef[tech])[0] + new_intercept[tech]) - (-3.0)) < 0.5
    assert np.isfinite(new_coef).all() and new_coef[chat].any()
    unchanged = runtime.fit_personal_head(np, coef, intercept, [], [])
    assert np.allclose(unchanged[0], coef) and np.allclose(unchanged[1], intercept)


def personal_head(np, **changes):
    head = {"kind": runtime.PERSONAL_HEAD_KIND, "classes": list(runtime.LABELS),
            "base_head_sha256": runtime.base_head_sha256(),
            "coef": np.zeros((len(runtime.LABELS), 512)).tolist(), "intercept": [0.5] * len(runtime.LABELS)}
    head.update(changes)
    return head


def test_personal_head_validation_and_engine_fallback(tmp_path):
    np = pytest.importorskip("numpy")
    path = tmp_path / "head.json"
    path.write_text(json.dumps(personal_head(np)), encoding="utf-8")
    coef, intercept, digest = runtime.load_personal_head(path, runtime.base_head_sha256())
    assert coef.shape == (7, 512) and intercept[0] == 0.5 and len(digest) == 8
    for bad in (personal_head(np, base_head_sha256="0" * 64), personal_head(np, coef=[[0.0]] * 7),
                personal_head(np, intercept=[math.inf] * 7), personal_head(np, classes=["tech"])):
        broken = tmp_path / "broken.json"
        broken.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            runtime.load_personal_head(broken, runtime.base_head_sha256())
    engine = runtime.TopicEngine()
    base = np.ones((7, 512)), np.zeros(7)
    engine._base_coef, engine._base_intercept = base
    engine._coef, engine._intercept = base
    engine.select_head(path)
    engine._ensure_head()
    assert engine.version.startswith(runtime.VERSION + "+personal-") and engine._intercept[0] == 0.5
    engine.select_head(None)
    engine._ensure_head()
    assert engine.version == runtime.VERSION and engine._coef is base[0]
    engine.select_head(tmp_path / "missing.json")
    engine._ensure_head()
    assert engine.version == runtime.VERSION and engine.head_error


def test_train_requests_are_answered_without_leaking_details():
    class Engine:
        def __init__(self, error=None):
            self.error, self.calls = error, []

        def train(self, examples, anchors):
            self.calls.append((examples, anchors))
            if self.error:
                raise self.error
            return {"kind": "personal"}, {"examples": len(examples)}

        def classify(self, text, kind):
            raise AssertionError("train requests never classify")

    engine = Engine()
    result = runtime.process_request({"request_id": "t", "op": "train", "examples": [{"text": "x", "tags": []}], "anchors": []}, engine)
    assert result == {"request_id": "t", "head": {"kind": "personal"}, "report": {"examples": 1}}
    result = runtime.process_request({"request_id": "t", "op": "train", "examples": [], "anchors": []},
                                     Engine(ValueError("可用于训练的手动标签太少")))
    assert result["error"] == "可用于训练的手动标签太少"
    result = runtime.process_request({"request_id": "t", "op": "train", "examples": [], "anchors": []},
                                     Engine(RuntimeError("C:/private/path secret")))
    assert "private" not in json.dumps(result) and result["error"]


def test_classification_requests_select_the_requested_head():
    selected = []
    engine = SimpleNamespace(select_head=selected.append,
                             classify=lambda text, kind: {"tags": [], "scores": {}, "version": "v"})
    runtime.process_request({"request_id": 1, "items": [{"key": "a", "text": "x"}], "head": "C:/head.json"}, engine)
    runtime.process_request({"request_id": 2, "items": [{"key": "a", "text": "x"}]}, engine)
    assert selected == ["C:/head.json", None]


def test_client_caps_training_text_and_passes_head(monkeypatch):
    client = topics.TopicClient()
    sent = []
    monkeypatch.setattr(client, "_exchange", lambda payload, **kwargs: sent.append(payload) or
                        ({"head": {"k": 1}, "report": {"r": 2}} if payload.get("op") == "train" else {"results": []}))
    head, report = client.train([{"text": "长" * 9000, "kind": "text", "tags": ["tech"], "key": "private-key"}],
                                [{"text": "锚点", "kind": "image", "key": "k2"}])
    assert (head, report) == ({"k": 1}, {"r": 2})
    example, anchor = sent[0]["examples"][0], sent[0]["anchors"][0]
    assert len(example["text"]) == runtime.MAX_TRAIN_CHARS and example["tags"] == ["tech"] and "key" not in example
    assert anchor == {"text": "锚点", "kind": "image"}
    client.head_path = "C:/data/models/personal-head.json"
    client.classify_batch([{"key": "a", "text": "x", "kind": "text"}])
    assert sent[1]["head"] == "C:/data/models/personal-head.json"


def test_head_trainer_stores_a_candidate_and_reports(monkeypatch, vault):
    for index in range(3):
        key, _ = vault.add_text(f"手动内容 {index}")
        vault.set_tags(key, ["tech"])

    class Client:
        def __init__(self, idle_seconds):
            self.closed = False

        def train(self, examples, anchors, stop_event=None):
            assert len(examples) == 3
            return {"kind": "personal"}, {"examples": 3}

        def close(self):
            self.closed = True

    monkeypatch.setattr(topics, "TopicClient", Client)
    trainer = workers.HeadTrainer(vault)
    reports = []
    trainer.trained.connect(reports.append)
    trainer.run()
    assert reports == [{"examples": 3}]
    assert json.loads(vault.personal_head_path(candidate=True).read_text(encoding="utf-8")) == {"kind": "personal"}
    assert vault.active_personal_head() is None


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Host(QWidget):
    def __init__(self, vault):
        super().__init__()
        self.vault = vault
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


def test_review_dialog_tags_with_number_keys_and_records_review(app, vault):
    a, _ = vault.add_text("第一条待整理")
    b, _ = vault.add_text("第二条待整理")
    c, _ = vault.add_text("第三条待整理")
    for key in (a, b, c):
        auto(vault, key, [])
    host = Host(vault)
    dialog = ReviewDialog(host)
    keys = {shortcut.key().toString() for shortcut in dialog.findChildren(QShortcut)}
    assert {"1", "2", "3", "4", "5", "6", "7", "0", "Return", "Right", "Left", "Space"} <= keys
    assert dialog.row["id"] == c  # newest first
    dialog.toggle("tech")
    dialog.toggle("design")
    dialog.toggle("media")     # a third topic is refused
    assert dialog._selected() == ["tech", "design"] and "最多两个" in dialog.notice.text()
    dialog.save_and_next()
    assert vault.get(c)["tags"] == ["tech", "design"] and vault.get(c)["tags_manual"]
    assert dialog.row["id"] == b
    dialog.step(1)
    assert dialog.row["id"] == a
    dialog.clear_and_save()
    assert vault.get(a)["tags_manual"] and vault.get(a)["tags"] == []
    assert dialog.row is None and "跳过" in dialog.title.text()
    dialog.step(-1)
    assert dialog.row["id"] == b
    dialog.done(0)
    assert host.refreshed == 1
    with vault.connect() as db:
        assert {row[0] for row in db.execute("SELECT source FROM classification_corrections")} == {"review"}
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    host.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_topic_worker_passes_an_enabled_personal_head(monkeypatch, vault):
    import threading
    import time
    key, _ = vault.add_text("需要分类的内容")
    vault.write_personal_head({"kind": "x"})
    vault.activate_personal_head({})
    seen = []

    class Client:
        head_path = None

        def classify_batch(self, items, *, stop_event=None):
            seen.append(self.head_path)
            return [{"key": item["key"], "result": {"tags": [], "scores": {}, "version": "v"}} for item in items]

        def close(self):
            pass

    monkeypatch.setattr(topics, "TopicClient", lambda: Client())
    worker = workers.TopicWorker(vault)
    worker.enqueue(key)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 4
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    worker.stopping.set()
    thread.join(timeout=4)
    assert seen == [vault.personal_head_path()]
