"""Tiling, offline resources and worker protocol without real native inference."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from PIL import Image
import pytest

from snapvault import ocr_runtime as runtime


def test_models_are_checked_with_streamed_sha256(tmp_path, monkeypatch):
    contents = {"first.onnx": b"model one", "second.onnx": b"model two"}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    monkeypatch.setattr(runtime, "MODEL_SHA256", hashes)
    for name, value in contents.items():
        (tmp_path / name).write_bytes(value)
    assert set(runtime.validate_models(tmp_path)) == set(contents)
    (tmp_path / "first.onnx").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="校验失败"):
        runtime.validate_models(tmp_path)
    (tmp_path / "first.onnx").unlink()
    with pytest.raises(RuntimeError, match="缺失"):
        runtime.validate_models(tmp_path)


def test_frozen_and_source_model_locations(monkeypatch, tmp_path):
    assert runtime.models_directory().parts[-2:] == ("assets", "ocr-models")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert runtime.models_directory() == tmp_path / "assets" / "ocr-models"


def test_long_tiles_cover_source_once_by_ownership():
    image = Image.new("L", (800, 8200))
    crops = list(runtime.tiles(image))
    assert len(crops) == 6
    for _, (x, y), owner in crops:
        assert owner[0] >= x and owner[1] >= y
    for y in range(0, 8200, 13):
        assert sum(owner[0] <= 400 < owner[2] and owner[1] <= y < owner[3] for _, _, owner in crops) == 1
    for crop, _, _ in crops:
        assert max(crop.size) <= 1600
        crop.close()
    image.close()


def test_arrangement_keeps_repeated_lines_and_sorts_columns():
    rows = [
        {"x": 100, "y": 10, "h": 15, "text": "right"},
        {"x": 2, "y": 11, "h": 15, "text": "left"},
        {"x": 2, "y": 40, "h": 15, "text": "repeat"},
        {"x": 2, "y": 80, "h": 15, "text": "repeat"},
    ]
    assert runtime.arrange(rows) == "left right\nrepeat\nrepeat"


def test_wide_single_line_is_never_split_horizontally():
    image = Image.new("L", (2400, 300))
    pieces = list(runtime.tiles(image))
    assert len(pieces) == 1
    crop, offset, owner = pieces[0]
    assert crop.size == (2400, 300) and offset == (0, 0) and owner == (0, 0, 2400, 300)
    crop.close(); image.close()


def test_wide_long_image_limits_strip_area_and_has_disjoint_row_ownership():
    image = Image.new("L", (2400, 8200))
    pieces = list(runtime.tiles(image))
    assert len(pieces) > 6
    for crop, offset, owner in pieces:
        assert crop.width == 2400 and crop.width * crop.height <= 1600 * 1600
        assert offset[0] == 0 and owner[0] == 0 and owner[2] == 2400
    for y in range(0, 8200, 17):
        assert sum(owner[1] <= y < owner[3] for _, _, owner in pieces) == 1
    for crop, _, _ in pieces:
        crop.close()
    image.close()


def test_narrow_vertical_geometry_remains_identical():
    image = Image.new("L", (800, 8200))
    pieces = list(runtime.tiles(image))
    positions = runtime.starts(8200, 1600, 128)
    assert [offset[1] for _, offset, _ in pieces] == positions
    expected = [0] + [(a + 1600 + b) / 2 for a, b in zip(positions, positions[1:])] + [8200]
    assert [owner[1] for _, _, owner in pieces] + [pieces[-1][2][3]] == expected
    for crop, _, _ in pieces:
        crop.close()
    image.close()


class Column(list):
    def min(self): return min(self)
    def max(self): return max(self)


class Box:
    def __init__(self, top, bottom, left=10, right=150):
        self.points = [(left, top), (right, top), (right, bottom), (left, bottom)]
    def __getitem__(self, index):
        return Column(point[index[1]] for point in self.points)


def fake_native(monkeypatch, engine_class):
    package = ModuleType("rapidocr")
    package.RapidOCR = engine_class
    package.ModelType = SimpleNamespace(SMALL="small")
    package.OCRVersion = SimpleNamespace(PPOCRV6="v6")
    download = ModuleType("rapidocr.utils.download_file")
    download.DownloadFile = type("DownloadFile", (), {})
    monkeypatch.setitem(sys.modules, "rapidocr", package)
    monkeypatch.setitem(sys.modules, "rapidocr.utils.download_file", download)
    monkeypatch.setattr(runtime, "validate_models", lambda: {name: Path(name) for name in runtime.MODEL_SHA256})
    return download.DownloadFile


def test_bounded_engine_and_overlap_do_not_duplicate_or_drop_distinct_lines(tmp_path, monkeypatch):
    observed = []
    class Engine:
        def __init__(self, params):
            observed.append(params)
            self.calls = 0
        def __call__(self, image):
            self.calls += 1
            assert image.size == (1600, 1600)
            boxes = [Box(850, 950), Box(1050, 1150)] if self.calls == 1 else [Box(450, 550), Box(650, 750)]
            return SimpleNamespace(boxes=boxes, txts=["重复", "重复"])
    download = fake_native(monkeypatch, Engine)
    path = tmp_path / "long.png"
    Image.new("RGB", (1600, 2000), "white").save(path)
    assert runtime.recognize_image(path) == "重复\n重复"
    params = observed[0]
    assert params["Det.limit_type"] == "max" and params["Det.limit_side_len"] == 1600
    assert params["EngineConfig.onnxruntime.intra_op_num_threads"] == 4
    assert params["EngineConfig.onnxruntime.inter_op_num_threads"] == 1
    assert params["EngineConfig.onnxruntime.enable_cpu_mem_arena"] is False
    assert all(params[name] for name in ("Det.model_path", "Rec.model_path", "Cls.model_path"))
    with pytest.raises(RuntimeError, match="禁用联网"):
        download._make_http_request("unused")


def test_transparent_small_input_is_composited_and_upscaled_without_source_changes(tmp_path, monkeypatch):
    class Engine:
        def __init__(self, params): pass
        def __call__(self, image):
            assert image.mode == "RGB" and image.size == (640, 240)
            assert image.getpixel((10, 10)) == (255, 255, 255)
            return SimpleNamespace(boxes=None, txts=[])
    fake_native(monkeypatch, Engine)
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (320, 120), (0, 0, 0, 0)).save(path)
    before = path.read_bytes()
    assert runtime.recognize_image(path) == ""
    assert path.read_bytes() == before


def test_wide_inference_receives_an_intact_line_with_original_detector_bound(tmp_path, monkeypatch):
    observed = []
    class Engine:
        def __init__(self, params):
            assert params["Det.limit_side_len"] == 1600 and params["Det.limit_type"] == "max"
        def __call__(self, image):
            observed.append(image.size)
            return SimpleNamespace(boxes=[Box(40, 70, left=20, right=image.width - 20)], txts=["完整宽行"])
    fake_native(monkeypatch, Engine)
    path = tmp_path / "wide.png"
    Image.new("RGB", (2400, 300), "white").save(path)
    assert runtime.recognize_image(path) == "完整宽行"
    assert observed == [(2400, 300)]


def test_extreme_width_caps_inference_edge_without_horizontal_text_cuts(tmp_path, monkeypatch):
    observed = []
    class Engine:
        def __init__(self, params): pass
        def __call__(self, image):
            observed.append(image.size)
            assert max(image.size) <= runtime.MAX_INPUT_EDGE
            return SimpleNamespace(boxes=None, txts=[])
    fake_native(monkeypatch, Engine)
    path = tmp_path / "extreme.png"
    Image.new("RGB", (6000, 300), "white").save(path)
    assert runtime.recognize_image(path) == ""
    assert observed == [(4096, 205)]


@pytest.mark.parametrize("fail", [False, True])
def test_worker_json_protocol_reports_native_isolation(tmp_path, monkeypatch, fail):
    output, marker, image = tmp_path / "out.json", tmp_path / "alive", tmp_path / "image.png"
    marker.touch(); image.touch()
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"input": str(image), "output": str(output), "lifetime": str(marker),
        "identity": str(tmp_path / "worker.json"), "parent_pid": 123}), encoding="utf-8")
    monkeypatch.setattr(runtime, "watch_owner", lambda *args: None)
    monkeypatch.setattr(runtime, "write_worker_identity", lambda *args: None)
    def infer(path):
        if fail: raise RuntimeError("synthetic failure")
        return "合成文字"
    monkeypatch.setattr(runtime, "recognize_image", infer)
    assert runtime.worker_main(["--ocr-job", str(job)]) == int(fail)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["ok"] is not fail
    assert "loaded_qt_modules" in result["diagnostics"]
    assert "loaded_winrt_modules" in result["diagnostics"]
    assert result.get("text") == (None if fail else "合成文字")


def test_cancelled_marker_refuses_to_start_watchdog(tmp_path):
    with pytest.raises(RuntimeError, match="取消"):
        runtime.watch_owner(123, tmp_path / "missing")


@pytest.mark.skipif(os.name != "nt", reason="Windows PID identity protection")
@pytest.mark.parametrize("same_creation", [True, False])
def test_cancellation_never_terminates_a_reused_pid(tmp_path, monkeypatch, same_creation):
    identity = tmp_path / "worker.json"
    identity.write_text(json.dumps({"pid": 987654, "created": 100}), encoding="utf-8")
    calls = []
    class Kernel:
        def OpenProcess(self, flags, inherit, pid):
            assert pid == 987654
            return 123
        def WaitForSingleObject(self, handle, timeout):
            return 0x102
        def TerminateProcess(self, handle, code): calls.append((handle, code))
        def CloseHandle(self, handle): calls.append(("close", handle))
    monkeypatch.setattr(runtime, "_windows_kernel", lambda: Kernel())
    monkeypatch.setattr(runtime, "_creation_time", lambda *args: 100 if same_creation else 999)
    runtime.stop_worker_identity(identity)
    assert ((123, 2) in calls) is same_creation
    assert calls[-1] == ("close", 123)


@pytest.mark.skipif(os.name != "nt", reason="Windows owner identity protection")
def test_watchdog_refuses_reused_parent_pid_before_starting_thread(tmp_path, monkeypatch):
    marker = tmp_path / "alive"
    marker.touch()
    closed = []
    kernel = SimpleNamespace(OpenProcess=lambda *args: 321, CloseHandle=closed.append)
    monkeypatch.setattr(runtime, "_windows_kernel", lambda: kernel)
    monkeypatch.setattr(runtime, "_creation_time", lambda *args: 999)
    with pytest.raises(RuntimeError, match="关闭"):
        runtime.watch_owner(987654, marker, 100)
    assert closed == [321]
