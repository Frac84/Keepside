"""Download only the three licensed OCR models, verifying their exact hashes."""
import hashlib
from pathlib import Path
import urllib.request

MODELS = {
    'PP-OCRv6_det_small.onnx': ('090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f', 'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/det/PP-OCRv6_det_small.onnx'),
    'PP-OCRv6_rec_small.onnx': ('6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884', 'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx'),
    'ch_ppocr_mobile_v2.0_cls_mobile.onnx': ('e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c', 'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx')}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    destination = Path(__file__).resolve().parents[1] / 'assets' / 'ocr-models'
    destination.mkdir(parents=True, exist_ok=True)
    for name, (expected, url) in MODELS.items():
        target = destination / name
        if target.is_file() and digest(target) == expected:
            print(name + ': verified')
            continue
        temporary = target.with_suffix('.download')
        try:
            urllib.request.urlretrieve(url, temporary)
            if digest(temporary) != expected:
                raise ValueError('Model checksum mismatch: ' + name)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        print(name + ': downloaded and verified')


if __name__ == '__main__':
    main()
