"""Production topic abstention, structural evidence, local assets and IPC."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

from snapvault import topic_runtime as runtime


def test_import_and_constructor_do_not_load_inference_dependencies():
    command = [sys.executable, "-c", "import sys; from snapvault.topic_runtime import TopicEngine; "
               "e=TopicEngine(); assert not e.loaded; "
               "assert not any(n in sys.modules for n in ('numpy','onnxruntime','tokenizers','torch','PySide6'))"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("text", ["", "好的", "行，那就这么搞。", "2026-10-19 18:35:22", "123.45", "🔔✨",
                                      "https://github.com/", "https://example.com/p/12345?token=secret",
                                      "https://example.com/ab12ef34cd56", "qwertyuiop", "----------------"])
def test_non_content_stays_pending_without_loading_model(text):
    engine = runtime.TopicEngine("missing-model-directory")
    assert engine.classify(text)["tags"] == []
    assert not engine.loaded


def test_url_domain_is_not_classification_evidence():
    assert runtime.semantic_text("https://github.com/notes/旅行摄影") == "notes 旅行摄影"
    assert runtime.semantic_text("https://shop.example.com/article/12345") == ""
    assert runtime.semantic_text("https://example.com/python/exception-handling") == "python exception handling"


def test_json_order_does_not_become_code_because_it_is_json():
    text = json.dumps({"商品": "机械键盘", "订单号": "12345", "金额": 399}, ensure_ascii=False)
    result = runtime.postprocess(text, {"tech": .95, "receipts": .2})
    assert result["tags"] == ["receipts"]


def test_timestamp_in_error_is_not_a_schedule():
    result = runtime.postprocess("2026-10-19 18:35:22 Connection timed out", {"tasks": .9, "tech": .3})
    assert result["tags"] == ["tech"]


def test_real_schedule_with_error_keeps_both_topics():
    result = runtime.postprocess("提醒：明天开会讨论 Connection timed out 报错", {"tasks": .8})
    assert set(result["tags"]) == {"tech", "tasks"}


def test_conversation_and_configuration_are_distinguished():
    result = runtime.postprocess("张三：你明天下午有空吗？\n李四：可以，一起去吃饭吧。", {})
    assert result["tags"] == ["chat"]
    assert runtime.postprocess("服务器：生产服务器\n端口：八零八零\n状态：连接已断开", {})["tags"] == []
    assert runtime.postprocess("注意：分母必须大于零\n例题：计算抽样概率", {})["tags"] == []
    assert runtime.postprocess("商户：便利商店\n交易单号：T202610190061\n付款方式：储蓄卡尾号 1234", {})["tags"] == []
    assert runtime.postprocess("项目：办公用品\n价税合计：壹佰贰拾元整\n开票日期：2026年10月18日", {})["tags"] == []


def test_css_color_theme_can_be_both_development_and_design():
    result = runtime.postprocess(".card { background: #1e2640; color: #e2e8f0; }", {})
    assert set(result["tags"]) == {"tech", "design"}
    variables = runtime.postprocess(":root { --canvas: #1e2640; --accent: #00e676; }", {})
    assert set(variables["tags"]) == {"tech", "design"}


def test_model_threshold_and_two_tag_cap_do_not_force_an_uncertain_tag():
    assert runtime.postprocess("这是一段暂时难以确定主题的内容", {"tech": .42})["tags"] == []
    result = runtime.postprocess("这是一份内容丰富的资料", {"tech": .7, "learning": .8, "design": .6})
    assert result["tags"] == ["learning", "tech"]
    assert result["version"] == runtime.VERSION
    with pytest.raises(ValueError):
        runtime.postprocess("文字", {"tech": math.nan})


def test_one_mib_limit_is_utf8_bytes_and_never_silently_truncates():
    engine = runtime.TopicEngine("missing")
    with pytest.raises(ValueError, match="1 MiB"):
        engine.classify("汉" * (runtime.MAX_TEXT_BYTES // 3 + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        engine.predict_raw("a" * (runtime.MAX_TEXT_BYTES + 1))


def _fake_assets(directory):
    head = {"kind": "independent_binary_logistic_regression", "classes": list(runtime.LABELS),
            "coef": [[0.0] * 512 for _ in runtime.LABELS], "intercept": [0.0] * len(runtime.LABELS)}
    files = {"encoder-fp32.onnx": b"not loaded in integrity tests", "tokenizer.json": b"{}",
             "head.json": json.dumps(head).encode()}
    for name, data in files.items():
        (directory / name).write_bytes(data)
    config = {"version": runtime.VERSION, "labels": list(runtime.LABELS), "pooling": "normalized_cls",
              "max_tokens": 256, "overlap_tokens": 32, "threshold": runtime.THRESHOLD,
              "tokenizer": {"do_lower_case": True},
              "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")


def test_asset_hashes_and_configuration_fail_closed(tmp_path):
    _fake_assets(tmp_path)
    config, paths, head = runtime.validate_assets(tmp_path)
    assert set(paths) == set(runtime.ASSET_NAMES)
    assert head["classes"] == list(runtime.LABELS)
    (tmp_path / "encoder-fp32.onnx").write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="校验失败"):
        runtime.validate_assets(tmp_path)
    (tmp_path / "encoder-fp32.onnx").unlink()
    with pytest.raises(RuntimeError, match="缺失"):
        runtime.validate_assets(tmp_path)


def test_source_and_frozen_asset_locations(monkeypatch, tmp_path):
    assert runtime.models_directory().parts[-2:] == ("assets", "classification-models")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert runtime.models_directory() == tmp_path / "assets" / "classification-models"


def test_asset_label_order_and_threshold_cannot_drift(tmp_path):
    _fake_assets(tmp_path)
    path = tmp_path / "config.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    changed = dict(original, threshold=.5)
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(RuntimeError):
        runtime.validate_assets(tmp_path)
    changed = dict(original, labels=list(reversed(runtime.LABELS)))
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(RuntimeError):
        runtime.validate_assets(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows parent process identity")
def test_owner_guard_rejects_reused_pid_creation_identity(tmp_path):
    from snapvault.ocr_runtime import current_process_identity
    identity = current_process_identity()
    identity["created"] += 1
    (tmp_path / "owner.json").write_text(json.dumps(identity), encoding="utf-8")
    with pytest.raises(RuntimeError, match="owner changed"):
        runtime._OwnerGuard(tmp_path)


def test_long_text_windows_preserve_tail_with_special_tokens():
    import numpy as np
    from tokenizers import Tokenizer

    engine = runtime.TopicEngine()
    engine._np = np
    engine._config = {"tokenizer": {"do_lower_case": True}}
    engine._coef = np.ones((len(runtime.LABELS), 1))
    engine._intercept = np.zeros(len(runtime.LABELS))
    tokenizer = Tokenizer.from_file(str(runtime.models_directory() / "tokenizer.json"))
    tokenizer.no_padding()
    tokenizer.enable_truncation(max_length=256, stride=32)
    engine._tokenizer = tokenizer
    chunks = []

    class FakeSession:
        def run(self, outputs, features):
            ids = features["input_ids"][0].tolist()
            chunks.append(ids)
            return [np.asarray([[float(len(chunks))]])]

    engine._session = FakeSession()
    text = "这是一份很长的技术资料。" * 100 + "末尾是一张演唱会的门票"
    scores = engine.predict_raw(text)
    assert len(chunks) > 2 and all(len(chunk) <= 256 for chunk in chunks)
    assert all(chunk[0] == tokenizer.token_to_id("[CLS]") and chunk[-1] == tokenizer.token_to_id("[SEP]") for chunk in chunks)
    untruncated = Tokenizer.from_file(str(runtime.models_directory() / "tokenizer.json"))
    untruncated.no_padding(); untruncated.no_truncation()
    expected = untruncated.encode(text.lower(), add_special_tokens=False).ids
    reconstructed = chunks[0][1:-1]
    for chunk in chunks[1:]:
        reconstructed.extend(chunk[33:-1])
    assert reconstructed == expected
    assert scores["tech"] > .95  # maximum, rather than first-window result


def test_batch_errors_do_not_include_input_or_native_exception_details():
    class FailingEngine:
        def classify(self, text, kind):
            raise RuntimeError("private-content C:/Users/private/model.onnx")
    result = runtime.process_request({"request_id": "a", "items": [{"key": 7, "text": "private-content"}]}, FailingEngine())
    assert result["request_id"] == "a" and result["results"][0]["key"] == 7
    assert "private-content" not in json.dumps(result) and "model.onnx" not in json.dumps(result)
    with pytest.raises(ValueError):
        runtime.process_request({"request_id": "a", "items": [{}] * 17}, FailingEngine())


def test_file_worker_without_stdio_processes_request_and_exits_idle(tmp_path, monkeypatch):
    request = {"request_id": "local-only", "items": [{"key": 1, "text": "好的", "kind": "text"}]}
    (tmp_path / "request.json").write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert runtime.worker_main(["--classify-worker", "--channel-dir", str(tmp_path), "--idle-seconds", ".15"]) == 0
    result = json.loads((tmp_path / "response.json").read_text(encoding="utf-8"))
    assert result["request_id"] == "local-only"
    assert result["results"][0]["result"]["tags"] == []
    assert (tmp_path / "identity.json").is_file()
    assert not (tmp_path / "request.json").exists()
