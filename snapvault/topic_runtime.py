"""Offline topic classification, loaded only by the disposable worker process.

The encoder already exports normalized CLS embeddings. Long text is tokenized
with overlapping 256-token windows and every window is evaluated. Per-label
maximum scores preserve topics appearing near the end of OCR; the final two-tag
cap limits, but cannot eliminate, incidental topics in long mixed documents.
No inference dependency or model is imported when this module is imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import unicodedata
from urllib.parse import unquote, urlsplit

VERSION = "setfit-bge-v1"
LABELS = ("tech", "learning", "design", "receipts", "tasks", "chat", "media")
THRESHOLD = 0.425
MAX_TEXT_BYTES = 1024 * 1024
MAX_BATCH = 16
# Training requests carry capped texts of every manual item plus anchors.
MAX_REQUEST_BYTES = 32 * 1024 * 1024
ASSET_NAMES = ("encoder-fp32.onnx", "head.json", "tokenizer.json")
_URL = re.compile(r"https?://[^\s<>]+", re.I)
_ACKNOWLEDGEMENTS = {
    "好", "好的", "好吧", "好嘞", "嗯", "嗯嗯", "哦", "哦哦", "行", "可以", "收到", "谢谢", "哈哈",
    "哈哈哈", "知道了", "明白了", "先这样吧", "后面再看", "行那就这么搞", "这个还行", "可以这个还行",
    "ok", "okay", "yes", "no", "sure", "thanks", "asdf", "asdfghjkl", "qwerty", "qwertyuiop",
}


def models_directory():
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return root / "assets" / "classification-models"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_assets(directory=None):
    """Fail closed on missing, corrupt or incompatible local model assets."""
    directory = Path(directory) if directory is not None else models_directory()
    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        if (config["version"] != VERSION or tuple(config["labels"]) != LABELS
                or config["pooling"] != "normalized_cls" or config["max_tokens"] != 256
                or config["threshold"] != THRESHOLD or config["overlap_tokens"] != 32
                or config["tokenizer"].get("do_lower_case") is not True):
            raise ValueError("unsupported configuration")
        paths = {}
        for filename in ASSET_NAMES:
            path = directory / filename
            expected = config["sha256"][filename]
            if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise ValueError("invalid hash")
            if _sha256(path) != expected:
                raise ValueError("asset hash mismatch")
            paths[filename] = path
        head = json.loads(paths["head.json"].read_text(encoding="utf-8"))
        if (head["kind"] != "independent_binary_logistic_regression"
                or tuple(head["classes"]) != LABELS or len(head["coef"]) != len(LABELS)
                or len(head["intercept"]) != len(LABELS)
                or not all(len(row) == 512 for row in head["coef"])):
            raise ValueError("invalid classifier head")
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for row in [*head["coef"], head["intercept"]] for value in row):
            raise ValueError("invalid classifier weights")
        return config, paths, head
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("离线分类模型缺失或校验失败，请重新安装完整程序。") from exc


def _checked_text(text):
    if not isinstance(text, str):
        raise ValueError("分类内容格式无效。")
    if len(text) > MAX_TEXT_BYTES or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("内容超过 1 MiB，暂未自动分类。")
    # Keep the model input identical to the frozen experiment's strip/lower
    # path. NFKC here would change Chinese full-width punctuation and scores.
    return text.strip()


def semantic_text(text, kind="text"):
    """Reject clear non-content; URL domains alone are never topic evidence."""
    text = _checked_text(text)
    if _URL.fullmatch(text):
        try:
            parts = urlsplit(text)
            path = unquote(parts.path + " " + parts.fragment)
        except ValueError:
            return ""
        words = re.findall(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", path)
        generic = {"www", "com", "html", "htm", "php", "asp", "aspx", "index", "article", "articles",
                   "detail", "details", "share", "watch", "video", "item", "items", "post", "posts",
                   "page", "pages", "view", "download", "redirect", "url", "https", "http"}
        words = [word for word in words if word.casefold() not in generic]
        # Random IDs split into letters are not titles. A whole path segment must
        # itself contain a word rather than a mixed UUID / hexadecimal identifier.
        meaningful = []
        for segment in re.split(r"[/\s_\-.]+", path):
            if re.fullmatch(r"[A-Za-z]{4,}|[\u3400-\u9fff]{2,}", segment) and segment.casefold() not in generic:
                if not re.fullmatch(r"[a-fA-F]{8,}", segment):
                    meaningful.append(segment)
        return " ".join(meaningful) if words and meaningful else ""
    normalized = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE).casefold()
    if not compact or compact in _ACKNOWLEDGEMENTS:
        return ""
    if not re.search(r"[A-Za-z\u3400-\u9fff]", text):
        return ""
    if re.fullmatch(r"[0-9a-fA-F\s\-{}:]+", text) and len(compact) >= 12:
        return ""
    if re.fullmatch(r"[\d\s年月日时分秒周星期TtZz:./+\-]+", normalized):
        return ""
    if len(compact) < 2 or (len(set(compact)) == 1 and len(compact) > 2):
        return ""
    return text


def _strong_evidence(text):
    """Only structurally clear evidence supplements model predictions."""
    boost, suppress = set(), set()
    document = None
    if text.lstrip().startswith(("{", "[")):
        try:
            document = json.loads(text)
        except (ValueError, RecursionError):
            pass
    if isinstance(document, dict):
        keys = {str(key).casefold() for key in document}
        receipts = {"商品", "商品名称", "订单号", "订单编号", "实付金额", "金额", "物流单号", "收款方",
                    "order_id", "order_number", "product", "product_name", "amount", "tracking_number"}
        if len(keys & receipts) >= 2:
            boost.add("receipts")
            # JSON syntax is not development evidence by itself.
            suppress.add("tech")
    error = re.search(r"Traceback\s*\(most recent call last\)|\b(?:[A-Z]\w*(?:Error|Exception))\b|"
                      r"\b(?:connection|request|operation)\s+(?:timed?\s*out|refused|failed)\b|"
                      r"\bHTTP\s*(?:500|502|503|504)\b|\bat\s+[\w.]+\([^\n]+:\d+\)", text, re.I)
    if error:
        boost.add("tech")
        suppress.discard("tech")
        if not re.search(r"提醒|预约|待办|日程|截止|开会|会议|记得", text):
            suppress.add("tasks")
    if document is None:
        turns = re.findall(r"(?m)^\s*([\u3400-\u9fff]{1,6})\s*[：:]\s*([^\n]+)", text)
        fields = {"名称", "商品", "金额", "订单号", "订单编号", "收款方", "地址", "标题", "描述", "作者",
                  "时间", "日期", "状态", "错误", "端口", "服务器", "用户名", "密码", "配置", "语言"}
        speakers = {name for name, body in turns if name not in fields and not body.lstrip().startswith(('"', "'", "{"))}
        # Chinese field names and speakers share the same colon syntax. Merely
        # having Chinese prose after two colons is insufficient: require actual
        # conversational language in both turns, or explicit paired chat roles.
        natural = sum(bool(re.search(r"[吗呢吧呀啊？?！!你我您咱]|一起|几点|明天|今天|谢谢|没事|行了|可以|知道了", body))
                      for name, body in turns if name in speakers)
        if len(speakers) >= 2 and (natural >= 2 or {"我", "你"} <= speakers or {"我", "对方"} <= speakers):
            boost.add("chat")
    css = re.search(r"(?:[.#][\w-]+|:root|body|html)\s*\{[^}]{0,4000}(?:\b(?:color|background|font|display|margin|padding|border)[\w-]*|--[\w-]+)\s*:", text, re.I | re.S)
    if css:
        boost.update(("tech", "design"))
    return boost, suppress


def postprocess(text, scores, *, threshold=THRESHOLD, kind="text"):
    """Return at most two known labels; uncertainty stays untagged / 待整理."""
    content = semantic_text(text, kind)
    clean = {label: float(scores.get(label, 0.0)) for label in LABELS}
    if not all(math.isfinite(value) for value in clean.values()):
        raise ValueError("分类模型返回无效分数。")
    clean = {label: max(0.0, min(1.0, value)) for label, value in clean.items()}
    if not content:
        return {"tags": [], "scores": {label: 0.0 for label in LABELS}, "version": VERSION}
    boosted, suppressed = _strong_evidence(content)
    for label in suppressed:
        clean[label] = 0.0
    for label in boosted:
        clean[label] = max(clean[label], 0.98)
    ordered = sorted(LABELS, key=lambda label: (-clean[label], LABELS.index(label)))
    return {"tags": [label for label in ordered if clean[label] >= threshold][:2],
            "scores": {label: round(clean[label], 6) for label in LABELS}, "version": VERSION}


PERSONAL_HEAD_KIND = "personal_logistic_regression_v1"
MIN_TRAIN_EXAMPLES = 10
MAX_TRAIN_ITEMS = 1200
MAX_TRAIN_CHARS = 4000


def base_head_sha256(directory=None):
    directory = Path(directory) if directory is not None else models_directory()
    return _sha256(directory / "head.json")


def _sigmoid(np, values):
    return np.exp(-np.logaddexp(0.0, -values))


def load_personal_head(path, base_sha256):
    """Validate a locally trained head; it must extend this exact shipped head."""
    import numpy as np
    raw = Path(path).read_bytes()
    head = json.loads(raw.decode("utf-8"))
    if (not isinstance(head, dict) or head.get("kind") != PERSONAL_HEAD_KIND
            or tuple(head.get("classes", ())) != LABELS or head.get("base_head_sha256") != base_sha256):
        raise ValueError("personal head does not match the shipped model")
    coef = np.asarray(head["coef"], dtype=np.float64)
    intercept = np.asarray(head["intercept"], dtype=np.float64)
    if coef.shape != (len(LABELS), 512) or intercept.shape != (len(LABELS),):
        raise ValueError("invalid personal head shape")
    if not (np.isfinite(coef).all() and np.isfinite(intercept).all()):
        raise ValueError("invalid personal head weights")
    return coef, intercept, hashlib.sha256(raw).hexdigest()[:8]


def fit_personal_head(np, base_coef, base_intercept, examples, anchors, *, l2=1.0, anchor_weight=2.0, iterations=25):
    """Adapt the linear head to manual decisions without forgetting the rest.

    examples: [(window embeddings k×512, tag set)] from manual decisions.
    anchors: [window embeddings] of automatically tagged items, whose current
    probabilities are kept as soft targets. Each label is an independent,
    L2-regularised logistic regression pulled toward the shipped weights, so
    a handful of corrections nudges rather than replaces the model. On the
    synthetic lab set these defaults kept items nobody corrected within about
    one percent of the shipped head, while about a dozen consistent
    corrections are enough to tag similar new items. Long text
    is scored by its best window at inference, so a positive example trains its
    best window and a negative example trains every window.
    """
    dimension = base_coef.shape[1]
    coef, intercept = base_coef.copy(), base_intercept.copy()
    for index, label in enumerate(LABELS):
        rows, targets, weights = [], [], []
        for windows, tags in examples:
            if label in tags:
                best = int(np.argmax(windows @ base_coef[index]))
                rows.append(windows[best]); targets.append(1.0); weights.append(1.0)
            else:
                rows.extend(windows); targets.extend([0.0] * len(windows)); weights.extend([1.0] * len(windows))
        for windows in anchors:
            probabilities = _sigmoid(np, windows @ base_coef[index] + base_intercept[index])
            share = anchor_weight / len(windows)
            rows.extend(windows); targets.extend(probabilities.tolist()); weights.extend([share] * len(windows))
        if not rows:
            continue
        features = np.hstack([np.asarray(rows, dtype=np.float64), np.ones((len(rows), 1))])
        target, weight = np.asarray(targets), np.asarray(weights)
        prior = np.concatenate([base_coef[index], [base_intercept[index]]])
        penalty = np.full(dimension + 1, float(l2))
        theta = prior.copy()
        for _ in range(iterations):
            probability = _sigmoid(np, features @ theta)
            gradient = features.T @ (weight * (probability - target)) + penalty * (theta - prior)
            curvature = weight * probability * (1.0 - probability)
            hessian = (features * curvature[:, None]).T @ features + np.diag(penalty)
            step = np.linalg.solve(hessian, gradient)
            theta -= step
            if float(np.max(np.abs(step))) < 1e-7:
                break
        coef[index], intercept[index] = theta[:-1], theta[-1]
    return coef, intercept


def window_tags(np, windows, coef, intercept, threshold=THRESHOLD):
    """Model-only tag set for one item's windows, with the production top-two cap."""
    scores = _sigmoid(np, windows @ coef.T + intercept).max(axis=0)
    ordered = sorted(range(len(LABELS)), key=lambda index: (-scores[index], index))
    return {LABELS[index] for index in ordered if scores[index] >= threshold}


class TopicEngine:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else models_directory()
        self._session = self._tokenizer = self._np = None
        self._config = self._coef = self._intercept = None
        self._base_coef = self._base_intercept = None
        self._requested_head = self._active_head = None
        self.version = VERSION
        self.head_error = False

    @property
    def loaded(self):
        return self._session is not None

    def _load(self):
        if self.loaded:
            return
        config, paths, head = validate_assets(self.directory)
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError("离线分类运行组件缺失，请重新安装完整程序。") from exc
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.enable_cpu_mem_arena = False
        options.log_severity_level = 3
        session = ort.InferenceSession(str(paths["encoder-fp32.onnx"]), sess_options=options,
                                       providers=["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(str(paths["tokenizer.json"]))
        tokenizer.no_padding()
        tokenizer.enable_truncation(max_length=config["max_tokens"], stride=config["overlap_tokens"],
                                    strategy="longest_first", direction="right")
        self._np, self._session, self._tokenizer, self._config = np, session, tokenizer, config
        self._coef = np.asarray(head["coef"], dtype=np.float64)
        self._intercept = np.asarray(head["intercept"], dtype=np.float64)
        self._base_coef, self._base_intercept = self._coef, self._intercept

    def select_head(self, path=None):
        """Use a personal head for later predictions, or None for the shipped one.

        Nothing is read until a prediction needs the model. A missing or
        invalid personal head falls back to the shipped head.
        """
        self._requested_head = str(path) if path else None

    def _ensure_head(self):
        requested = self._requested_head
        if requested is None:
            if self._active_head is not None:
                self._coef, self._intercept = self._base_coef, self._base_intercept
                self._active_head, self.version = None, VERSION
            return
        path = Path(requested)
        try:
            stat = path.stat()
            key = (requested, stat.st_mtime_ns, stat.st_size)
            if key == self._active_head:
                return
            coef, intercept, digest = load_personal_head(path, base_head_sha256(self.directory))
        except (OSError, ValueError, KeyError, TypeError):
            self._coef, self._intercept = self._base_coef, self._base_intercept
            self._active_head, self.version, self.head_error = None, VERSION, True
            return
        self._coef, self._intercept = coef, intercept
        self._active_head, self.version, self.head_error = key, f"{VERSION}+personal-{digest}", False

    def _model_text(self, text):
        if self._config["tokenizer"].get("do_lower_case", True):
            return text.lower()
        return text

    def _chunks(self, text):
        encoded = self._tokenizer.encode(self._model_text(text), add_special_tokens=True)
        # Overflow encodings include the BGE postprocessor's CLS / SEP tokens;
        # unlike slicing strings, they neither drop the tail nor split a token.
        for chunk in [encoded, *encoded.overflowing]:
            yield {"input_ids": self._np.asarray([chunk.ids], dtype=self._np.int64),
                   "attention_mask": self._np.asarray([chunk.attention_mask], dtype=self._np.int64),
                   "token_type_ids": self._np.asarray([chunk.type_ids], dtype=self._np.int64)}

    def embeddings(self, text):
        """Normalized CLS embedding of every overlapping token window (k×512)."""
        text = _checked_text(text)
        self._load()
        return self._np.asarray([self._session.run(["embeddings"], features)[0][0]
                                 for features in self._chunks(text)], dtype=self._np.float64)

    def predict_raw(self, text):
        """Model-only scores, before abstention and structural evidence rules."""
        text = _checked_text(text)
        if not text:
            return {label: 0.0 for label in LABELS}
        self._load()
        if self._base_coef is not None:
            self._ensure_head()
        scores = self._np.zeros(len(LABELS), dtype=self._np.float64)
        for features in self._chunks(text):
            vectors = self._session.run(["embeddings"], features)[0]
            logits = vectors @ self._coef.T + self._intercept
            probabilities = self._np.exp(-self._np.logaddexp(0.0, -logits))[0]
            scores = self._np.maximum(scores, probabilities)
        return {label: float(scores[index]) for index, label in enumerate(LABELS)}

    def classify(self, text, kind="text"):
        content = semantic_text(text, kind)
        scores = self.predict_raw(content) if content else {}
        result = postprocess(text, scores, threshold=THRESHOLD, kind=kind)
        if content:
            result["version"] = self.version
        return result

    def train(self, examples, anchors):
        """Fit a personal head; returns the head document and a short report."""
        self._load()
        np = self._np
        prepared, anchor_windows = [], []
        for item in examples[:MAX_TRAIN_ITEMS]:
            tags = set(item.get("tags", ()))
            if not tags <= set(LABELS):
                continue
            content = semantic_text(str(item.get("text", ""))[:MAX_TRAIN_CHARS], item.get("kind", "text"))
            if content:
                prepared.append((self.embeddings(content), tags))
        for item in anchors[:MAX_TRAIN_ITEMS]:
            content = semantic_text(str(item.get("text", ""))[:MAX_TRAIN_CHARS], item.get("kind", "text"))
            if content:
                anchor_windows.append(self.embeddings(content))
        if len(prepared) < MIN_TRAIN_EXAMPLES:
            raise ValueError("可用于训练的手动标签太少")
        base = self._base_coef, self._base_intercept
        # Honest estimate on the user's own decisions: every manual item is
        # predicted once by a head that never saw it (five folds).
        folds = 5
        cv_base = cv_personal = 0
        for fold in range(folds):
            held = prepared[fold::folds]
            seen = [example for index, example in enumerate(prepared) if index % folds != fold]
            if not held or not seen:
                continue
            fold_coef, fold_intercept = fit_personal_head(np, *base, seen, anchor_windows)
            cv_base += sum(window_tags(np, windows, *base) == tags for windows, tags in held)
            cv_personal += sum(window_tags(np, windows, fold_coef, fold_intercept) == tags for windows, tags in held)
        coef, intercept = fit_personal_head(np, *base, prepared, anchor_windows)
        changed = sum(window_tags(np, windows, coef, intercept) != window_tags(np, windows, *base)
                      for windows in anchor_windows)
        head = {"kind": PERSONAL_HEAD_KIND, "classes": list(LABELS),
                "base_head_sha256": base_head_sha256(self.directory),
                "coef": coef.round(8).tolist(), "intercept": intercept.round(8).tolist()}
        report = {"examples": len(prepared), "anchors": len(anchor_windows),
                  "cv_base_agree": int(cv_base), "cv_personal_agree": int(cv_personal),
                  "anchors_changed": int(changed)}
        return head, report


def _atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def process_train_request(request, engine):
    examples, anchors = request.get("examples"), request.get("anchors", [])
    if not isinstance(examples, list) or not isinstance(anchors, list):
        raise ValueError("训练任务格式无效。")
    try:
        head, report = engine.train(examples, anchors)
    except ValueError as exc:
        return {"request_id": request["request_id"], "error": str(exc)}
    except Exception:
        # Same rule as classification: never echo input or native details.
        return {"request_id": request["request_id"], "error": "本地训练失败，可稍后重试。"}
    return {"request_id": request["request_id"], "head": head, "report": report}


def process_request(request, engine):
    if not isinstance(request, dict) or not isinstance(request.get("request_id"), (str, int)):
        raise ValueError("分类任务格式无效。")
    if request.get("op") == "train":
        return process_train_request(request, engine)
    select_head = getattr(engine, "select_head", None)
    if select_head is not None:
        head = request.get("head")
        select_head(head if isinstance(head, str) else None)
    items = request.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH:
        raise ValueError("分类任务数量无效。")
    results = []
    for item in items:
        key = item.get("key") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict) or not isinstance(key, (str, int)):
                raise ValueError("invalid item")
            result = engine.classify(item.get("text"), kind=item.get("kind", "text"))
            results.append({"key": key, "result": result})
        except Exception:
            # Never persist input, arbitrary exception strings, filesystem paths
            # or native diagnostics in responses visible to the UI.
            results.append({"key": key, "error": "本地分类失败，可稍后重试。"})
    return {"request_id": request["request_id"], "results": results}


class _OwnerGuard:
    def __init__(self, channel):
        self.handle = self.kernel = None
        self.pid = None
        owner = channel / "owner.json"
        if not owner.is_file():
            return
        identity = json.loads(owner.read_text(encoding="utf-8"))
        self.pid = int(identity["pid"])
        if self.pid <= 0:
            raise ValueError("invalid owner")
        if os.name == "nt":
            from .ocr_runtime import _windows_kernel, _creation_time
            self.kernel = _windows_kernel()
            self.handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, self.pid)
            if not self.handle:
                raise RuntimeError("owner closed")
            if identity.get("created") is not None and _creation_time(self.kernel, self.handle) != identity["created"]:
                self.close()
                raise RuntimeError("owner changed")

    def alive(self):
        if self.pid is None:
            return True
        if self.kernel:
            return self.kernel.WaitForSingleObject(self.handle, 0) == 0x102
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def close(self):
        if self.kernel and self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def worker_main(argv=None):
    """Single-owner file channel; safe with PyInstaller --windowed streams."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--classify-worker", action="store_true", required=True)
    parser.add_argument("--channel-dir", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)
    channel = args.channel_dir.resolve()
    if not channel.is_dir() or not 0 < args.idle_seconds <= 3600:
        return 1
    from .ocr_runtime import write_worker_identity
    guard = None
    owner_stop, owner_thread = threading.Event(), None
    try:
        write_worker_identity(channel / "identity.json")
        guard = _OwnerGuard(channel)
        if guard.pid is not None:
            def watch_owner():
                # A long OCR document may take several windows to classify.
                # Do not leave it running until the next request after a GUI
                # crash; the exact Windows parent handle avoids PID reuse.
                while not owner_stop.wait(0.2):
                    if not guard.alive():
                        os._exit(2)
            owner_thread = threading.Thread(target=watch_owner, daemon=True, name="shiying-topic-owner")
            owner_thread.start()
        engine = TopicEngine()
        request_path, response_path = channel / "request.json", channel / "response.json"
        last_request, previous_id = time.monotonic(), None
        while guard.alive():
            if time.monotonic() - last_request >= args.idle_seconds:
                return 0
            if request_path.is_file() and not response_path.exists():
                try:
                    if request_path.stat().st_size > MAX_REQUEST_BYTES:
                        raise ValueError("oversized request")
                    request = json.loads(request_path.read_text(encoding="utf-8"))
                    request_path.unlink(missing_ok=True)
                    if request.get("request_id") == previous_id:
                        continue
                    result = process_request(request, engine)
                    previous_id = request["request_id"]
                    last_request = time.monotonic()
                    _atomic_json(response_path, result)
                except (OSError, ValueError, TypeError, AttributeError):
                    request_path.unlink(missing_ok=True)
                    return 1
            time.sleep(0.05)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return 1
    finally:
        owner_stop.set()
        if owner_thread is not None:
            owner_thread.join(timeout=0.5)
        if guard is not None:
            guard.close()


if __name__ == "__main__":
    raise SystemExit(worker_main())
