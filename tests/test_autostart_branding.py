"""Release-name aliases use simulated Run values; never access the registry."""
import sys

import pytest

from snapvault import autostart


@pytest.fixture
def startup(monkeypatch, tmp_path):
    store = {"value": None, "writes": []}
    monkeypatch.setattr(autostart, "supported", lambda: True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "捕风捉影.exe"))
    monkeypatch.setattr(autostart, "_read", lambda: store["value"])

    def write(value):
        store["writes"].append(value)
        store["value"] = value

    monkeypatch.setattr(autostart, "_write", write)
    return store


@pytest.mark.parametrize("running, registered", [
    ("捕风捉影.exe", "拾影.exe"),
    ("拾影.exe", "捕风捉影.exe"),
])
def test_old_and_new_names_in_same_installation_are_equivalent(startup, tmp_path, monkeypatch,
                                                               running, registered):
    monkeypatch.setattr(sys, "executable", str(tmp_path / running))
    startup["value"] = autostart.command(tmp_path / registered)
    assert autostart.enabled()
    assert startup["writes"] == []


def test_old_name_from_another_directory_is_not_this_installation(startup, tmp_path):
    startup["value"] = autostart.command(tmp_path / "another-installation" / "拾影.exe")
    assert not autostart.enabled()


def test_alias_does_not_apply_to_arbitrary_executable_name(startup, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "another-app.exe"))
    startup["value"] = autostart.command(tmp_path / "拾影.exe")
    assert not autostart.enabled()


def test_alias_requires_frozen_release(startup, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False)
    startup["value"] = autostart.command(tmp_path / "拾影.exe")
    assert not autostart.enabled()


def test_missing_run_value_is_disabled(startup):
    assert not autostart.enabled()


def test_disabling_keeps_the_existing_delete_value_behavior(startup, tmp_path):
    startup["value"] = autostart.command(tmp_path / "拾影.exe")
    assert autostart.enabled()
    assert autostart.set_enabled(False) is False
    assert startup["writes"] == [None]
    assert startup["value"] is None


def test_enabling_still_registers_the_current_executable(startup):
    assert autostart.set_enabled(True)
    assert startup["writes"] == [autostart.command()]
