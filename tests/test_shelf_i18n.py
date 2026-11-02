"""Shelf strings localize while paths, file names, and stored source labels do not."""
import ast
import json
from pathlib import Path
from string import Formatter

import pytest

from snapvault import i18n
from snapvault.clipboard_policy import sensitive_text_reason
from test_recent_panel import app, flush, shelf  # noqa: F401


@pytest.fixture
def shelf_english(monkeypatch):
    catalog_path = Path(__file__).parents[1] / "snapvault" / "locales" / "en_shelf.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(i18n, "_catalog", catalog)
    with i18n.using_language("en_US"):
        yield catalog


def test_shelf_translation_catalog_is_complete_and_preserves_placeholders():
    root = Path(__file__).parents[1] / "snapvault"
    catalog = json.loads((root / "locales" / "en_shelf.json").read_text(encoding="utf-8"))
    keys = set()
    for filename in ("recent_panel.py", "file_transfer.py", "tab_scroller.py", "clipboard_policy.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "tr" and node.args and isinstance(node.args[0], ast.Constant)):
                keys.add(node.args[0].value)
    assert keys <= catalog.keys()
    for source, english in catalog.items():
        assert not any("\u3400" <= char <= "\u9fff" for char in english)
        fields = lambda value: {field for _, field, _, _ in Formatter().parse(value) if field is not None}
        assert fields(source) == fields(english)


def test_english_shelf_buttons_do_not_change_file_payloads(shelf, shelf_english, tmp_path):
    host, panel, clipboard, _ = shelf()
    assert panel.tabs.tabText(0) == "Library"
    assert panel.tabs.tabText(1) == "Recent files"
    assert panel.tabs.tabText(2) == "File basket"
    assert panel.basket_paste_button.text() == "Paste files"
    assert panel.basket_clear_button.text() == "Clear basket"
    original = tmp_path / "中文用户文件.txt"
    original.write_text("unchanged", encoding="utf-8")
    panel.add_basket_paths([original])
    panel.copy_basket(all_items=True)
    assert [Path(url.toLocalFile()) for url in clipboard.payloads[-1].urls()] == [original]
    assert panel.basket_entries()[0]["name"] == "中文用户文件.txt"
    assert host.file_basket.entries()[0]["path"] == str(original)
    assert original.read_text(encoding="utf-8") == "unchanged"


def test_builtin_sources_localize_without_rewriting_folder_names(shelf, shelf_english, tmp_path):
    host, panel, *_ = shelf()
    entry = dict(path=str(tmp_path / "sample.txt"), source="最近使用", origin="system_recent")
    assert panel.source_label(entry) == "Recently used"
    assert entry["source"] == "最近使用"
    assert panel.source_label(dict(entry, source="手动添加", origin="manual")) == "Manually added"
    host.recent_files._download_folder = str(tmp_path / "system-downloads")
    assert panel.source_label(dict(entry, source="下载", origin="watch")) == "下载"


def test_lock_accessibility_and_secret_reason_use_selected_language(shelf, shelf_english):
    host, panel, *_ = shelf()
    assert panel.unlock_button.text() == "Unlock with Windows Hello"
    assert panel.lock_files()
    assert "locked" in panel.lock_message.text().lower() or panel.files_locked
    assert sensitive_text_reason("-----BEGIN PRIVATE KEY-----") == "private key"
    with i18n.using_language("zh_CN"):
        assert sensitive_text_reason("-----BEGIN PRIVATE KEY-----") == "私钥"
