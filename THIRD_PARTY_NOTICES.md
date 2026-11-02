# Third-party notices

Keepside application source is distributed under GPL-3.0-only; see `LICENSE`. Portable builds include corresponding application source and build scripts, excluding personal libraries and local profile bindings.

## Browser tab engine

The browser-tab enhancement derives from Chrome Mouse Wheel Tab Scroller by Maxim Biro (nurupo), Copyright 2017–2022, GPL-3.0-only:

https://github.com/nurupo/chrome-mouse-wheel-tab-scroller

The original mouse handling and focus behavior are retained. The embedded engine adds lifecycle management, configuration IPC and optional double-click behavior. The original baseline, attribution and license are preserved under `browser_engine/upstream`. The audit script checks the original core functions. AutoIt is a build dependency, not a bundled installer requirement.

## Offline OCR

RapidOCR engineering code is Copyright RapidOCR Authors, Apache-2.0. The bundled PaddleOCR-derived detection, recognition and angle-classification model weights retain copyright of Baidu and the applicable PaddleOCR rights holders, Apache-2.0. Keepside does not retrain or modify these OCR weights.

Model provenance, exact source URLs, SHA-256 hashes and the complete Apache license are recorded under `assets/ocr-models`. Runtime dependency licenses are collected under `assets/ocr-licenses`; its manifest records actual versions and file hashes. Model names do not imply endorsement.

OCR inference uses ONNX Runtime CPU (MIT), together with NumPy, OpenCV Headless, Shapely/GEOS, pyclipper, OmegaConf and other dependencies under their respective licenses. GEOS remains a separate dynamic library.

## Optional offline topic tagging

The custom encoder derives from BAAI/bge-small-zh-v1.5 (BAAI / FlagEmbedding, MIT), adapted with Hugging Face SetFit (Apache-2.0) using author-written synthetic examples. SetFit and PyTorch are training tools and are not bundled in the runtime. Hugging Face Tokenizers is Apache-2.0. Model descriptions and licenses are retained under `assets/classification-models`.

https://huggingface.co/BAAI/bge-small-zh-v1.5

https://github.com/FlagOpen/FlagEmbedding

https://github.com/huggingface/setfit

https://github.com/huggingface/tokenizers

Synthetic validation results are not accuracy claims about personal collections. Optional model weights are excluded from the source repository; matching assets are required to enable that component.

## Other runtime components

Python, PySide6/Qt, Pillow and all other dependencies retain their original licenses. These notices do not replace upstream license terms or transfer ownership. Application interface icons are drawn for this project; the internal AutoIt engine retains its upstream icon attribution.
