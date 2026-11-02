from .i18n import tr
import io
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from PySide6.QtCore import QThread, Signal

from . import ocr


@dataclass
class TransientOcrJob:
    tag: object
    image: object


class OcrWorker(QThread):
    changed = Signal(str)
    availability = Signal(object, str)
    transient = Signal(object, object, str)

    def __init__(self, vault):
        super().__init__()
        self.vault = vault
        self.jobs = queue.Queue()
        self.stopping = threading.Event()
        self.queued = set()
        self.guard = threading.Lock()

    def enqueue(self, key):
        with self.guard:
            if key not in self.queued and not self.stopping.is_set():
                self.queued.add(key)
                self.jobs.put(key)

    def enqueue_image(self, image, tag):
        """Recognize a final screenshot without creating any vault record."""
        token = ("transient", tag)
        with self.guard:
            if image.isNull() or token in self.queued or self.stopping.is_set():
                return False
            self.queued.add(token)
            self.jobs.put(TransientOcrJob(tag, image.copy()))
            return True

    def _recognize_transient(self, session, job):
        result, error = {}, ""
        try:
            with TemporaryDirectory(prefix="SnapVaultOCR-") as directory:
                path = Path(directory) / "screenshot.png"
                if not job.image.save(str(path), "PNG"):
                    raise OSError(tr('无法创建本地识字临时图片'))
                result = session.recognize(path, stop_event=self.stopping)
                if self.stopping.is_set():
                    raise ocr.OcrCancelled(tr('识别任务已取消。'))
        except ocr.OcrCancelled:
            error = tr('识别已取消。')
        except Exception as exc:
            error = str(exc)
        finally:
            with self.guard:
                self.queued.discard(("transient", job.tag))
        if not self.stopping.is_set():
            self.transient.emit(job.tag, result, error)

    def run(self):
        try:
            ocr.check_availability()
            self.availability.emit(ocr.languages(), "")
        except Exception as exc:
            self.availability.emit([], str(exc))
        # One process serves a burst of images and leaves after 20 idle seconds.
        session = ocr.OcrSession()
        try:
            while not self.stopping.is_set():
                try:
                    key = self.jobs.get(timeout=0.2)
                except queue.Empty:
                    continue
                if isinstance(key, TransientOcrJob):
                    self._recognize_transient(session, key)
                    continue
                try:
                    row = self.vault.get(key)
                    if row and row.get("item_type", "image") == "image":
                        result = session.recognize(self.vault.images / row["filename"], stop_event=self.stopping)
                        content = result["text"]
                        if self.stopping.is_set():
                            raise ocr.OcrCancelled(tr('识别任务已取消。'))
                        if not content.strip() and row.get("ocr_text", "").strip():
                            raise RuntimeError(tr('本次未识别到文字，已保留已有识别结果。'))
                        self.vault.finish_ocr(key, content, lines=result.get("lines"), codes=result.get("codes", []))
                except ocr.OcrCancelled:
                    # Leave unfinished work for next launch; cancellation is not a
                    # reason to replace recognized text or its search index.
                    pass
                except Exception as exc:
                    self.vault.fail_ocr(key, str(exc))
                finally:
                    with self.guard:
                        self.queued.discard(key)
                    self.changed.emit(key)
        finally:
            session.close()


class CaptureWorker(QThread):
    saved = Signal(str, bool)
    # Emitted in addition to saved when a job carried a tag, so a caller can
    # tell which of its own requests produced which record.
    tagged = Signal(object, str, bool)
    failed = Signal(str)
    cleaned = Signal(int)
    hashed = Signal(int)

    def __init__(self, vault):
        super().__init__()
        self.vault = vault
        self.jobs = queue.Queue(maxsize=64)

    def submit(self, kind, value=None, tag=None):
        try:
            self.jobs.put_nowait((kind, value, tag))
            return True
        except queue.Full:
            self.failed.emit(tr('保存队列已满，这张图片未入库。请稍后重新复制或导入。'))
            return False

    def run(self):
        while True:
            kind, value, tag = self.jobs.get()
            if kind == "stop":
                return
            try:
                if kind == "cleanup":
                    self.cleaned.emit(self.vault.cleanup())
                    continue
                if kind == "hashes":
                    # Thumbnail hashes for screenshots saved before 1.6.0.
                    self.hashed.emit(self.vault.backfill_image_hashes())
                    continue
                if kind == "image":
                    from PySide6.QtGui import QImage
                    rgba = value.convertToFormat(QImage.Format.Format_RGBA8888)
                    image = Image.frombytes("RGBA", (rgba.width(), rgba.height()), bytes(rgba.constBits()), "raw", "RGBA", rgba.bytesPerLine())
                    key, created = self.vault.add(image)
                elif kind == "text":
                    key, created = self.vault.add_text(value)
                elif kind == "file":
                    with Image.open(Path(value)) as source:
                        key, created = self.vault.add(source)
                else:
                    raise ValueError(tr('不支持的保存任务类型'))
                self.saved.emit(key, created)
                if tag is not None:
                    self.tagged.emit(tag, key, created)
            except Exception as exc:
                self.failed.emit(tr('保存失败：{v0}', v0=exc))


class TopicWorker(QThread):
    """Queue IDs, read fresh snapshots and apply only unchanged nonmanual rows."""
    changed = Signal(str)
    failed = Signal(str)

    def __init__(self, vault):
        super().__init__()
        self.vault = vault
        self.jobs = queue.Queue()
        self.stopping = threading.Event()
        self.queued = set()
        self.dirty = set()
        self.guard = threading.Lock()
        self.client = None

    def enqueue(self, key):
        with self.guard:
            if self.stopping.is_set():
                return
            if key in self.queued:
                self.dirty.add(key)
            else:
                self.queued.add(key)
                self.jobs.put(key)

    def enqueue_pending(self, keys=None):
        for key in (self.vault.pending_classifications() if keys is None else keys):
            self.enqueue(key)

    def run(self):
        from .topics import TopicClient, ClassificationCancelled
        self.client = TopicClient()
        notified_failure = False
        try:
            while not self.stopping.is_set():
                try:
                    first = self.jobs.get(timeout=0.2)
                except queue.Empty:
                    continue
                keys = [first]
                self.stopping.wait(0.12)
                while len(keys) < 16:
                    try:
                        keys.append(self.jobs.get_nowait())
                    except queue.Empty:
                        break
                snapshots = {}
                try:
                    for key in keys:
                        row = self.vault.get(key)
                        if not row or row["classification_status"] not in {"pending", "error"}:
                            continue
                        snapshot = self.vault.classification_input(key)
                        if snapshot:
                            snapshots[key] = snapshot
                    if not snapshots:
                        continue
                    # An enabled personal head is read by the worker itself.
                    self.client.head_path = self.vault.active_personal_head()
                    items = [{"key": key, "text": value["text"], "kind": value["kind"]} for key, value in snapshots.items()]
                    results = self.client.classify_batch(items, stop_event=self.stopping)
                    if self.stopping.is_set():
                        raise ClassificationCancelled(tr('分类已取消'))
                    received = set()
                    for entry in results:
                        if self.stopping.is_set():
                            raise ClassificationCancelled(tr('分类已取消'))
                        key = entry.get("key")
                        if key not in snapshots or key in received:
                            raise RuntimeError(tr('分类结果条目无效'))
                        received.add(key)
                        fingerprint = snapshots[key]["fingerprint"]
                        value = entry.get("result")
                        if isinstance(value, dict) and "tags" in value:
                            applied = self.vault.apply_classification(key, value["tags"], value.get("scores", {}), fingerprint,
                                                                      version=value.get("version", "setfit-bge-v1"))
                            if not applied:
                                latest = self.vault.classification_input(key)
                                if latest and latest["fingerprint"] != fingerprint:
                                    with self.guard:
                                        self.dirty.add(key)
                        else:
                            self.vault.fail_classification(key, tr('本地分类暂未完成，请重新分类。'), fingerprint)
                        self.changed.emit(key)
                    if received != set(snapshots):
                        raise RuntimeError(tr('本地分类结果缺少条目'))
                    notified_failure = False
                except ClassificationCancelled:
                    pass  # Leave pending for next launch; never write an empty result.
                except Exception:
                    # Never log contents, labels or exceptions that may quote input.
                    if not self.stopping.is_set():
                        for key, value in snapshots.items():
                            self.vault.fail_classification(key, tr('本地分类暂不可用，请重新分类。'), value["fingerprint"])
                            self.changed.emit(key)
                    if not notified_failure and not self.stopping.is_set():
                        self.failed.emit(tr('本地分类暂不可用，内容已保存，可从主题菜单重新分类。'))
                        notified_failure = True
                finally:
                    again = []
                    with self.guard:
                        for key in keys:
                            self.queued.discard(key)
                            if key in self.dirty:
                                self.dirty.discard(key)
                                again.append(key)
                    for key in again:
                        self.enqueue(key)
        finally:
            self.client.close()


class HeadTrainer(QThread):
    """Train a personal topic head in a private worker; the GUI decides on use."""
    trained = Signal(object)
    failed = Signal(str)

    def __init__(self, vault):
        super().__init__()
        self.vault = vault
        self.stopping = threading.Event()

    def run(self):
        from .topics import TopicClient, ClassificationCancelled
        client = TopicClient(idle_seconds=5)
        try:
            head, report = client.train(self.vault.training_examples(), self.vault.anchor_examples(),
                                        stop_event=self.stopping)
            if self.stopping.is_set():
                return
            self.vault.write_personal_head(head)
            self.trained.emit(report)
        except ClassificationCancelled:
            pass
        except ValueError as exc:
            self.failed.emit(str(exc))
        except Exception:
            # Never surface exceptions that could quote library content.
            self.failed.emit(tr('本地训练暂不可用，内容和标签均未改变。'))
        finally:
            client.close()
