"""Native glass contracts are tested with fake DLLs, never real windows."""
import ctypes
from types import SimpleNamespace

import pytest

from snapvault import glass


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *arguments):
        return self.callback(*arguments)


class Widget:
    def __init__(self):
        self.translucent = False
        self.opacity = 1.0
        self.handle = 0x123456789
        self.handle_reads = 0

    def testAttribute(self, _attribute):
        return self.translucent

    def windowOpacity(self):
        return self.opacity

    def winId(self):
        self.handle_reads += 1
        return self.handle

    def internalWinId(self):
        return self.handle


@pytest.fixture
def native(monkeypatch):
    state = SimpleNamespace(calls=[], remote=False, high_contrast=False, overlap=False,
        battery=False, layered=False, composition=True, transparency=True, frame_result=0,
        theme_result=0, backdrop_result=0, read_result=0, read_value=3, accessibility=True)

    def attribute(hwnd, key, value, size):
        assert size == 4
        item = ctypes.cast(value, ctypes.POINTER(ctypes.c_int32)).contents.value
        state.calls.append(("attribute", hwnd.value, key, item))
        if key == glass.DWMWA_USE_IMMERSIVE_DARK_MODE:
            return state.theme_result
        return state.backdrop_result if key == glass.DWMWA_SYSTEMBACKDROP_TYPE and item == 3 else 0

    def read(hwnd, key, pointer, size):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int32)).contents.value = state.read_value
        return state.read_result

    def frame(hwnd, pointer):
        margins = ctypes.cast(pointer, ctypes.POINTER(glass._Margins)).contents
        values = tuple(getattr(margins, name) for name, _type in margins._fields_)
        state.calls.append(("frame", hwnd.value, values))
        return state.frame_result

    def composition(pointer):
        ctypes.cast(pointer, ctypes.POINTER(glass.wintypes.BOOL)).contents.value = state.composition
        return 0

    def parameters(action, size, pointer, flags):
        assert flags == 0
        if action == glass.SPI_GETHIGHCONTRAST:
            ctypes.cast(pointer, ctypes.POINTER(glass._HighContrast)).contents.dwFlags = int(state.high_contrast)
            return state.accessibility
        ctypes.cast(pointer, ctypes.POINTER(glass.wintypes.BOOL)).contents.value = state.overlap
        return True

    def power(pointer):
        ctypes.cast(pointer, ctypes.POINTER(glass._PowerStatus)).contents.SystemStatusFlag = int(state.battery)
        return True

    style = Function(lambda hwnd, index: glass.WS_EX_LAYERED if state.layered else 0)
    user = SimpleNamespace(GetSystemMetrics=Function(lambda _index: int(state.remote)),
        SystemParametersInfoW=Function(parameters), GetWindowLongPtrW=style, GetWindowLongW=style)
    dwm = SimpleNamespace(DwmSetWindowAttribute=Function(attribute), DwmGetWindowAttribute=Function(read),
        DwmExtendFrameIntoClientArea=Function(frame), DwmIsCompositionEnabled=Function(composition))
    kernel = SimpleNamespace(GetSystemPowerStatus=Function(power))
    libraries = {"user32.dll": user, "dwmapi.dll": dwm, "kernel32.dll": kernel}

    def load(name, **kwargs):
        assert kwargs == {"winmode": 0x800}
        return libraries[name]

    monkeypatch.setattr(glass.ctypes, "WinDLL", load)
    monkeypatch.setattr(glass.os, "name", "nt")
    monkeypatch.setattr(glass, "_build", lambda: 26100)
    monkeypatch.setattr(glass, "_qt_platform", lambda: "windows")
    monkeypatch.setattr(glass, "_transparency_preference", lambda: state.transparency)
    yield state
    glass._active.clear()
    glass._accent_active.clear()


@pytest.mark.parametrize("dark", [False, True])
def test_documented_backdrop_and_frame_are_requested_and_read_back(native, dark):
    widget = Widget()
    result = glass.apply_glass(widget, dark)
    assert result["enabled"] and result["method"] == "dwm-desktop-acrylic"
    assert ("frame", widget.handle, (-1, -1, -1, -1)) in native.calls
    assert ("attribute", widget.handle, 20, int(dark)) in native.calls
    assert ("attribute", widget.handle, 33, 2) in native.calls
    assert ("attribute", widget.handle, 38, 3) in native.calls
    assert widget.handle_reads == 1


@pytest.mark.parametrize("field,reason", [
    ("remote", "remote-session"), ("high_contrast", "high-contrast"),
    ("overlap", "transparent-overlap-disabled"), ("battery", "battery-saver"),
])
def test_system_policy_falls_back_without_touching_window(native, field, reason):
    setattr(native, field, True)
    widget = Widget()
    result = glass.apply_glass(widget)
    assert result == dict(enabled=False, method="fallback", reason=reason)
    assert native.calls == [] and widget.handle_reads == 0


def test_disabled_transparency_falls_back(native):
    native.transparency = False
    assert glass.apply_glass(Widget())["reason"] == "transparency-disabled"
    assert native.calls == []


def test_whole_window_opacity_is_not_misreported_as_acrylic(native):
    widget = Widget()
    widget.opacity = 0.8
    assert glass.apply_glass(widget)["reason"] == "whole-window-opacity"
    assert native.calls == []


def test_native_frame_alpha_surface_is_allowed_when_not_layered(native):
    widget = Widget()
    widget.translucent = True
    assert not native.layered
    assert glass.apply_glass(widget)["enabled"]
    assert ("attribute", widget.handle, 38, 3) in native.calls


def test_translucent_frameless_native_layered_surface_still_falls_back(native):
    widget = Widget()
    widget.translucent = True
    native.layered = True
    assert glass.apply_glass(widget)["reason"] == "layered-native-window"
    assert native.calls == []


def test_native_layered_style_is_rejected_even_without_qt_attribute(native):
    native.layered = True
    assert glass.apply_glass(Widget())["reason"] == "layered-native-window"
    assert native.calls == []


def test_old_windows_and_offscreen_do_not_load_native_dlls(native, monkeypatch):
    monkeypatch.setattr(glass, "_build", lambda: 19045)
    assert glass.apply_glass(Widget())["reason"] == "requires-windows-11-22621"
    monkeypatch.setattr(glass, "_qt_platform", lambda: "offscreen")
    assert glass.apply_glass(Widget())["reason"] == "non-native-qt-platform"
    assert native.calls == []


def test_api_failure_is_a_fallback_and_does_not_claim_blur(native):
    native.backdrop_result = -2147024809
    result = glass.apply_glass(Widget())
    assert not result["enabled"] and result["reason"].startswith("backdrop-request-failed")
    assert any(call[0] == "attribute" and call[2:] == (38, 1) for call in native.calls)
    assert native.calls[-1][0] == "frame" and native.calls[-1][2] == (0, 0, 0, 0)


def test_unconfirmed_readback_clears_backdrop(native):
    native.read_value = 0
    assert glass.apply_glass(Widget())["reason"] == "backdrop-readback-unconfirmed"
    assert any(call[0] == "attribute" and call[2:] == (38, 1) for call in native.calls)


def test_policy_change_clears_previously_requested_backdrop(native):
    widget = Widget()
    assert glass.apply_glass(widget)["enabled"]
    native.transparency = False
    result = glass.apply_glass(widget)
    assert not result["enabled"]
    assert native.calls[-2][2:] == (38, 1)
    assert native.calls[-1][2] == (0, 0, 0, 0)


def test_native_handle_replacement_does_not_clear_destroyed_handle(native):
    widget = Widget()
    assert glass.apply_glass(widget)["enabled"]
    before = list(native.calls)
    widget.handle += 1
    native.transparency = False
    assert not glass.apply_glass(widget)["enabled"]
    assert native.calls == before


def test_missing_native_api_returns_fallback(native, monkeypatch):
    monkeypatch.setattr(glass, "_load_api", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    assert glass.apply_glass(Widget())["reason"] == "native-api-unavailable:OSError"


def test_choosing_an_opaque_theme_clears_existing_material(native):
    widget = Widget()
    assert glass.apply_glass(widget)["enabled"]
    reads = widget.handle_reads
    result = glass.disable_glass(widget)
    assert result == dict(enabled=False, method="fallback", reason="theme-disabled")
    assert native.calls[-2][2:] == (38, 1)
    assert native.calls[-1][2] == (0, 0, 0, 0)
    assert widget.handle_reads == reads
    assert widget not in glass._active


@pytest.fixture
def raster(native, monkeypatch):
    native.accent_ok, native.alpha_result, native.region = True, 0, 0x123456780
    native.accents, native.blurs, native.freed = [], [], []

    def accent(hwnd, pointer):
        data = ctypes.cast(pointer, ctypes.POINTER(glass._CompositionData)).contents
        assert data.attribute == 19 and data.size == ctypes.sizeof(glass._AccentPolicy)
        policy = ctypes.cast(data.data, ctypes.POINTER(glass._AccentPolicy)).contents
        native.accents.append((hwnd.value, policy.state, policy.color))
        return native.accent_ok

    def blur(hwnd, pointer):
        item = ctypes.cast(pointer, ctypes.POINTER(glass._BlurBehind)).contents
        native.blurs.append((hwnd.value, item.flags, bool(item.enable), item.region))
        return native.alpha_result

    user = SimpleNamespace(SetWindowCompositionAttribute=Function(accent))
    dwm = SimpleNamespace(DwmEnableBlurBehindWindow=Function(blur))
    gdi = SimpleNamespace(CreateRectRgn=Function(lambda *rect: native.region if rect == (0, 0, -1, -1) else 0),
                          DeleteObject=Function(lambda region: native.freed.append(region) or True))
    monkeypatch.setattr(glass, "_load_acrylic_api", lambda: (user, dwm, gdi))
    widget = Widget()
    widget.property = lambda name: name == "appGlass"
    return native, widget


@pytest.mark.parametrize("dark,color", [(False, 0x99F3F3F3), (True, 0x99202020)])
def test_qt_raster_acrylic_connects_alpha_and_frees_region(raster, dark, color):
    state, widget = raster
    result = glass.apply_glass(widget, dark)
    assert result["enabled"] and result["method"] == "accent-acrylic"
    assert state.accents == [(widget.handle, 4, color)]
    assert state.blurs == [(widget.handle, 3, True, state.region)]
    assert state.freed == [state.region]
    assert widget.handle_reads == 1 and widget.opacity == 1
    glass.disable_glass(widget)
    assert state.accents[-1][1] == 0
    assert state.blurs[-1] == (widget.handle, 1, False, None)
    assert widget not in glass._active and widget not in glass._accent_active


@pytest.mark.parametrize("failure", ["accent", "alpha", "region"])
def test_failed_raster_request_cleans_partial_native_state(raster, failure):
    state, widget = raster
    if failure == "accent":
        state.accent_ok = False
    elif failure == "alpha":
        state.alpha_result = -1
    else:
        state.region = 0
    result = glass.apply_glass(widget)
    assert not result["enabled"]
    assert widget not in glass._active and widget not in glass._accent_active
    assert len(state.freed) == (0 if failure == "region" else 1)
    if failure != "region":
        assert state.accents[-1][1] == 0 and state.blurs[-1][2] is False
