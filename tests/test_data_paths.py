"""New portable versions retain an explicitly bound synthetic vault."""
import json
from pathlib import Path
import sqlite3

import pytest

from snapvault.data_paths import (DataLocationError, bind_data_directory,
                                 default_binding_file, resolve_data_location)


def vault(path, *, records=0, settings=False):
    path.mkdir(parents=True)
    with sqlite3.connect(path / "vault.sqlite3") as db:
        db.executescript("CREATE TABLE screenshots(id TEXT PRIMARY KEY); CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);")
        for index in range(records):
            db.execute("INSERT INTO screenshots VALUES (?)", (str(index),))
        if settings:
            db.execute("INSERT INTO settings VALUES ('recent_files_state_v1','synthetic metadata')")
    return path


def test_explicit_path_wins_over_a_broken_binding(tmp_path):
    binding = tmp_path / "profile.json"
    binding.write_text("broken", encoding="utf-8")
    requested = tmp_path / "chosen"
    result = resolve_data_location(tmp_path / "new-version", requested, binding)
    assert result.directory == requested and result.source == "explicit"
    assert not requested.exists(), "Resolution must not create or overwrite data"


def test_first_unbound_portable_stays_local_without_writing_profile(tmp_path):
    binding = tmp_path / "profile.json"
    result = resolve_data_location(tmp_path / "portable", binding_file=binding)
    assert result.directory == tmp_path / "portable" / "data"
    assert result.source == "portable" and result.local_conflict is None
    assert not binding.exists() and not result.directory.exists()


def test_a_new_version_uses_original_records_without_copying_them(tmp_path):
    original = vault(tmp_path / "original", records=2)
    binding = tmp_path / "profile.json"
    bind_data_directory(original, binding)
    result = resolve_data_location(tmp_path / "version-2", binding_file=binding)
    assert result.directory == original and result.source == "bound"
    assert not (tmp_path / "version-2" / "data").exists()
    assert not list(tmp_path.glob("profile.json.*.tmp"))


@pytest.mark.parametrize("records,settings", [(1, False), (0, True)])
def test_new_local_records_or_recent_settings_raise_a_conflict_without_overwrite(tmp_path, records, settings):
    original = vault(tmp_path / "original", records=2)
    new = tmp_path / "new"
    local = vault(new / "data", records=records, settings=settings)
    before = (local / "vault.sqlite3").read_bytes()
    binding = tmp_path / "profile.json"
    bind_data_directory(original, binding)
    result = resolve_data_location(new, binding_file=binding)
    assert result.directory == original and result.local_conflict == local
    assert (local / "vault.sqlite3").read_bytes() == before


def test_empty_schema_does_not_force_a_conflict(tmp_path):
    original = vault(tmp_path / "original")
    new = tmp_path / "new"
    vault(new / "data")
    binding = tmp_path / "profile.json"
    bind_data_directory(original, binding)
    assert resolve_data_location(new, binding_file=binding).local_conflict is None


def test_same_installation_and_bound_vault_are_one_location(tmp_path):
    base = tmp_path / "install"
    original = vault(base / "data", records=1)
    binding = tmp_path / "profile.json"
    bind_data_directory(original, binding)
    assert resolve_data_location(base, binding_file=binding).local_conflict is None


@pytest.mark.parametrize("content", ["broken", "[]", '{"version":1,"data_dir":"relative"}', '{"version":2,"data_dir":"C:/other"}'])
def test_broken_profile_never_silently_creates_an_empty_library(tmp_path, content):
    binding = tmp_path / "profile.json"
    binding.write_text(content, encoding="utf-8")
    base = tmp_path / "new"
    with pytest.raises(DataLocationError):
        resolve_data_location(base, binding_file=binding)
    assert not (base / "data").exists()


def test_missing_original_drive_never_falls_back_to_new_empty_data(tmp_path):
    binding = tmp_path / "profile.json"
    binding.write_text(json.dumps({"version": 1, "data_dir": str(tmp_path / "missing")}), encoding="utf-8")
    with pytest.raises(DataLocationError, match="没有创建新的空资料库"):
        resolve_data_location(tmp_path / "new", binding_file=binding)


def test_bad_local_database_is_preserved_as_a_conflict(tmp_path):
    original = vault(tmp_path / "original")
    binding = tmp_path / "profile.json"
    bind_data_directory(original, binding)
    local = tmp_path / "new" / "data"
    local.mkdir(parents=True)
    (local / "vault.sqlite3").write_bytes(b"not sqlite")
    assert resolve_data_location(tmp_path / "new", binding_file=binding).local_conflict == local


def test_binding_rejects_non_vault_and_preserves_previous_profile(tmp_path):
    binding = tmp_path / "profile.json"
    binding.write_text("existing binding", encoding="utf-8")
    with pytest.raises(DataLocationError):
        bind_data_directory(tmp_path / "not-created", binding)
    assert binding.read_text(encoding="utf-8") == "existing binding"


def test_profile_path_uses_user_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_binding_file() == tmp_path / "SnapVault" / "profile.json"
