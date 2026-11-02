# Keepside

A local-first Windows utility for capturing screenshots, collecting useful content, and keeping files close at hand.

## Features

- Capture a full display or select a region with resize handles, a magnifier, and precise pixel dimensions.
- Annotate screenshots with rectangles, ellipses, arrows, lines, pen strokes, highlights, text, numbered markers, pixelation, and opaque redaction.
- Select and move individual annotations, edit text inline, change colors and stroke widths, and undo or redo edits.
- Choose **Copy only** or **Copy and save**. Remember capture preferences and reuse the last confirmed region.
- Pin images to the desktop and compress images to a chosen file-size limit.
- Extract Chinese and English text locally with offline OCR.
- Keep searchable images, text, and links in a local library.
- Access recent-file paths and a file basket from a movable, edge-docking floating button. Large files stay in their original locations.
- Manually lock recent files and the file basket with Windows Hello.
- Switch between **English** and **Simplified Chinese** in Settings → Appearance. Save to restart safely; open image editors defer the switch until the next launch.
- Choose Windows-style light, dark, and frosted-glass themes.

Content, OCR results, file names, and custom labels are never translated by the language switch. OCR and tagging run locally. Existing Windows recent-item history and clipboard history are controlled separately by Windows.

## Run from source

Requires Windows 11 and Python 3.13. Python 3.12+ may work with compatible dependency wheels.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe tools\download_ocr_models.py
.venv\Scripts\python.exe main.py
```

The OCR downloader verifies SHA-256 against the upstream model manifest. Model licenses and provenance are retained under `assets/ocr-models`. Large model weights, generated binaries, and personal data are excluded from Git.

Semantic topic tagging is optional. Its custom offline encoder is not included in this source repository; the application disables that component when matching assets are unavailable. The remaining library, screenshot, OCR, file-basket, and pinning features remain available. See `assets/classification-models/MODEL-CARD.md` for the model description and `config.json` for expected hashes.

Browser tab enhancements use an original GPL-3.0 AutoIt engine. To build it, install the official [AutoIt tools](https://www.autoitscript.com/site/autoit/downloads/) and run `browser_engine/build-native.ps1` with `-AutoItRoot` pointing to the portable installation. If the engine is unavailable, the rest of Keepside remains usable.

## Build a portable application

Install PyInstaller in the virtual environment and build the browser helper first:

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
.\Build.ps1 -StageOnly -ReuseBrowserEngine -WithoutSmartTags
```

The portable application is created in `build/package/Keepside`; the archive is `Keepside-Windows-x64.zip`. If you have the matching semantic-tagging assets, omit `-WithoutSmartTags`.

The publishing script preserves the selected existing library, makes a consistent backup, verifies original image bytes and settings, and creates desktop shortcuts. Review `docs/release-workflow.md` before using it on an existing installation.

## Local data and language

Use `--data-dir "absolute-path"` to choose a library. Upgrades retain the original `SnapVault` profile-binding namespace for compatibility. Do not move an active library or overwrite it with an empty directory.

Public application files and documentation use the Keepside name. An optional local settings file can override a display name for one language; it is never included in a source export or portable archive. Language preferences and library data remain local.

## Tests

```powershell
.venv\Scripts\python.exe -m pip install pytest
$env:QT_QPA_PLATFORM = 'offscreen'
.venv\Scripts\python.exe -m pytest tests/test_i18n.py tests/test_capture_i18n.py tests/test_shelf_i18n.py
```

Synthetic diagnostics can exercise the screenshot and editing interfaces without reading the real desktop or clipboard. Changes to language are checked in both languages; ordinary releases do not rerun every historical theme.

## License and credits

Keepside application source is GPL-3.0-only. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The browser-tab engine derives from [Chrome Mouse Wheel Tab Scroller](https://github.com/nurupo/chrome-mouse-wheel-tab-scroller), Copyright 2017–2022 Maxim Biro. Third-party runtimes and model assets retain their original licenses.
