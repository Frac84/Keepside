"""File transfer checks use only synthetic temporary libraries and files."""
from pathlib import Path

from PIL import Image
import pytest

from snapvault.file_transfer import encoded_image_file, file_mime_data, library_file, reveal_file
from snapvault.storage import Vault


def test_snapshot_survives_ocr_rename_and_original_deletion(tmp_path):
    vault = Vault(tmp_path / "vault")
    key, _ = vault.add(Image.new("RGB", (30, 20), "red"))
    before = vault.images / vault.get(key)["filename"]
    original = before.read_bytes()
    exported = library_file(vault, key)
    assert exported.read_bytes() == original
    assert exported.parent.parent == vault.root / "drag-cache"
    vault.finish_ocr(key, "新标题：学校 / 作业")
    assert not before.exists()
    vault.delete(key)
    assert exported.read_bytes() == original


def test_encoded_export_keeps_exact_bounded_bytes_outside_library(tmp_path):
    vault = Vault(tmp_path / "vault")
    payload = b"synthetic encoded image bytes"
    first = encoded_image_file(vault, payload, "压缩图片.jpg")
    second = encoded_image_file(vault, payload, "压缩图片.jpg")
    assert first != second and first.read_bytes() == second.read_bytes() == payload
    assert first.parent.parent == vault.root / "drag-cache"
    assert vault.query() == []
    assert [Path(url.toLocalFile()) for url in file_mime_data([first]).urls()] == [first.resolve()]


@pytest.mark.parametrize("filename", ["../original.png", "nested/original.jpg", "hidden:stream.png", "file.exe"])
def test_encoded_export_rejects_unsafe_filename(tmp_path, filename):
    vault = Vault(tmp_path / "vault")
    with pytest.raises(ValueError):
        encoded_image_file(vault, b"synthetic", filename)


def test_each_snapshot_uses_current_row_and_does_not_overwrite(tmp_path):
    vault = Vault(tmp_path / "vault")
    key, _ = vault.add(Image.new("RGB", (30, 20), "red"))
    first = library_file(vault, key)
    vault.finish_ocr(key, "新的标题")
    second = library_file(vault, key)
    assert first != second
    assert "新的标题" in second.name
    assert first.read_bytes() == second.read_bytes()


def test_text_and_missing_library_rows_cannot_be_exported(tmp_path):
    vault = Vault(tmp_path / "vault")
    key, _ = vault.add_text("不是截图")
    with pytest.raises(ValueError, match="只有截图"):
        library_file(vault, key)
    with pytest.raises(FileNotFoundError):
        library_file(vault, "missing")
    assert not (vault.root / "drag-cache").exists()


def test_library_export_rejects_path_outside_images(tmp_path):
    vault = Vault(tmp_path / "vault")
    key, _ = vault.add(Image.new("RGB", (30, 20), "red"))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    with vault.connect() as db:
        db.execute("UPDATE screenshots SET filename=? WHERE id=?", (str(outside), key))
    with pytest.raises(ValueError, match="图库"):
        library_file(vault, key)
    assert outside.read_bytes() == b"outside"


def test_multiple_unicode_files_produce_local_urls_and_preserve_sources(tmp_path):
    first = tmp_path / "学校 作业.pdf"
    second = tmp_path / "image # 1.png"
    first.write_bytes(b"document")
    second.write_bytes(b"image")
    mime = file_mime_data([first, str(second), first])
    assert mime.hasUrls()
    assert [Path(url.toLocalFile()) for url in mime.urls()] == [first, second]
    assert all(url.isLocalFile() for url in mime.urls())
    assert first.read_bytes() == b"document"
    assert second.read_bytes() == b"image"


@pytest.mark.parametrize("case", ["missing", "directory", "empty", "scalar", "mixed"])
def test_bad_file_lists_are_rejected_as_a_whole(tmp_path, case):
    valid = tmp_path / "valid.txt"
    valid.write_text("content", encoding="utf-8")
    values = {
        "missing": [tmp_path / "missing.txt"],
        "directory": [tmp_path],
        "empty": [],
        "scalar": str(valid),
        "mixed": [valid, None],
    }
    with pytest.raises((ValueError, FileNotFoundError)):
        file_mime_data(values[case])


def test_reveal_file_passes_filename_as_a_separate_argument(tmp_path, monkeypatch):
    from snapvault import file_transfer
    file = tmp_path / "学校 & 作业.txt"
    file.write_text("synthetic", encoding="utf-8")
    calls = []
    monkeypatch.setattr(file_transfer.os, "name", "nt")
    monkeypatch.setattr(file_transfer.subprocess, "Popen", lambda args: calls.append(args))
    assert reveal_file(file)
    assert calls == [["explorer.exe", "/select,", str(file)]]
