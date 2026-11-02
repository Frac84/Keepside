"""Copy installed OCR dependencies' existing notices, without importing runtimes."""
from importlib import metadata
from pathlib import Path
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ('rapidocr', 'onnxruntime', 'numpy', 'opencv-python-headless', 'Shapely',
            'pyclipper', 'omegaconf', 'PyYAML', 'flatbuffers', 'protobuf', 'requests',
            'urllib3', 'certifi', 'charset-normalizer', 'idna', 'six', 'tqdm', 'colorlog',
            'packaging', 'typing-extensions', 'colorama')


def main():
    destination = ROOT / 'assets/ocr-licenses'
    manifest = {}
    for package in PACKAGES:
        dist = metadata.distribution(package)
        files = []
        for entry in dist.files or ():
            entry_path = Path(entry)
            if entry_path.is_absolute() or '..' in entry_path.parts:
                continue
            lower = str(entry_path).lower()
            if not any(word in lower for word in ('license', 'licence', 'notice', 'copying')):
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file() or source.suffix in ('.py', '.pyc', '.pyd', '.dll'):
                continue
            target = destination / package / entry_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files.append({'path': str(target.relative_to(destination)).replace('\\', '/'),
                          'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        if not files and package == 'flatbuffers':
            # This wheel omits its license file. A copy from the matching
            # upstream release is checked in with the redistribution notices.
            for source in (destination / package).glob('*'):
                if source.is_file():
                    files.append({'path': str(source.relative_to(destination)).replace('\\', '/'),
                                  'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
        if not files:
            raise RuntimeError(f'No bundled license found for {package}')
        manifest[package] = {'version': dist.version, 'files': files}
    (destination / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'Copied notices for {len(manifest)} OCR dependencies.')


if __name__ == '__main__':
    main()
