"""Exercise the real PyInstaller PySide6 hook without importing Qt."""
from pathlib import Path
import runpy
import sys
import types

import pytest


ISOLATION_HOOK = Path(__file__).resolve().parents[1] / "tools" / "rthook_ocr_isolation.py"


def _qt_helpers(monkeypatch):
    calls = []
    qt = types.ModuleType("_pyi_rth_utils.qt")
    qt.ensure_single_qt_bindings_package = lambda name: calls.append(("ensure", name))

    def create_conf(*args):
        calls.append(("import_qt_core", args))

    qt.create_embedded_qt_conf = create_conf
    helpers = types.ModuleType("_pyi_rth_utils")
    helpers.qt = qt
    helpers.is_macos_app_bundle = False
    helpers.prepend_path_to_environment_variable = lambda *_args: None
    monkeypatch.setitem(sys.modules, "_pyi_rth_utils", helpers)
    monkeypatch.setitem(sys.modules, "_pyi_rth_utils.qt", qt)
    return qt, calls


def _standard_pyside_hook():
    pyinstaller = pytest.importorskip("PyInstaller")
    return Path(pyinstaller.__file__).parent / "hooks" / "rthooks" / "pyi_rth_pyside6.py"


@pytest.mark.parametrize("args,is_worker", [
    (["shiying.exe", "--ocr-job", "job.json"], True),
    (["shiying.exe", "--minimized", "--ocr-job", "job.json"], True),
    (["shiying.exe", "--classify-worker", "--channel-dir", "private"], True),
    (["shiying.exe"], False),
    (["shiying.exe", "--minimized"], False),
])
def test_isolation_precedes_standard_qt_hook(monkeypatch, tmp_path, args, is_worker):
    qt, calls = _qt_helpers(monkeypatch)
    original = qt.create_embedded_qt_conf
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    # Restore variables modified by the real runtime hook after each test.
    monkeypatch.setenv("QT_PLUGIN_PATH", "before")
    monkeypatch.setenv("QML2_IMPORT_PATH", "before")
    runpy.run_path(str(ISOLATION_HOOK))
    runpy.run_path(str(_standard_pyside_hook()))
    assert (qt.create_embedded_qt_conf is not original) is is_worker
    assert sum(call[0] == "import_qt_core" for call in calls) == (0 if is_worker else 1)
    assert calls[0] == ("ensure", "PySide6")


def test_normal_startup_does_not_import_private_qt_helpers(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["shiying.exe", "--data-dir", "library"])
    monkeypatch.setitem(sys.modules, "_pyi_rth_utils", None)
    runpy.run_path(str(ISOLATION_HOOK))
