"""Consent boundary tests. Never open a real Windows authentication dialog."""
import ctypes
import threading
import uuid

import pytest

from snapvault import windows_hello as hello


class Operation:
    def __init__(self, value=0, status=1, on_result=None):
        self.value, self.state, self.on_result = value, status, on_result
        self.cancelled = self.closed = self.results_read = False

    def status(self):
        return self.state

    def result(self):
        self.results_read = True
        if self.on_result:
            self.on_result()
        return self.value

    def error(self):
        return 0x80070005

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


@pytest.fixture
def runtime(monkeypatch):
    state = {"operation": Operation(), "events": []}

    class Runtime:
        def __enter__(self):
            state["events"].append(("enter", threading.get_ident()))
            return self

        def __exit__(self, *_args):
            state["events"].append(("exit", threading.get_ident()))

        def factory(self, _iid):
            state["events"].append(("factory", threading.get_ident()))

        def availability_operation(self):
            state["events"].append(("availability", threading.get_ident()))
            return state["operation"]

        def verification_operation(self, hwnd, message):
            state["events"].append(("verify", threading.get_ident()))
            state["request"] = (hwnd, message)
            return state["operation"]

    monkeypatch.setattr(hello, "_supported", lambda: True)
    monkeypatch.setattr(hello, "_valid_owner_window", lambda hwnd: hwnd == 123)
    monkeypatch.setattr(hello, "_Runtime", Runtime)
    return state


@pytest.mark.parametrize("value", [0, 1, 2, 3, 4, 5, 6, 7, -1, False, True, None])
def test_only_verified_result_unlocks(runtime, value):
    runtime["operation"].value = value
    result = hello.verify(123)
    assert result.success is (type(value) is int and value == 0)
    assert bool(result) is result.success
    assert runtime["operation"].closed


@pytest.mark.parametrize("value", [0, 1, 2, 3, 4, 5, -1, None])
def test_availability_is_non_interactive_and_only_available_enables_lock(runtime, value):
    runtime["operation"].value = value
    result = hello.availability()
    assert result.usable is (value == 0)
    assert result.reason == result.message
    assert "verify" not in [event for event, _thread in runtime["events"]]


def test_unsupported_platform_never_initializes_runtime(runtime, monkeypatch):
    monkeypatch.setattr(hello, "_supported", lambda: False)
    assert not hello.availability().usable
    assert not hello.verify(123).success
    assert runtime["events"] == []


def test_invalid_owner_never_opens_authentication(runtime):
    result = hello.verify(999)
    assert not result.success and result.status == "invalid_window"
    assert runtime["events"] == []


@pytest.mark.parametrize("message", ["", "  ", "a\0b", None, "a" * 513])
def test_invalid_verification_request_does_not_open_authentication(runtime, message):
    result = hello.verify(123, message)
    assert not result.success and result.status == "invalid_request"
    assert runtime["events"] == []


def test_pre_cancelled_request_never_opens_authentication(runtime):
    cancel = threading.Event()
    cancel.set()
    result = hello.verify(123, cancel_event=cancel)
    assert not result.success and result.status == "cancelled"
    assert runtime["events"] == []


def test_cancel_wins_over_a_late_success(runtime):
    cancel = threading.Event()
    runtime["operation"].on_result = cancel.set
    result = hello.verify(123, cancel_event=cancel)
    assert not result.success and result.status == "cancelled"
    assert runtime["operation"].closed


@pytest.mark.parametrize("status,expected", [(2, "cancelled"), (3, "error"), (9, "error")])
def test_async_failure_never_reads_success_result(runtime, status, expected):
    runtime["operation"].state = status
    result = hello.verify(123)
    assert not result.success and result.status == expected
    assert not runtime["operation"].results_read
    assert runtime["operation"].closed


def test_timeout_cancels_without_reading_result(runtime, monkeypatch):
    runtime["operation"].state = 0
    monkeypatch.setattr(hello, "VERIFICATION_TIMEOUT", 0)
    result = hello.verify(123)
    assert not result.success and result.status == "timeout"
    assert runtime["operation"].cancelled and runtime["operation"].closed
    assert not runtime["operation"].results_read


def test_cancelled_availability_check_fails_closed(runtime):
    runtime["operation"].state = 2
    result = hello.availability()
    assert not result.usable and result.status == "cancelled"


def test_com_call_failure_is_a_closed_result(runtime, monkeypatch):
    def fail(*_args, **_kwargs):
        raise hello._NativeError(0x80070005)

    monkeypatch.setattr(hello, "_wait", fail)
    assert not hello.verify(123).success
    assert not hello.availability().usable
    events = [event for event, _thread in runtime["events"]]
    assert events.count("enter") == events.count("exit") == 2


def test_runtime_lifetime_stays_on_calling_worker(runtime):
    result = []
    worker = threading.Thread(target=lambda: result.append(hello.verify(123)))
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and result[0].success
    ids = {thread for _event, thread in runtime["events"]}
    assert ids == {worker.ident} and worker.ident != threading.get_ident()


@pytest.mark.parametrize("hr", [0x80004005, 0x80070005, -2147467259])
def test_hresult_failure_is_signed_32_bit(hr):
    with pytest.raises(hello._NativeError):
        hello._check(hr)


def test_documented_generic_interface_iids():
    assert str(uuid.UUID(bytes_le=bytes(hello._IID_VERIFY_OPERATION))) == "fd596ffd-2318-558f-9dbe-d21df43764a5"
    availability = hello._operation_iid("UserConsentVerifierAvailability")
    assert str(uuid.UUID(bytes_le=bytes(availability))) == "ddd384f3-d818-5d83-ab4b-32119c28587c"
    assert ctypes.sizeof(hello._GUID) == 16


def test_native_operation_uses_correct_qi_status_result_and_release_slots():
    """Exercise real ctypes calls on a synthetic COM object, not Windows Hello."""
    callbacks = []
    events = []
    operation_table = (ctypes.c_void_p * 9)()
    info_table = (ctypes.c_void_p * 11)()
    operation_object = ctypes.c_void_p(ctypes.addressof(operation_table))
    info_object = ctypes.c_void_p(ctypes.addressof(info_table))

    def bind(table, slot, result, args, function):
        callback = hello._CALLTYPE(result, ctypes.c_void_p, *args)(function)
        callbacks.append(callback)
        table[slot] = ctypes.cast(callback, ctypes.c_void_p).value

    def query(_self, iid, result):
        assert bytes(iid.contents) == bytes(hello._IID_ASYNC_INFO)
        result[0] = ctypes.addressof(info_object)
        events.append("query")
        return 0

    def status(_self, result):
        result[0] = 1
        events.append("status")
        return 0

    def get_result(_self, result):
        result[0] = 0
        events.append("result")
        return 0

    bind(operation_table, 0, hello._HRESULT, [ctypes.POINTER(hello._GUID), ctypes.POINTER(ctypes.c_void_p)], query)
    bind(operation_table, 2, ctypes.c_uint32, [], lambda _self: events.append("release_operation") or 0)
    bind(operation_table, 8, hello._HRESULT, [ctypes.POINTER(ctypes.c_int32)], get_result)
    bind(info_table, 2, ctypes.c_uint32, [], lambda _self: events.append("release_info") or 0)
    bind(info_table, 7, hello._HRESULT, [ctypes.POINTER(ctypes.c_int32)], status)
    bind(info_table, 10, hello._HRESULT, [], lambda _self: events.append("close") or 0)
    native = hello._Runtime()
    operation = hello._Operation(native, ctypes.c_void_p(ctypes.addressof(operation_object)))
    try:
        assert hello._wait(operation, 1) == 0
    finally:
        native.__exit__(None, None, None)
    assert events == ["query", "status", "result", "close", "release_info", "release_operation"]
