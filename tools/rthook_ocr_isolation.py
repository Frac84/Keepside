"""Keep the frozen OCR worker free of Qt imports before the entrypoint runs.

PyInstaller's standard PySide6 runtime hook embeds a qt.conf resource by
importing QtCore. Custom runtime hooks run first, so the private OCR process can
skip just that operation. The GUI process still uses the standard hook intact.
"""
import sys


def _isolate_ocr_runtime():
    if not {"--ocr-job", "--ocr-worker", "--classify-worker"}.intersection(sys.argv[1:]):
        return

    # This helper module itself imports only the standard library. The other
    # operations in pyi_rth_pyside6 only prepare environment variables.
    from _pyi_rth_utils import qt

    qt.create_embedded_qt_conf = lambda *_args, **_kwargs: None


_isolate_ocr_runtime()
del _isolate_ocr_runtime
