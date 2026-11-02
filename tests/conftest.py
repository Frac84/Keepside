"""Legacy interaction assertions use Chinese; language tests switch explicitly."""
import pytest
from pathlib import Path
from snapvault import i18n

@pytest.fixture(autouse=True)
def stable_test_language(monkeypatch):
    monkeypatch.setenv('KEEPSIDE_LOCAL_SETTINGS', str(Path(__file__).parent / 'no-local-overrides.json'))
    previous = i18n.language()
    i18n.set_language('zh_CN')
    yield
    i18n.set_language(previous)
