"""Real Windows hotkey registration/Qt delivery around an isolated test vault.

No input is injected. Automatic checks post WM_HOTKEY only to this process's
owned HWND; preview counts only real shortcuts and performs no screenshot,
clipboard, paste, browser, authentication or production-library operation.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time
import traceback
import uuid

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QWidget

from . import hotkey, recent_panel, ui
from .basket_diagnostic import _replacements
from .shelf_diagnostic import _Capture, _Clipboard, _Ocr
from .storage import Vault


def run_hotkey_diagnostic(app, destination, *, preview=False):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fixture = root / ("hotkey-run-" + uuid.uuid4().hex[:8])
    fixture.mkdir()
    report_path = root / "hotkey-result.json"
    report = dict(ok=False, synthetic_only=True, preview=bool(preview),
                  dispatch_source="real_keyboard" if preview else "PostMessageW to owned HWND",
                  system_input_injected=False, system_clipboard_accessed=False,
                  screenshot_captured=False, browser_input_started=False,
                  authentication_prompted=False, windows_closed_cleanly=False,
                  callbacks={}, registered={}, configured_keys={}, checks=[])
    window = timer = timeout = None
    callbacks = {name: 0 for name in ("fullscreen", "region", "pin", "quick")}
    report["callbacks"] = callbacks
    clipboard = _Clipboard()
    labels = {}
    status = None
    stage = "setup"
    phase_started = time.monotonic()
    started = phase_started
    posted = 0
    old_hwnd = new_hwnd = 0

    def persist():
        report["callbacks"] = dict(callbacks)
        report["stage"] = stage
        report["posted_messages"] = posted
        report["seconds"] = round(time.monotonic() - started, 3)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (fixture / "hotkey-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    class Application:
        clipboard = staticmethod(lambda: clipboard)
        instance = staticmethod(lambda: app)
        quit = staticmethod(app.quit)

    def count(name):
        callbacks[name] += 1
        if name in labels:
            labels[name].setText(f"{labels[name].property('counterTitle')}：{callbacks[name]}")
        if preview and all(value >= 1 for value in callbacks.values()):
            report["ok"] = True
            if status is not None:
                status.setText("四个测试快捷键均已收到。所有操作只更新计数，不截屏、不贴图。")
        persist()

    replacements = [
        (ui, "QApplication", Application), (recent_panel, "QApplication", Application),
        (ui, "CaptureWorker", _Capture), (ui, "OcrWorker", _Ocr),
        (ui.MainWindow, "sequence", staticmethod(lambda: 0)),
        (ui.MainWindow, "take_fullscreen", lambda _self: count("fullscreen")),
        (ui.MainWindow, "start_screenshot", lambda _self: count("region")),
        (ui.MainWindow, "pin_clipboard", lambda _self: count("pin")),
        (ui.MainWindow, "toggle_quick_paste", lambda _self: count("quick")),
        (hotkey, "foreground_window", lambda: 0),
    ]

    def finish(ok, error=None):
        nonlocal stage
        if stage in {"quitting", "closed"}:
            return
        stage = "quitting"
        report["ok"] = bool(ok)
        if error:
            report["error"] = error
        if timer is not None:
            timer.stop()
        if timeout is not None:
            timeout.stop()
        persist()
        if window is not None:
            window.quit_app()
        else:
            app.quit()

    try:
        if os.name != "nt" or app.platformName() != "windows":
            raise RuntimeError("原生热键验收需要 Windows Qt 平台；offscreen 不能替代操作系统消息验证。")
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.PostMessageW.restype = wintypes.BOOL
        with _replacements(replacements):
            vault = Vault(fixture / "synthetic-data")
            for key, value in (("retention_days", 0), ("shelf_ball_enabled", False),
                               ("recent_files_system_enabled", False), ("restore_pins", False),
                               ("theme", "windows_light")):
                vault.set_setting(key, value)
            # Probe only uncommon modified function keys on our own throwaway
            # HWND, then release them before MainWindow registers the selection.
            probe_window = QWidget()
            probe_hwnd = int(probe_window.winId())
            available = []
            try:
                for number in range(13, 25):
                    key = f"Ctrl+Alt+Shift+F{number}"
                    probe = hotkey.GlobalHotkey(0x7E00 + number)
                    try:
                        if probe.register(probe_hwnd, key):
                            available.append(key)
                    finally:
                        probe.unregister()
                    if len(available) == 4:
                        break
            finally:
                probe_window.close()
                probe_window.deleteLater()
            if len(available) != 4:
                raise RuntimeError("未找到四个空闲的专用测试热键，未改动任何现有配置。")
            names = ("fullscreen", "region", "pin", "quick")
            settings = ("fullscreen_hotkey", "region_hotkey", "pin_clipboard_hotkey", "quick_paste_hotkey")
            for setting, key in zip(settings, available):
                vault.set_setting(setting, key)
            report["configured_keys"] = dict(zip(names, available))
            window = ui.MainWindow(vault, listen=False, use_tray=False, browser_tools=False,
                                   classify=False, hotkeys=True)
            pairs = {
                "fullscreen": (window.full_key, ui.FULL_HOTKEY_ID),
                "region": (window.shot_key, ui.SHOT_HOTKEY_ID),
                "pin": (window.pin_key, ui.PIN_HOTKEY_ID),
                "quick": (window.quick_key, ui.QUICK_HOTKEY_ID),
            }

            def registered_on(hwnd):
                return all(reg.active and reg.hwnd == hwnd for reg, _id in pairs.values())

            hwnd = int(window.winId())
            report["registered"] = {name: dict(active=reg.active, hwnd=reg.hwnd,
                                                   error=reg.error) for name, (reg, _id) in pairs.items()}
            if not registered_on(hwnd):
                raise AssertionError("实际 RegisterHotKey 未能为四个测试配置注册。")
            report["checks"].append("four uncommon shortcuts are registered by real Windows RegisterHotKey")

            def post_all():
                nonlocal posted
                owned = int(window.winId())
                for reg, identifier in pairs.values():
                    modifiers, vk = hotkey.parse_hotkey(reg.text)
                    if not user.PostMessageW(owned, hotkey.WM_HOTKEY, identifier, modifiers | (vk << 16)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    posted += 1

            if preview:
                # Keep the complete production MainWindow, chrome, native
                # event handling and material lifecycle. Counters are merely
                # added to its status bar; no owned widgets are deleted.
                for name, title in zip(names, ("全屏截图", "区域截图", "贴图", "随手面板")):
                    labels[name] = QLabel(title + "：0")
                    labels[name].setProperty("counterTitle", title)
                    labels[name].setAccessibleName(title + "收到次数")
                    labels[name].setToolTip(report["configured_keys"][name])
                    labels[name].setStyleSheet("font-size:14px;font-weight:600;color:#0067c0;padding:6px")
                    window.statusBar().addPermanentWidget(labels[name])
                status = QLabel("等待真实按键。Ctrl+Q 或“关闭”可正常退出；180 秒后自动结束。")
                status.setWordWrap(True)
                window.statusBar().addWidget(status, 1)
                window.setWindowTitle("全局快捷键原生验证 · " + fixture.name)
                window.show()
                stage = "real_keyboard_preview"
                timeout = QTimer(window)
                timeout.setSingleShot(True)
                timeout.timeout.connect(window.quit_app)
                timeout.start(180000)
                persist()
            else:
                stage = "initial_dispatch"
                post_all()
                phase_started = time.monotonic()

                def inspect():
                    nonlocal stage, phase_started, old_hwnd, new_hwnd
                    try:
                        elapsed = time.monotonic() - phase_started
                        if elapsed > 8:
                            raise AssertionError(f"阶段 {stage} 未收到预期消息，计数={callbacks}")
                        if stage == "initial_dispatch" and all(value == 1 for value in callbacks.values()):
                            report["checks"].append("owned WM_HOTKEY messages traverse Qt nativeEvent and dispatch all four callbacks")
                            window.suspend_hotkeys(True)
                            assert not any(reg.active for reg, _id in pairs.values())
                            window.apply_quick_settings(report=False)
                            assert window._hotkeys_suspended
                            assert not any(reg.active for reg, _id in pairs.values())
                            post_all()
                            stage, phase_started = "suspended_dispatch", time.monotonic()
                            persist()
                        elif stage == "suspended_dispatch" and elapsed >= .3:
                            assert all(value == 1 for value in callbacks.values()), callbacks
                            report["checks"].append("suspended shortcuts stay unregistered through settings refresh and ignore posted messages")
                            window.suspend_hotkeys(False)
                            assert registered_on(int(window.winId()))
                            post_all()
                            stage, phase_started = "resumed_dispatch", time.monotonic()
                            persist()
                        elif stage == "resumed_dispatch" and all(value == 2 for value in callbacks.values()):
                            report["checks"].append("resume re-registers all four shortcuts and native dispatch works again")
                            old_hwnd = int(window.winId())
                            window.hide()
                            window.destroy()
                            new_hwnd = int(window.winId())
                            assert old_hwnd != new_hwnd, "Windows reused the same HWND; rebind cannot be conclusively checked"
                            report["recreated_window"] = dict(old_hwnd=old_hwnd, new_hwnd=new_hwnd)
                            stage, phase_started = "rebind_wait", time.monotonic()
                            persist()
                        elif stage == "rebind_wait" and registered_on(new_hwnd):
                            post_all()
                            stage, phase_started = "rebound_dispatch", time.monotonic()
                            persist()
                        elif stage == "rebound_dispatch" and all(value == 3 for value in callbacks.values()):
                            report["checks"].append("real HWND recreation triggers automatic re-registration and all four callbacks still dispatch")
                            finish(True)
                    except Exception:
                        finish(False, traceback.format_exc())

                timer = QTimer(window)
                timer.timeout.connect(inspect)
                timer.start(30)
                timeout = QTimer(window)
                timeout.setSingleShot(True)
                timeout.timeout.connect(lambda: finish(False, "原生热键自检超过 30 秒。"))
                timeout.start(30000)
                persist()
            app.exec()
            for reg, _id in pairs.values():
                reg.unregister()
            window.recent_files.stop()
            report["all_unregistered_after_exit"] = not any(reg.active for reg, _id in pairs.values())
            report["windows_closed_cleanly"] = (window.quitting and not window.capture.isRunning()
                                                 and not window.ocr.isRunning())
            stage = "closed"
            persist()
    except Exception:
        report["ok"] = False
        report["error"] = traceback.format_exc()
        if timer is not None:
            timer.stop()
        if timeout is not None:
            timeout.stop()
        if window is not None:
            window.quit_app()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not getattr(window, "stop_queued", False):
                app.processEvents()
                time.sleep(.01)
            for reg in (window.full_key, window.shot_key, window.pin_key, window.quick_key):
                reg.unregister()
            window.recent_files.stop()
            window.hide()
        stage = "failed"
        persist()
    return 0 if report["ok"] and report["windows_closed_cleanly"] else 1
