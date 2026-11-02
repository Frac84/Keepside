"""Offline OCR job runtime. Importing this module loads no native models."""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time


MODEL_SHA256 = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}
MAX_INPUT_EDGE = 4096


def models_directory():
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return root / "assets" / "ocr-models"


def validate_models(directory=None):
    directory = Path(directory) if directory is not None else models_directory()
    paths = {}
    for filename, expected in MODEL_SHA256.items():
        path = directory / filename
        if not path.is_file():
            raise RuntimeError(f"离线 OCR 模型缺失：{filename}。请重新运行安装或解压完整程序。")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError(f"离线 OCR 模型校验失败：{filename}。未开始识别，也不会联网下载。")
        paths[filename] = path
    return paths


def starts(length, size=1600, overlap=128):
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("无效的 OCR 分块参数")
    positions = [0]
    while positions[-1] + size < length:
        positions.append(min(positions[-1] + size - overlap, length - size))
    return positions


def tiles(image, size=1600, overlap=128):
    """Full-width vertical strips keep text lines intact with bounded area.

    Images up to size pixels wide use exactly the original vertical geometry.
    Wider images get shorter strips rather than cuts through horizontal text.
    Ownership is spatial, so equal text on distinct rows is never deduplicated.
    """
    width, height = image.size
    strip_height = min(size, max(1, size * size // width))
    strip_overlap = min(overlap, strip_height // 4)
    ys = starts(height, strip_height, strip_overlap)
    yb = [0] + [(a + strip_height + b) / 2 for a, b in zip(ys, ys[1:])] + [height]
    for yi, y in enumerate(ys):
        crop = image.crop((0, y, width, min(y + strip_height, height)))
        yield crop, (0, y), (0, yb[yi], width, yb[yi + 1])


def _rows(lines):
    rows = []
    for line in sorted(lines, key=lambda item: (item["y"], item["x"])):
        if rows and abs(line["y"] - statistics.mean(value["y"] for value in rows[-1])) <= max(3, line["h"] * 0.45):
            rows[-1].append(line)
        else:
            rows.append([line])
    return [sorted(row, key=lambda item: item["x"]) for row in rows]


def arrange(lines):
    """Sort geometry without deleting genuine repeated text at other positions."""
    return "\n".join(" ".join(value["text"] for value in row) for row in _rows(lines))


def layout(lines):
    """Reading-order line boxes in original image pixels, matching arrange()."""
    return [{"x": line["x"], "y": line["top"], "w": line["w"], "h": line["h"], "text": line["text"]}
            for row in _rows(lines) for line in row]


def _no_download(cls, *args, **kwargs):
    raise RuntimeError("离线 OCR 已禁用联网下载；请检查随程序附带的模型文件。")


class OcrEngine:
    """Load RapidOCR once, then recognize any number of images in this process."""

    def __init__(self):
        self._engine = None
        self._image = self._image_ops = None

    def _load(self):
        if self._engine is not None:
            return
        paths = validate_models()
        # These heavy imports occur only in OCR worker processes, never in the GUI.
        try:
            from rapidocr import RapidOCR, ModelType, OCRVersion
            from rapidocr.utils.download_file import DownloadFile
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise RuntimeError("离线 OCR 运行组件缺失，请重新运行安装。") from exc
        DownloadFile._make_http_request = classmethod(_no_download)
        params = {
            "Det.model_type": ModelType.SMALL, "Rec.model_type": ModelType.SMALL,
            "Det.ocr_version": OCRVersion.PPOCRV6, "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_path": str(paths["PP-OCRv6_det_small.onnx"]),
            "Rec.model_path": str(paths["PP-OCRv6_rec_small.onnx"]),
            "Cls.model_path": str(paths["ch_ppocr_mobile_v2.0_cls_mobile.onnx"]),
            "Global.model_root_dir": str(models_directory()), "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
            "EngineConfig.onnxruntime.use_cuda": False, "EngineConfig.onnxruntime.use_dml": False,
            "Det.limit_type": "max", "Det.limit_side_len": 1600,
        }
        self._image, self._image_ops = Image, ImageOps
        self._engine = RapidOCR(params=params)

    def recognize(self, path):
        """Return (text, line boxes) for one image."""
        self._load()
        Image, ImageOps, engine = self._image, self._image_ops, self._engine
        with Image.open(path) as source:
            if source.width * source.height > 80_000_000:
                raise ValueError("图片超过 8000 万像素，未开始识别。")
            original = ImageOps.exif_transpose(source) if source.getexif().get(274, 1) != 1 else source
            try:
                return self._recognize_tiles(original, engine)
            finally:
                if original is not source:
                    original.close()

    def decode_codes(self, path):
        """QR code payloads in an image; a failed scan returns [].

        Tall screenshots are scanned in overlapping full-width strips so a
        small code is not shrunk away; very large inputs are scaled down.
        """
        try:
            import cv2
            import numpy as np
        except ImportError:
            return []
        try:
            image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        except (OSError, ValueError, cv2.error):
            return []
        if image is None:
            return []
        height, width = image.shape[:2]
        if width > MAX_INPUT_EDGE:
            scale = MAX_INPUT_EDGE / width
            image = cv2.resize(image, (MAX_INPUT_EDGE, max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
            height, width = image.shape[:2]
        strip = max(1, min(height, max(width * 2, 1200)))
        detector = cv2.QRCodeDetectorAruco() if hasattr(cv2, "QRCodeDetectorAruco") else cv2.QRCodeDetector()
        payloads = []
        for top in starts(height, strip, strip // 4) if height > strip else [0]:
            try:
                found, texts, _points, _straight = detector.detectAndDecodeMulti(image[top:top + strip])
            except cv2.error:
                continue
            for text in texts if found else ():
                if text and text not in payloads:
                    payloads.append(text[:2000])
        return payloads[:20]

    def _recognize_tiles(self, original, engine):
        Image = self._image
        all_lines = []
        for crop, (x, y), owner in tiles(original):
            prepared = resized = None
            try:
                if crop.mode in ("RGBA", "LA") or "transparency" in crop.info:
                    rgba = crop.convert("RGBA")
                    prepared = Image.new("RGB", crop.size, "white")
                    prepared.paste(rgba, mask=rgba.getchannel("A"))
                    rgba.close()
                else:
                    prepared = crop.convert("RGB")
                input_scale = 1.0
                if max(prepared.size) > MAX_INPUT_EDGE:
                    input_scale = MAX_INPUT_EDGE / max(prepared.size)
                elif min(prepared.size) < 320 and max(prepared.size) < 1000:
                    input_scale = min(2.0, 1600 / max(prepared.size))
                if input_scale != 1.0:
                    resized = prepared.resize((max(1, round(prepared.width * input_scale)), max(1, round(prepared.height * input_scale))), Image.Resampling.LANCZOS)
                output = engine(resized if resized is not None else prepared)
                if output.boxes is None or not output.txts:
                    continue
                for box, text in zip(output.boxes, output.txts):
                    left = float(box[:, 0].min()) / input_scale + x
                    right = float(box[:, 0].max()) / input_scale + x
                    top = float(box[:, 1].min()) / input_scale + y
                    bottom = float(box[:, 1].max()) / input_scale + y
                    cx, cy = (left + right) / 2, (top + bottom) / 2
                    if owner[0] <= cx < owner[2] and owner[1] <= cy < owner[3]:
                        all_lines.append({"x": left, "y": cy, "top": top, "w": right - left,
                                          "h": bottom - top, "text": text})
            finally:
                if resized is not None:
                    resized.close()
                if prepared is not None:
                    prepared.close()
                crop.close()
        return arrange(all_lines), layout(all_lines)


def recognize_layout(path):
    """One-shot recognition with a fresh engine: (text, line boxes)."""
    return OcrEngine().recognize(path)


def recognize_image(path):
    return recognize_layout(path)[0]


@lru_cache(maxsize=1)
def _windows_kernel():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _creation_time(kernel, handle):
    import ctypes
    from ctypes import wintypes
    values = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
        return None
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def current_process_identity():
    created = None
    if os.name == "nt":
        kernel = _windows_kernel()
        created = _creation_time(kernel, kernel.GetCurrentProcess())
    return {"pid": os.getpid(), "created": created}


def write_worker_identity(path):
    Path(path).write_text(json.dumps(current_process_identity()), encoding="utf-8")


def stop_worker_identity(path):
    """Stop only this job's verified real Windows interpreter, not a reused PID."""
    if os.name != "nt":
        return
    try:
        identity = json.loads(Path(path).read_text(encoding="utf-8"))
        pid, created = identity["pid"], identity["created"]
        if not isinstance(pid, int) or pid <= 0 or pid == os.getpid() or not isinstance(created, int):
            return
    except (OSError, ValueError, KeyError, TypeError):
        return
    kernel = _windows_kernel()
    handle = kernel.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, pid)
    if not handle:
        return
    try:
        if _creation_time(kernel, handle) != created:
            return
        if kernel.WaitForSingleObject(handle, 1500) == 0x102:
            kernel.TerminateProcess(handle, 2)
            kernel.WaitForSingleObject(handle, 1500)
    finally:
        kernel.CloseHandle(handle)


def watch_owner(parent_pid, lifetime, parent_created=None):
    """An exact parent handle and a marker cover GUI exit and cancellation."""
    lifetime = Path(lifetime)
    if parent_pid <= 0 or not lifetime.is_file():
        raise RuntimeError("识别任务已取消。")
    if os.name == "nt":
        kernel = _windows_kernel()
        parent = kernel.OpenProcess(0x00100000 | 0x1000, False, parent_pid)
        if not parent:
            raise RuntimeError("识别任务所属窗口已关闭。")
        if parent_created is not None and _creation_time(kernel, parent) != parent_created:
            kernel.CloseHandle(parent)
            raise RuntimeError("识别任务所属窗口已关闭。")

        def watch():
            try:
                while kernel.WaitForSingleObject(parent, 200) == 0x102:
                    if not lifetime.is_file():
                        os._exit(2)
                os._exit(2)
            finally:
                kernel.CloseHandle(parent)
    else:
        def watch():
            while lifetime.is_file():
                try:
                    os.kill(parent_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.2)
            os._exit(2)
    threading.Thread(target=watch, daemon=True, name="shiying-ocr-owner").start()


def worker_main(argv=None):
    # Frozen --windowed jobs can have None streams; model logging must still work.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ocr-job", type=Path, required=True)
    args = parser.parse_args(argv)
    output = None
    try:
        job = json.loads(args.ocr_job.read_text(encoding="utf-8"))
        output = Path(job["output"])
        watch_owner(int(job["parent_pid"]), job["lifetime"], job.get("parent_created"))
        write_worker_identity(job["identity"])
        path = Path(job["input"])
        if not path.is_file():
            raise FileNotFoundError("待识别图片不存在。")
        started = time.monotonic()
        text = recognize_image(path)
        result = {"ok": True, "text": text, "seconds": time.monotonic() - started}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    if output is None:
        return 1
    result["diagnostics"] = {
        "loaded_qt_modules": [name for name in sys.modules if name == "PySide6" or name.startswith("PySide6.")],
        "loaded_winrt_modules": [name for name in sys.modules if name == "winrt" or name.startswith("winrt.")],
        "worker_pid": os.getpid(),
    }
    try:
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, output)
    except OSError:
        return 1
    return 0 if result.get("ok") else 1


MAX_REQUEST_BYTES = 64 * 1024


def _atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def serve_main(argv=None):
    """Long-lived OCR process: one model load for a whole queue, idle exit.

    Uses the same single-owner file channel as the classification worker. The
    process exits when its owner disappears, the owner marker is removed, it
    has been idle for --idle-seconds, or after --max-jobs images, so a burst of
    work shares one model load while memory is still returned afterwards.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ocr-worker", action="store_true", required=True)
    parser.add_argument("--channel-dir", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=float, default=20.0)
    parser.add_argument("--max-jobs", type=int, default=40)
    args = parser.parse_args(argv)
    channel = args.channel_dir.resolve()
    if not channel.is_dir() or not 0 < args.idle_seconds <= 3600 or not 1 <= args.max_jobs <= 10000:
        return 1
    from .topic_runtime import _OwnerGuard
    guard = None
    owner_stop, owner_thread = threading.Event(), None
    owner_marker = channel / "owner.json"
    try:
        write_worker_identity(channel / "identity.json")
        guard = _OwnerGuard(channel)
        if guard.pid is not None:
            def watch():
                # A long image can take seconds; leave immediately if the GUI
                # exits or cancels, without waiting for the current job.
                while not owner_stop.wait(0.2):
                    if not guard.alive() or not owner_marker.is_file():
                        os._exit(2)
            owner_thread = threading.Thread(target=watch, daemon=True, name="shiying-ocr-owner")
            owner_thread.start()
        engine = OcrEngine()
        request_path, response_path = channel / "request.json", channel / "response.json"
        last_request, previous_id, completed = time.monotonic(), None, 0
        while guard.alive() and owner_marker.is_file():
            if time.monotonic() - last_request >= args.idle_seconds or completed >= args.max_jobs:
                return 0
            if request_path.is_file() and not response_path.exists():
                try:
                    if request_path.stat().st_size > MAX_REQUEST_BYTES:
                        raise ValueError("oversized request")
                    request = json.loads(request_path.read_text(encoding="utf-8"))
                    request_path.unlink(missing_ok=True)
                    if not isinstance(request, dict) or not isinstance(request.get("request_id"), str):
                        raise ValueError("invalid request")
                    if request["request_id"] == previous_id:
                        continue
                except (OSError, ValueError, TypeError):
                    request_path.unlink(missing_ok=True)
                    return 1
                started = time.monotonic()
                try:
                    path = Path(request["input"])
                    if not path.is_file():
                        raise FileNotFoundError("待识别图片不存在。")
                    text, lines = engine.recognize(path)
                    decode = getattr(engine, "decode_codes", None)
                    try:
                        codes = decode(path) if decode else []
                    except Exception:
                        codes = []  # QR scanning is optional; never fail OCR over it.
                    result = {"ok": True, "text": text, "lines": lines, "codes": codes}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                result.update(request_id=request["request_id"], seconds=time.monotonic() - started)
                previous_id = request["request_id"]
                completed += 1
                last_request = time.monotonic()
                try:
                    _atomic_json(response_path, result)
                except OSError:
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
