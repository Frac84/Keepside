"""Stable screenshot exports and standard native file drag payloads.

Exports live outside the original-image lifecycle: a target application may
finish reading a dropped file after the drag operation has already returned.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QDesktopServices

from .storage import safe_title

from .i18n import tr


def _existing_file(value):
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(tr('无效的文件路径')) from exc
    if not path.is_file():
        raise ValueError(tr('只能拖出普通文件'))
    return path


def library_file(vault, key):
    """Copy the current original under the vault lock and return a stable path.

    Each drag receives its own snapshot. Neither OCR-derived renaming nor
    retention of the original can invalidate an already prepared export. The
    dedicated drag-cache can be removed while the application is not in use.
    """
    with vault.connect() as db:
        row = db.execute("SELECT filename,title,item_type FROM screenshots WHERE id=?", (key,)).fetchone()
        if row is None:
            raise FileNotFoundError(tr('这条截图已不存在'))
        if row["item_type"] != "image":
            raise ValueError(tr('只有截图可以作为图片文件拖出'))
        image_root = vault.images.resolve()
        source = _existing_file(image_root / row["filename"])
        if source.parent != image_root:
            raise ValueError(tr('原图路径不在图库中'))
        cache = vault.root / "drag-cache"
        cache.mkdir(exist_ok=True)
        if cache.resolve().parent != vault.root.resolve():
            raise ValueError(tr('文件拖出缓存目录无效'))
        export_dir = Path(tempfile.mkdtemp(prefix="shot-", dir=cache))
        target = export_dir / (tr('截图_') + safe_title(row["title"] or tr('截图')) + ".png")
        try:
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
        except Exception:
            target.unlink(missing_ok=True)
            export_dir.rmdir()
            raise
        return target


def file_mime_data(paths):
    """Build a native file-list payload, rejecting incomplete or invalid lists."""
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError(tr('请提供文件路径列表'))
    checked = []
    try:
        for value in paths:
            path = _existing_file(value)
            if path not in checked:
                checked.append(path)
    except TypeError as exc:
        raise ValueError(tr('请提供文件路径列表')) from exc
    if not checked:
        raise ValueError(tr('没有可拖出的文件'))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in checked])
    return mime


def encoded_image_file(vault, data, filename):
    """Keep the exact encoded output alive for a later clipboard paste."""
    if not isinstance(data, bytes) or not data:
        raise ValueError(tr('没有可复制的压缩结果'))
    name = Path(filename)
    if (name.name != filename or any(char in filename for char in '/\\:*?"<>|\0')
            or name.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")):
        raise ValueError(tr('无效的图片导出名称'))
    cache = vault.root / "drag-cache"
    cache.mkdir(exist_ok=True)
    if cache.resolve().parent != vault.root.resolve():
        raise ValueError(tr('文件导出缓存目录无效'))
    directory = Path(tempfile.mkdtemp(prefix="compressed-", dir=cache))
    target = directory / filename
    try:
        with target.open("xb") as stream:
            stream.write(data)
    except Exception:
        target.unlink(missing_ok=True)
        directory.rmdir()
        raise
    return target


def reveal_file(value):
    """Select an explicitly requested file without interpreting a shell command."""
    path = _existing_file(value)
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
        return True
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
