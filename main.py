"""Source and frozen application entrypoint."""
import sys

# Classification is a private offline process and must branch before Qt loads.
if "--classify-worker" in sys.argv[1:]:
    from snapvault.topic_runtime import worker_main
    raise SystemExit(worker_main(sys.argv[1:]))

# OCR processes branch before importing Qt, branding, UI or the browser engine.
if "--ocr-worker" in sys.argv[1:]:
    from snapvault.ocr_runtime import serve_main
    raise SystemExit(serve_main(sys.argv[1:]))
if "--ocr-job" in sys.argv[1:]:
    from snapvault.ocr_runtime import worker_main
    raise SystemExit(worker_main(sys.argv[1:]))

import argparse
import hashlib
from pathlib import Path
import subprocess
import traceback

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from snapvault.branding import APP_NAME, APP_TAGLINE, app_icon, app_name
from snapvault.i18n import configure_language, preferred_language, tr
from snapvault.storage import Vault
from snapvault.ui import MainWindow, STYLE
from snapvault.data_paths import resolve_data_location


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} — {APP_TAGLINE}")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--language-self-test", type=Path, help="Verify both UI languages with a synthetic library")
    parser.add_argument("--minimized", action="store_true", help="启动后隐藏到托盘")
    parser.add_argument("--smoke-test", type=Path, help="在指定独立目录执行合成图片端到端自检后退出")
    parser.add_argument("--browser-self-test", type=Path, help="禁用鼠标动作，验证原版AutoIt引擎的启动、配置和退出")
    parser.add_argument("--collection-self-test", type=Path, help="使用独立合成内容验证文字、链接、分类和混合图库")
    parser.add_argument("--theme-self-test", type=Path, help="在独立合成图库验证全部主题的预览、保存与取消")
    parser.add_argument("--classification-self-test", type=Path, help="在独立合成图库验证本地模型、多标签、手动保护和后台退出")
    parser.add_argument("--shelf-self-test", type=Path, help="使用合成文件检查最近文件浮台并导出预览")
    parser.add_argument("--shelf-preview", type=Path, help="打开独立合成文件浮台供预览")
    parser.add_argument("--basket-self-test", type=Path, help="用独立合成文件验证中转篮后退出")
    parser.add_argument("--basket-preview", type=Path, help="打开独立合成中转篮用于拖放验收")
    parser.add_argument("--daily-tools-self-test", type=Path, help="用合成图片验证日常入口、标注与定大小压缩")
    parser.add_argument("--daily-tools-preview", type=Path, help="打开隔离的合成日常工具验收窗口")
    parser.add_argument("--hotkey-self-test", type=Path, help="真实注册并验证四个全局快捷键的原生消息通路")
    parser.add_argument("--hotkey-preview", type=Path, help="打开仅计数、不截图的全局快捷键验收窗口")
    parser.add_argument("--window-self-test", type=Path, help="在合成窗口验证原生标题栏、边框与命中处理")
    parser.add_argument("--window-preview", type=Path, help="打开记录实际移动和缩放的合成窗口验收界面")
    parser.add_argument("--screenshot-self-test", type=Path, help="使用冻结合成图验证选区、现场标注与截图输出")
    parser.add_argument("--screenshot-preview", type=Path, help="打开独立合成截图操作验收界面")
    parser.add_argument("--screenshot-edit-self-test", type=Path, help="合成验证标注二次编辑和截图日夜界面")
    parser.add_argument("--screenshot-edit-preview", type=Path, help="打开隔离的二次编辑验收界面")
    parser.add_argument("--screenshot-privacy-self-test", type=Path, help="验证临时截图识字不入库和临时文件清理")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)
    configure_language(preferred_language(), app)
    app.setApplicationDisplayName(app_name())
    if any(value.endswith('self-test') for value in sys.argv[1:]):
        configure_language('zh_CN', app)
    if args.language_self_test:
        from snapvault.language_diagnostic import run_language_diagnostic
        return run_language_diagnostic(app,args.language_self_test)
    if args.screenshot_edit_self_test or args.screenshot_edit_preview:
        from snapvault.screenshot_edit_diagnostic import run_screenshot_edit_diagnostic
        return run_screenshot_edit_diagnostic(app, args.screenshot_edit_self_test or args.screenshot_edit_preview,
                                              preview=bool(args.screenshot_edit_preview))
    if args.screenshot_privacy_self_test:
        from snapvault.screenshot_privacy_diagnostic import run_screenshot_privacy_diagnostic
        return run_screenshot_privacy_diagnostic(app, args.screenshot_privacy_self_test)
    if args.screenshot_self_test or args.screenshot_preview:
        from snapvault.capture_diagnostic import run_capture_diagnostic
        return run_capture_diagnostic(app, args.screenshot_self_test or args.screenshot_preview,
                                      preview=bool(args.screenshot_preview))
    if args.window_self_test or args.window_preview:
        from snapvault.window_diagnostic import run_window_diagnostic
        return run_window_diagnostic(app, args.window_self_test or args.window_preview,
                                      preview=bool(args.window_preview))
    if args.hotkey_self_test or args.hotkey_preview:
        from snapvault.hotkey_diagnostic import run_hotkey_diagnostic
        return run_hotkey_diagnostic(app, args.hotkey_self_test or args.hotkey_preview,
                                      preview=bool(args.hotkey_preview))
    if args.daily_tools_self_test or args.daily_tools_preview:
        from snapvault.daily_tools_diagnostic import run_daily_tools_diagnostic
        return run_daily_tools_diagnostic(app, args.daily_tools_self_test or args.daily_tools_preview,
                                          preview=bool(args.daily_tools_preview))
    if args.basket_preview:
        from snapvault.basket_diagnostic import run_basket_preview
        return run_basket_preview(app, args.basket_preview)
    if args.basket_self_test:
        from snapvault.basket_diagnostic import run_basket_diagnostic
        return run_basket_diagnostic(app, args.basket_self_test)
    if args.shelf_self_test or args.shelf_preview:
        from snapvault.shelf_diagnostic import run_shelf_diagnostic
        return run_shelf_diagnostic(app, args.shelf_self_test or args.shelf_preview,
                                    preview=bool(args.shelf_preview))
    if args.classification_self_test:
        from snapvault.topic_diagnostic import run_topic_diagnostic
        return run_topic_diagnostic(app, args.classification_self_test)
    if args.browser_self_test:
        from snapvault.diagnostics import run_browser_diagnostic
        return run_browser_diagnostic(app, args.browser_self_test)
    if args.smoke_test:
        from snapvault.smoke import run_smoke
        return run_smoke(app, args.smoke_test)
    if args.collection_self_test:
        from snapvault.collection_diagnostic import run_collection_diagnostic
        return run_collection_diagnostic(app, args.collection_self_test)
    if args.theme_self_test:
        from snapvault.collection_diagnostic import run_collection_diagnostic
        return run_collection_diagnostic(app, args.theme_self_test, include_themes=True)
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    try:
        location = resolve_data_location(base, args.data_dir)
        root = location.directory
        if location.local_conflict is not None:
            choice = QMessageBox()
            choice.setWindowTitle(tr('选择资料库') + ' · ' + app_name())
            choice.setText(tr('检测到两个资料库，请选择本次使用的位置，记录不会被覆盖。'))
            choice.setInformativeText(tr('原资料库：{original}\n当前资料库：{current}', original=root, current=location.local_conflict))
            original = choice.addButton(tr('打开原资料库'), QMessageBox.ButtonRole.AcceptRole)
            local = choice.addButton(tr('打开当前资料库'), QMessageBox.ButtonRole.ActionRole)
            choice.addButton(QMessageBox.StandardButton.Cancel)
            choice.setDefaultButton(original)
            choice.exec()
            if choice.clickedButton() is local:
                root = location.local_conflict
            elif choice.clickedButton() is not original:
                return 0
        root.mkdir(parents=True, exist_ok=True)
        server_name = "SnapVault-" + hashlib.sha256(str(root).casefold().encode()).hexdigest()[:24]
        lock = QLockFile(str(root / "instance.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            socket = QLocalSocket()
            socket.connectToServer(server_name)
            if socket.waitForConnected(1000):
                socket.write(b"show")
                socket.waitForBytesWritten(1000)
            else:
                QMessageBox.information(None, app_name(), tr('资料库已在运行，请从系统托盘打开应用。'))
            return 0
        vault = Vault(root)
        configure_language(vault.setting("ui_language", preferred_language()), app)
        app.setApplicationDisplayName(app_name())
        try:
            from snapvault.topic_runtime import validate_assets
            validate_assets()
            tagging_available = True
        except (OSError, ValueError, RuntimeError):
            tagging_available = False
        window = MainWindow(vault, classify=tagging_available)
        server = QLocalServer()
        QLocalServer.removeServer(server_name)
        server.listen(server_name)
        def activate():
            client = server.nextPendingConnection()
            window.show_window()
            if client:
                client.disconnectFromServer()
                client.deleteLater()
        server.newConnection.connect(activate)
        if not args.minimized or not window.tray:
            window.show()
        def report(kind, value, tb):
            # Logs deliberately omit screenshots and recognized content.
            with (root / "error.log").open("a", encoding="utf-8") as out:
                traceback.print_exception(kind, value, tb, file=out)
            window.report_error(tr('发生错误：{error}。详情已写入本地 error.log。', error=value))
        sys.excepthook = report
        exit_code = app.exec()
        server.close()
        lock.unlock()
        if getattr(window, "language_restart_requested", False):
            executable = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
            subprocess.Popen(executable + ["--data-dir", str(root)], cwd=str(base))
        return exit_code
    except Exception as exc:
        QMessageBox.critical(None, app_name(), tr('无法启动：{error}\n请确保资料库可写，或使用 --data-dir 指定其他目录。', error=exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
