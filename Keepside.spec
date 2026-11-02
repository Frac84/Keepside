# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas = [('assets/shiying.ico', 'assets'), ('snapvault/locales', 'snapvault/locales'), ('assets/shiying.png', 'assets'),
         ('assets/chevron-light.png', 'assets'), ('assets/chevron-dark.png', 'assets'),
         ('assets/ocr-models', 'assets/ocr-models'), ('assets/ocr-licenses', 'assets/ocr-licenses'),
         ('assets/classification-models', 'assets/classification-models')]
binaries = [('browser_engine/bin/SnapVaultBrowserEngine.exe', 'browser_engine')]
hiddenimports = ['snapvault.ocr_runtime', 'snapvault.topic_runtime', 'tokenizers']
tmp_ret = collect_all('rapidocr', include_py_files=False, exclude_datas=['models/*'])
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime', include_py_files=False)
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['tools/rthook_ocr_isolation.py'],
    excludes=['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtOpenGL', 'PySide6.QtPdf', 'PySide6.QtNetworkAuth',
              'winrt', 'torch', 'paddle', 'paddleocr', 'openvino', 'tensorrt', 'MNN', 'tkinter'],
    # Standard directory distribution: keep application bytecode as files.
    noarchive=True,
    optimize=0,
)
# OpenCV's FFmpeg video plugin (about 30 MB) is loaded only for video capture.
# OCR reads still images, so it is never needed by this application.
a.binaries = [entry for entry in a.binaries if not Path(entry[0]).name.lower().startswith("opencv_videoio_ffmpeg")]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Keepside',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version-info.txt',
    icon=['assets/shiying.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Keepside',
)
