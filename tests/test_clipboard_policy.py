"""Privacy marker tests use fabricated metadata; never read the OS clipboard."""
import ctypes
from types import SimpleNamespace

import pytest

from snapvault import clipboard_policy as policy


class Mime:
    def __init__(self, formats=None, local=False, remote=False):
        self.values = formats or {}
        self.local, self.remote = local, remote

    def hasFormat(self, name):
        return name in self.values

    def data(self, name):
        return self.values[name]

    def hasUrls(self):
        return self.local or self.remote

    def urls(self):
        return [SimpleNamespace(isLocalFile=lambda: self.local)]


def native(monkeypatch, formats=(), value=0, can_open=True, size=4):
    marker = ctypes.c_uint32(value)
    calls = []
    user = SimpleNamespace(
        IsClipboardFormatAvailable=lambda number: number in formats,
        OpenClipboard=lambda _: calls.append('open') or can_open,
        GetClipboardData=lambda _: 42,
        CloseClipboard=lambda: calls.append('close'),
    )
    kernel = SimpleNamespace(
        GlobalSize=lambda _: size,
        GlobalLock=lambda _: ctypes.addressof(marker),
        GlobalUnlock=lambda _: calls.append('unlock'),
    )
    monkeypatch.setattr(policy, '_native_api', lambda: (user, kernel, 1, 2))
    return calls, marker


def test_unmarked_item_does_not_open_or_read_clipboard(monkeypatch):
    calls, _ = native(monkeypatch)
    assert not policy.should_exclude_clipboard(Mime())
    assert calls == []


def test_explicit_exclusion_never_reads_payload(monkeypatch):
    calls, _ = native(monkeypatch, formats=(1,))
    assert policy.should_exclude_clipboard(Mime())
    assert calls == []


@pytest.mark.parametrize('value,excluded', [(0, True), (1, False)])
def test_windows_history_dword_and_handle_cleanup(monkeypatch, value, excluded):
    calls, _ = native(monkeypatch, formats=(2,), value=value)
    assert policy.clipboard_excluded() is excluded
    assert calls == ['open', 'unlock', 'close']


def test_locked_marked_clipboard_is_skipped_without_closing_other_owner(monkeypatch):
    calls, _ = native(monkeypatch, formats=(2,), can_open=False)
    assert policy.clipboard_excluded()
    assert calls == ['open']


def test_malformed_history_marker_is_skipped_and_clipboard_closed(monkeypatch):
    calls, _ = native(monkeypatch, formats=(2,), size=2)
    assert policy.clipboard_excluded()
    assert calls == ['open', 'close']


@pytest.mark.parametrize('format_name', [policy.EXCLUDE_FORMAT,
    f'application/x-qt-windows-mime;value="{policy.EXCLUDE_FORMAT}"'])
def test_qt_private_marker_short_circuits_native_access(monkeypatch, format_name):
    monkeypatch.setattr(policy, '_native_api', lambda: pytest.fail('No native read expected'))
    assert policy.should_exclude_clipboard(Mime({format_name: b''}))


def test_qt_history_marker_blocks_capture_without_native_read(monkeypatch):
    monkeypatch.setattr(policy, '_native_api', lambda: pytest.fail('No native read expected'))
    assert policy.should_exclude_clipboard(Mime({policy.HISTORY_FORMAT: b'\0' * 4}))


def test_copied_file_metadata_is_not_collected_as_text(monkeypatch):
    calls, _ = native(monkeypatch, formats=(15,))
    assert policy.should_exclude_clipboard(Mime())
    assert calls == []


def test_local_file_urls_skipped_remote_urls_allowed(monkeypatch):
    monkeypatch.setattr(policy, '_native_api', lambda: None)
    assert policy.should_exclude_clipboard(Mime(local=True))
    assert not policy.should_exclude_clipboard(Mime(remote=True))


def test_no_windows_api_does_not_block_ordinary_items(monkeypatch):
    monkeypatch.setattr(policy, '_native_api', lambda: None)
    assert not policy.should_exclude_clipboard(Mime())
