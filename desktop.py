"""Windows desktop host for Agent Desk, with tray, login startup, and one instance.

Models run only after the user submits a task. --autostart opens the office;
it does not replay history or invent work.
"""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
from dataclasses import replace

from desktop_instance import SingleInstance, SingleInstanceError
from desktop_runtime import DesktopServer
from desktop_settings import DesktopConfig, SettingsStore, default_data_dir

TITLE = "Agent Desk · 模型辦公室"
PROJECT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("island.desktop")


def desktop_python() -> Path:
    candidate = PROJECT / ".venv-desktop" / "Scripts" / "pythonw.exe"
    if candidate.is_file():
        return candidate
    executable = Path(sys.executable).resolve()
    windowless = executable.with_name("pythonw.exe")
    return windowless if windowless.is_file() else executable


def create_tray_image():
    """The existing three-office mark, drawn procedurally for native icon sizes."""
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, 250, 250), radius=58, fill="#152a2d")
    for x, top, color in ((49, 95, "#b6d6a5"), (110, 61, "#e5b293"), (171, 84, "#9bcbdc")):
        draw.rounded_rectangle((x, top, x + 38, 185), radius=9, fill=color)
        draw.rounded_rectangle((x + 9, top + 14, x + 29, top + 29), radius=3, fill="#294448")
    draw.rounded_rectangle((43, 197, 213, 210), radius=6, fill="#78998c")
    return image


def ensure_icon() -> Path:
    target = PROJECT / "assets" / "agentdesk.ico"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        create_tray_image().save(target, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return target


def window_size(config: DesktopConfig) -> tuple[int, int]:
    width, height = config.window_width, config.window_height
    if os.name == "nt":
        from ctypes import wintypes
        bounds = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(bounds), 0):
            width = min(width, max(780, bounds.right - bounds.left - 50))
            height = min(height, max(560, bounds.bottom - bounds.top - 60))
    return width, height


class DesktopApp:
    def __init__(self, config: DesktopConfig, data_dir: Path, *, server=None,
                 webview_module=None, tray_module=None, startup_module=None):
        self.config = config
        self.data_dir = Path(data_dir)
        self.server = server or DesktopServer(config.workspace, self.data_dir, port=config.port,
                                              app_kwargs={"monitor_timeout": config.monitor_timeout})
        self.webview = webview_module
        self.pystray = tray_module
        self.startup = startup_module
        self.window = None
        self.tray = None
        self.instance = None
        self.quitting = threading.Event()
        self.loaded = threading.Event()
        self.gui_started = threading.Event()
        self.pending_show = threading.Event()
        self.smoke_result: dict | None = None
        self._smoke = False
        self._smoke_path = None
        self._startup_enabled = False

    def show(self, *_args) -> None:
        if self.quitting.is_set():
            return
        self.pending_show.set()
        if self.window is not None and self.gui_started.is_set():
            try:
                self.window.show()
                self.window.restore()
                self.pending_show.clear()
            except Exception:
                LOGGER.warning("Could not show the desktop window.")

    def on_closing(self, *_args):
        if self.quitting.is_set():
            return True
        if self.config.close_to_tray and self.tray is not None and self.tray.visible:
            self.window.hide()
            LOGGER.info("Office hidden to the system tray.")
            return False
        self.quitting.set()
        return True

    def request_quit(self, *_args) -> None:
        if self.quitting.is_set():
            return
        self.quitting.set()
        if self.window is not None:
            # Never wait for service/thread cleanup in a synchronous WinForms event.
            def destroy():
                try:
                    self.window.destroy()
                except Exception:
                    LOGGER.warning("Desktop window shutdown was already in progress.")
            threading.Thread(target=destroy, name="desktop-window-close", daemon=True).start()

    def on_loaded(self, *_args) -> None:
        self.loaded.set()
        LOGGER.info("WebView2 loaded the local office page.")

    def toggle_startup(self, *_args) -> None:
        def update():
            try:
                enabled = not self._startup_enabled
                self.startup.install_startup(desktop_python(), PROJECT / "desktop.py", enabled=enabled)
                self._startup_enabled = enabled
                LOGGER.info("Login startup %s.", "enabled" if enabled else "disabled")
                if self.tray is not None:
                    self.tray.update_menu()
            except Exception:
                LOGGER.exception("Could not update login startup.")
                if self.tray is not None:
                    self.tray.notify("無法變更開機啟動，請查看桌面程式日誌。", TITLE)
        threading.Thread(target=update, name="desktop-startup-setting", daemon=True).start()

    def _make_tray(self) -> None:
        item = self.pystray.MenuItem
        self.tray = self.pystray.Icon("island-agent-desk", create_tray_image(), TITLE,
            menu=self.pystray.Menu(
                item("開啟辦公室", self.show, default=True),
                item("登入 Windows 後啟動", self.toggle_startup,
                     checked=lambda _item: self._startup_enabled),
                self.pystray.Menu.SEPARATOR,
                item("離開", self.request_quit)))
        self.tray.run_detached()

    def _after_start(self) -> None:
        self.gui_started.set()
        if self.quitting.is_set():
            if self.window is not None:
                self.window.destroy()
            return
        if self.pending_show.is_set() and not self._smoke:
            self.show()
        if self._smoke:
            success = self.loaded.wait(30)
            self.smoke_result = {"window_loaded": success, "engine": "edgechromium",
                                 "url": self.server.url, "workspace": self.config.workspace,
                                 "automatic_model_jobs": False}
            if self._smoke_path:
                self._smoke_path.parent.mkdir(parents=True, exist_ok=True)
                self._smoke_path.write_text(json.dumps(self.smoke_result, ensure_ascii=False, indent=2), encoding="utf-8")
            self.request_quit()

    def run(self, *, background: bool = False, smoke: bool = False, smoke_path: Path | None = None) -> int:
        if self.webview is None:
            import webview
            self.webview = webview
        if self.pystray is None:
            import pystray
            self.pystray = pystray
        if self.startup is None:
            import windows_startup
            self.startup = windows_startup
        self._smoke, self._smoke_path = smoke, smoke_path
        self.instance = SingleInstance(self.data_dir, self.show)
        if not self.instance.acquire():
            LOGGER.info("An existing office was shown; no second server started.")
            return 0
        try:
            self._startup_enabled = self.startup.startup_enabled()
            url = self.server.start()
            width, height = window_size(self.config)
            self.window = self.webview.create_window(TITLE, url, width=width, height=height,
                min_size=(780, 560), hidden=background or smoke, background_color="#0b1518",
                text_select=True, zoomable=True, confirm_close=False)
            self.window.events.closing += self.on_closing
            self.window.events.loaded += self.on_loaded
            try:
                self._make_tray()
            except Exception:
                LOGGER.exception("System tray unavailable; closing the window will exit.")
                self.tray = None
            LOGGER.info("Starting native office at %s", url)
            self.webview.start(self._after_start, gui="edgechromium", private_mode=False,
                               storage_path=str(self.data_dir / "webview"), icon=str(ensure_icon()), debug=False)
            return 0 if not smoke or (self.smoke_result and self.smoke_result["window_loaded"]) else 1
        finally:
            self.quitting.set()
            try:
                if self.tray is not None:
                    self.tray.stop()
            finally:
                try:
                    self.server.stop()
                finally:
                    self.instance.close()
                    LOGGER.info("Desktop office stopped.")


def _configure_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(data_dir / "desktop.log", maxBytes=2 * 1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)
    LOGGER.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Agent Desk desktop office; jobs start only when assigned.")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--autostart", action="store_true", help="login startup; open office without running tasks")
    parser.add_argument("--background", action="store_true", help="start hidden in the tray")
    parser.add_argument("--install", action="store_true", help="create desktop and per-user login shortcuts")
    parser.add_argument("--disable-startup", action="store_true", help="remove only this app's login shortcut")
    parser.add_argument("--smoke-test", action="store_true", help="load a hidden native window and exit; no model requests")
    parser.add_argument("--smoke-result", type=Path)
    args = parser.parse_args(argv)
    if (args.install or args.disable_startup) and args.data_dir.resolve() != default_data_dir().resolve():
        parser.error("Desktop shortcuts use the standard data directory; omit --data-dir for installation.")
    _configure_logging(args.data_dir)
    store = SettingsStore(args.data_dir)
    config = store.load()
    if args.workspace:
        if not args.workspace.is_dir():
            parser.error("--workspace must be an existing directory")
        config = replace(config, workspace=str(args.workspace.resolve()))
        store.save(config)
    elif not store.path.exists():
        store.save(config)
    try:
        if args.install or args.disable_startup:
            import windows_startup
            if args.install:
                ensure_icon()
                shortcut = windows_startup.install_desktop_shortcut(desktop_python(), PROJECT / "desktop.py")
                startup = windows_startup.install_startup(desktop_python(), PROJECT / "desktop.py")
                print(json.dumps({"desktop_shortcut": str(shortcut), "startup_shortcut": str(startup),
                                  "data_dir": str(args.data_dir)}, ensure_ascii=False))
            else:
                windows_startup.install_startup(desktop_python(), PROJECT / "desktop.py", enabled=False)
                print("登入時自動啟動已關閉。")
            return 0
        if importlib.util.find_spec("webview") is None or importlib.util.find_spec("pystray") is None:
            pythonw = desktop_python()
            if pythonw.resolve() != Path(sys.executable).resolve() and pythonw.parent.name == "Scripts":
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                subprocess.Popen([str(pythonw), str(PROJECT / "desktop.py"), *(argv if argv is not None else sys.argv[1:])],
                                 cwd=PROJECT, shell=False, **options)
                return 0
            raise RuntimeError("Desktop dependencies are missing. Install requirements-desktop.txt first.")
        app = DesktopApp(config, args.data_dir)
        return app.run(background=args.background or (args.autostart and config.start_minimized),
                       smoke=args.smoke_test, smoke_path=args.smoke_result)
    except Exception:
        LOGGER.exception("Desktop startup failed.")
        message = f"辦公室啟動失敗。請查看 {args.data_dir / 'desktop.log'}"
        if os.name == "nt" and not args.smoke_test:
            ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
