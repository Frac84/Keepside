"""Owned-window native move/resize tracing with synthetic application content.

The automatic check asks Windows for hit tests and styles only. It never sends
mouse input or changes geometry to stand in for a successful drag. Interactive
preview records actual native move/size loops and their resulting geometry.
"""
from __future__ import annotations

from collections import Counter, deque
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import traceback
import uuid

from PIL import Image, ImageDraw
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QPushButton
from shiboken6 import isValid

from . import hotkey, recent_panel, ui, window_chrome as chrome
from .basket_diagnostic import _replacements
from .shelf_diagnostic import _Capture, _Clipboard, _Ocr
from .storage import Vault


_NAMES = {chrome.WM_NCHITTEST: "WM_NCHITTEST", chrome.WM_NCLBUTTONDOWN: "WM_NCLBUTTONDOWN",
          chrome.WM_NCLBUTTONDBLCLK: "WM_NCLBUTTONDBLCLK", 0x0112: "WM_SYSCOMMAND",
          0x0231: "WM_ENTERSIZEMOVE", 0x0232: "WM_EXITSIZEMOVE", 0x0003: "WM_MOVE",
          0x0005: "WM_SIZE", chrome.WM_CAPTURECHANGED: "WM_CAPTURECHANGED"}


def _rectangle(value):
    return dict(x=value.x(), y=value.y(), width=value.width(), height=value.height())


def run_window_diagnostic(app, destination, *, preview=False):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    fixture = root / ("window-run-" + token)
    fixture.mkdir()
    report_path = root / "window-result.json"
    started = time.monotonic()
    events, event_counts, inspectors, gestures = deque(maxlen=160), Counter(), [], deque(maxlen=16)
    report = dict(ok=False, synthetic_only=True, preview=bool(preview),
                  system_input_injected=False, system_clipboard_accessed=False,
                  screenshot_captured=False, browser_input_started=False,
                  global_hotkeys_registered=False, authentication_prompted=False,
                  system_recent_read=False, windows_closed_cleanly=False,
                  movement_observed=False, resize_observed=False,
                  system_move_observed=False, system_resize_observed=False,
                  automatic_check_scope="owned HWND hit tests and styles only; no drag simulated",
                  checks=[])
    window = timer = timeout = None
    baseline = active_gesture = None
    setup_complete = False
    clipboard = _Clipboard()

    class Application:
        clipboard = staticmethod(lambda: clipboard)
        instance = staticmethod(lambda: app)
        quit = staticmethod(app.quit)

    class NoDialogs:
        getOpenFileName = staticmethod(lambda *_args, **_kwargs: ("", ""))
        getOpenFileNames = staticmethod(lambda *_args, **_kwargs: ([], ""))
        getSaveFileName = staticmethod(lambda *_args, **_kwargs: ("", ""))
        getExistingDirectory = staticmethod(lambda *_args, **_kwargs: "")

    class NoExternalApps:
        openUrl = staticmethod(lambda _url: False)

    replacements = [
        (ui, "QApplication", Application), (recent_panel, "QApplication", Application),
        (ui, "CaptureWorker", _Capture), (ui, "OcrWorker", _Ocr),
        (ui.MainWindow, "sequence", staticmethod(lambda: 0)),
        (ui, "QFileDialog", NoDialogs), (recent_panel, "QFileDialog", NoDialogs),
        (ui, "QDesktopServices", NoExternalApps), (recent_panel, "QDesktopServices", NoExternalApps),
        (recent_panel, "reveal_file", lambda _path: False),
        (hotkey, "foreground_window", lambda: 0),
    ]

    def state(widget):
        return dict(geometry=_rectangle(widget.geometry()), frame_geometry=_rectangle(widget.frameGeometry()),
                    normal_geometry=_rectangle(widget.normalGeometry()), dpr=widget.devicePixelRatioF(),
                    hwnd=int(widget.internalWinId()), visible=widget.isVisible(),
                    maximized=widget.isMaximized(), minimized=widget.isMinimized(),
                    fullscreen=widget.isFullScreen(), window_opacity=widget.windowOpacity(),
                    window_flags=int(widget.windowFlags()))

    def compare(first, current):
        a, b = first["geometry"], current["geometry"]
        return ((a["x"], a["y"]) != (b["x"], b["y"]),
                (a["width"], a["height"]) != (b["width"], b["height"]))

    def persist():
        if window is not None and isValid(window):
            current = state(window)
            report["current"] = current
            report["glass"] = dict(window.glass_state)
            if baseline is not None:
                moved, resized = compare(baseline, current)
                report["movement_observed"] |= moved
                report["resize_observed"] |= resized
            report["inspectors"] = [state(dialog) for dialog in inspectors if isValid(dialog)]
        report["seconds"] = round(time.monotonic() - started, 3)
        report["native_events"] = list(events)
        report["native_event_counts"] = dict(event_counts)
        report["gestures"] = list(gestures)
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        report_path.write_text(payload, encoding="utf-8")

    def completed_gesture(gesture):
        if window is None or not isValid(window):
            return
        gesture["end"] = state(window)
        gesture["moved"], gesture["resized"] = compare(gesture["start"], gesture["end"])
        report["system_move_observed"] |= gesture["moved"]
        report["system_resize_observed"] |= gesture["resized"]
        gestures.append(gesture)
        if preview and report["system_move_observed"] and report["system_resize_observed"]:
            report["ok"] = True
        persist()

    class ProbeMainWindow(ui.MainWindow):
        def nativeEvent(self, event_type, message):
            nonlocal active_gesture
            traced = None
            try:
                if bytes(event_type) == b"windows_generic_MSG" and message is not None and int(message):
                    msg = wintypes.MSG.from_address(int(message))
                    if int(msg.hWnd or 0) == int(self.internalWinId()) and msg.message in _NAMES:
                        traced = dict(event=_NAMES[msg.message], message=int(msg.message),
                                      wparam=int(msg.wParam), lparam=int(msg.lParam),
                                      hwnd=int(msg.hWnd or 0), seconds=round(time.monotonic() - started, 4))
                        event_counts[traced["event"]] += 1
                        events.append(traced)
                        if setup_complete and msg.message == 0x0231:
                            active_gesture = dict(start=state(self), started_at=traced["seconds"])
                        elif setup_complete and msg.message == 0x0232 and active_gesture is not None:
                            gesture, active_gesture = active_gesture, None
                            gesture["ended_at"] = traced["seconds"]
                            QTimer.singleShot(0, lambda current=gesture: completed_gesture(current))
            except (TypeError, ValueError, RuntimeError, OSError):
                # Trace collection must not turn an unrelated Qt message into
                # an input failure. The production nativeEvent still receives it.
                pass
            result = super().nativeEvent(event_type, message)
            if traced is not None:
                traced["handled"], traced["result"] = bool(result[0]), int(result[1])
            return result

        def quit_app(self):
            for dialog in list(inspectors):
                if isValid(dialog):
                    dialog.close()
            super().quit_app()

    def finish():
        if timer is not None:
            timer.stop()
        if timeout is not None:
            timeout.stop()
        persist()
        if window is not None and isValid(window):
            window.quit_app()
        else:
            app.quit()

    def hit_tests():
        """Query only this process's HWND; no mouse/key message is sent."""
        user = chrome._user_api()
        send = user.SendMessageW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        send.restype = ctypes.c_ssize_t
        hwnd = int(window.winId())
        rect = chrome.NativeRect()
        if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError()
        middle_x, middle_y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        points = [
            ("top_left", rect.left + 1, rect.top + 1, chrome.HTTOPLEFT),
            ("top", middle_x, rect.top + 1, chrome.HTTOP),
            ("top_right", rect.right - 2, rect.top + 1, chrome.HTTOPRIGHT),
            ("left", rect.left + 1, middle_y, chrome.HTLEFT),
            ("right", rect.right - 2, middle_y, chrome.HTRIGHT),
            ("bottom_left", rect.left + 1, rect.bottom - 2, chrome.HTBOTTOMLEFT),
            ("bottom", middle_x, rect.bottom - 2, chrome.HTBOTTOM),
            ("bottom_right", rect.right - 2, rect.bottom - 2, chrome.HTBOTTOMRIGHT),
        ]
        origin = chrome.NativePoint(0, 0)
        if not user.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise ctypes.WinError()
        ratio = window.devicePixelRatioF()
        header = window.window_chrome.header
        for x in range(20, header.width() - 20, 8):
            local = header.mapTo(window, QPoint(x, header.height() // 2))
            if window.window_chrome.is_drag_point(local):
                points.append(("caption", round(origin.x + local.x() * ratio),
                               round(origin.y + local.y() * ratio), chrome.HTCAPTION))
                break
        for name, control, expected in (("toolbar_button", window.basket_button, chrome.HTCLIENT),
                                         ("maximize_button", window.window_chrome.buttons["maximize"], chrome.HTMAXBUTTON)):
            local = control.mapTo(window, control.rect().center())
            points.append((name, round(origin.x + local.x() * ratio),
                           round(origin.y + local.y() * ratio), expected))
        queries = []
        for name, x, y, expected in points:
            packed = (x & 0xffff) | ((y & 0xffff) << 16)
            result = int(send(hwnd, chrome.WM_NCHITTEST, 0, packed))
            queries.append(dict(name=name, screen_x=x, screen_y=y, expected=expected,
                                observed=result, passed=result == expected))
        report["native_hit_tests"] = queries
        report["native_styles"] = dict(style=hex(int(user.GetWindowLongPtrW(hwnd, chrome.GWL_STYLE))),
                                        exstyle=hex(int(user.GetWindowLongPtrW(hwnd, -20))))
        report["checks"].append("real SendMessageW WM_NCHITTEST queried only this owned HWND")
        return len(queries) == 11 and all(query["passed"] for query in queries)

    def arm():
        nonlocal baseline, setup_complete
        baseline = state(window)
        report["baseline"] = baseline
        setup_complete = True
        try:
            passed = hit_tests()
            report["native_hit_tests_passed"] = passed
            if not preview:
                report["ok"] = passed
                if not passed:
                    report["error"] = "Native hit tests did not match the owned header, controls or resize frame."
        except Exception:
            report["error"] = traceback.format_exc()
        persist()
        if not preview:
            finish()

    try:
        if os.name != "nt" or app.platformName() != "windows":
            raise RuntimeError("窗口原生验收需要 Windows Qt 平台；离屏命中测试不能替代真实鼠标拖动和缩放。")
        with _replacements(replacements):
            vault = Vault(fixture / "synthetic-data")
            for key, value in (("retention_days", 0), ("theme", "glass_day"),
                               ("shelf_ball_enabled", False), ("restore_pins", False),
                               ("recent_files_system_enabled", False)):
                vault.set_setting(key, value)
            with Image.new("RGB", (640, 360), "#E7E7E7") as image:
                draw = ImageDraw.Draw(image)
                draw.rectangle((55, 55, 585, 305), outline="#0067C0", width=5)
                draw.line((55, 305, 240, 150, 420, 280, 585, 90), fill="#737373", width=5)
                key, _ = vault.add(image, datetime.now(timezone.utc))
            vault.finish_ocr(key, "Synthetic window operation fixture")
            window = ProbeMainWindow(vault, listen=False, use_tray=False, browser_tools=False,
                                     classify=False, hotkeys=False)
            window.setWindowTitle("窗口操作验证 · " + token)
            window.resize(1120, 720)
            area = window.screen().availableGeometry()
            window.move(area.center() - window.rect().center())
            # Even accidental capture toolbar actions only return our fixture
            # pixels, never any real desktop screenshot or clipboard content.
            synthetic = QPixmap(str(vault.images / vault.get(key)["filename"]))
            window.grab_screens = lambda: [(QRect(0, 0, synthetic.width(), synthetic.height()), synthetic.copy())]
            window.grab_fullscreen = lambda: synthetic.copy()

            def open_inspector():
                dialog = window.preview()
                if dialog is not None and dialog not in inspectors:
                    inspectors.append(dialog)
                    dialog.finished.connect(lambda _value, current=dialog:
                        inspectors.remove(current) if current in inspectors else None)
                persist()

            preview_button = QPushButton("打开合成图片预览")
            preview_button.setObjectName("openSyntheticInspector")
            preview_button.clicked.connect(open_inspector)
            window.statusBar().addPermanentWidget(preview_button)
            window.statusBar().showMessage("窗口操作验证：拖动顶部空白处、边缘或角落；Ctrl+Q 退出。仅合成资料。")
            if not preview:
                window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            window.show()
            timer = QTimer(window)
            timer.setInterval(200)
            timer.timeout.connect(persist)
            timer.start()
            timeout = QTimer(window)
            timeout.setSingleShot(True)
            timeout.timeout.connect(finish)
            timeout.start(180000 if preview else 15000)
            QTimer.singleShot(800, arm)
            persist()
            app.exec()
            report["windows_closed_cleanly"] = not window.capture.isRunning() and not window.ocr.isRunning()
            report["current"] = state(window)
            window.hide()
            persist()
    except Exception:
        report["error"] = traceback.format_exc()
        if window is not None and isValid(window) and not window.quitting:
            window.quit_app()
    finally:
        if timer is not None:
            timer.stop()
        if timeout is not None:
            timeout.stop()
        persist()
    return 0 if report["ok"] else 1
