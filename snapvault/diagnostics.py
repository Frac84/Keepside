"""Original engine startup/configuration/exit checks with input actions disabled."""
import json
from pathlib import Path
import time
import traceback
from PySide6.QtWidgets import QWidget
from .tab_scroller import TabScroller, OPTIONS_PROPERTY, encode_options


def run_browser_diagnostic(app, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "checks": [], "mouse_actions_enabled": False}
    window = QWidget()
    service = TabScroller(app, int(window.winId()), diagnostic=True)
    try:
        assert service.configure(), service.error
        deadline = time.monotonic() + 10
        while not service.engine_ready and not service.error and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert service.engine_ready, service.error or "Engine readiness timeout"
        first_pid = service.engine_pid
        result["checks"].append("original AutoIt engine reports readiness")
        assert not service.active
        for mode, delay in [(0, 3), (1, 50), (2, 25)]:
            assert service.configure(reverse=True, focus_mode=mode, focus_delay=delay), service.error
            assert service.engine_pid == first_pid
            expected = encode_options(False, True, True, True, mode, delay) + 1
            assert service._api.property(service._window, OPTIONS_PROPERTY) == expected
        result["checks"].append("all original focus modes acknowledge without spawning duplicates")
        service.stop()
        assert not service.engine_ready
        result["checks"].append("graceful exit leaves no owned engine")
        assert service.configure(), service.error
        deadline = time.monotonic() + 10
        while not service.engine_ready and not service.error and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert service.engine_ready, service.error
        service.stop()
        result["checks"].append("pause and restart release the original single-instance lock")
        result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        service.stop()
        window.destroy()
    (destination / "browser-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return 0 if result["ok"] else 1
