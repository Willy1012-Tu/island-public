"""Desktop lifecycle checks with no native window, shortcuts, or model calls."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import desktop
from desktop import DesktopApp
from desktop_settings import DesktopConfig, SettingsStore


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self


class Window:
    def __init__(self):
        self.events = SimpleNamespace(closing=Event(), loaded=Event())
        self.calls = []
        self.destroyed = threading.Event()

    def show(self):
        self.calls.append(("show", threading.get_ident()))

    def restore(self):
        self.calls.append(("restore", threading.get_ident()))

    def hide(self):
        self.calls.append(("hide", threading.get_ident()))

    def destroy(self):
        self.calls.append(("destroy", threading.get_ident()))
        self.destroyed.set()


class Webview:
    def __init__(self):
        self.window = Window()
        self.created = []
        self.starts = []
        self.on_start = lambda callback: callback()
        self.create_error = None

    def create_window(self, *args, **kwargs):
        if self.create_error:
            raise self.create_error
        self.created.append((args, kwargs))
        return self.window

    def start(self, callback, **kwargs):
        self.starts.append(kwargs)
        self.on_start(callback)


class Server:
    def __init__(self):
        self.url = "http://127.0.0.1:49152/monitor"
        self.starts = 0
        self.stops = 0
        self.running = False
        self.start_error = None
        self.stop_error = None

    def start(self):
        self.starts += 1
        if self.start_error:
            raise self.start_error
        self.running = True
        return self.url

    def stop(self):
        self.stops += 1
        self.running = False
        if self.stop_error:
            raise self.stop_error


class MenuItem:
    def __init__(self, text, action, **options):
        self.text, self.action = text, action
        self.__dict__.update(options)


class Menu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class Tray:
    def __init__(self, _name, _image, _title, *, menu):
        self.menu = menu
        self.visible = False
        self.stops = 0
        self.updates = 0
        self.notifications = []
        self.detached_error = None
        self.stop_error = None

    def run_detached(self):
        if self.detached_error:
            raise self.detached_error
        self.visible = True

    def stop(self):
        self.stops += 1
        self.visible = False
        if self.stop_error:
            raise self.stop_error

    def update_menu(self):
        self.updates += 1

    def notify(self, *args):
        self.notifications.append(args)


class Startup:
    def __init__(self):
        self.reads = []
        self.writes = []
        self.enabled = True
        self.error = None
        self.updated = threading.Event()

    def startup_enabled(self):
        self.reads.append(threading.get_ident())
        if self.error:
            raise self.error
        return self.enabled

    def install_startup(self, *args, **kwargs):
        self.writes.append((args, kwargs, threading.get_ident()))
        self.updated.set()


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    instances = []
    trays = []

    class Instance:
        def __init__(self, data_dir, on_show):
            self.data_dir, self.on_show = data_dir, on_show
            self.closes = 0
            self.result = True
            self.focus_before_gui = False
            instances.append(self)

        def acquire(self):
            if self.focus_before_gui:
                self.on_show()
            return self.result

        def close(self):
            self.closes += 1

    def icon(*args, **kwargs):
        tray = Tray(*args, **kwargs)
        trays.append(tray)
        return tray

    monkeypatch.setattr(desktop, "SingleInstance", Instance)
    monkeypatch.setattr(desktop, "ensure_icon", lambda: tmp_path / "native.ico")
    monkeypatch.setattr(desktop, "create_tray_image", lambda: object())
    monkeypatch.setattr(desktop, "window_size", lambda _config: (1200, 800))
    monkeypatch.setattr(desktop, "desktop_python", lambda: tmp_path / "pythonw.exe")

    def build(**settings):
        config = replace(DesktopConfig(workspace=str(tmp_path)), **settings)
        app = DesktopApp(
            config, tmp_path / "app-data", server=Server(), webview_module=Webview(),
            tray_module=SimpleNamespace(MenuItem=MenuItem, Menu=Menu, Icon=icon),
            startup_module=Startup(),
        )
        app.test_instances = instances
        app.test_trays = trays
        return app

    return build


def test_x_hides_to_tray_and_keeps_backend_alive_until_explicit_exit(make_app):
    app = make_app()

    def interact(callback):
        callback()
        assert app.window.events.closing.handlers == [app.on_closing]
        assert app.on_closing() is False
        assert [call[0] for call in app.window.calls] == ["hide"]
        assert app.server.running and app.server.stops == 0
        assert not app.quitting.is_set()

    app.webview.on_start = interact
    assert app.run() == 0
    assert app.server.stops == 1 and app.instance.closes == 1
    assert app.test_trays[0].stops == 1


def test_explicit_quit_destroys_asynchronously_and_bypasses_hide_to_tray(make_app):
    app = make_app()
    caller = threading.get_ident()

    def interact(callback):
        callback()
        app.request_quit()
        assert app.window.destroyed.wait(1)
        app.request_quit()
        assert app.window.calls == [("destroy", app.window.calls[0][1])]
        assert app.window.calls[0][1] != caller
        assert app.on_closing() is True
        assert app.server.running and app.server.stops == 0

    app.webview.on_start = interact
    assert app.run() == 0
    assert app.server.stops == app.instance.closes == 1


def test_existing_instance_does_not_start_another_server_or_native_gui(make_app, monkeypatch):
    app = make_app()
    instance_type = desktop.SingleInstance

    def existing(*args):
        instance = instance_type(*args)
        instance.result = False
        return instance

    monkeypatch.setattr(desktop, "SingleInstance", existing)
    assert app.run() == 0
    assert app.server.starts == app.server.stops == 0
    assert not app.webview.created and not app.webview.starts
    assert not app.startup.reads and not app.test_trays


def test_native_host_forces_edgechromium_profile_icon_and_background_standby(make_app):
    app = make_app(start_minimized=True)
    assert app.run(background=True) == 0
    args, options = app.webview.created[0]
    assert args == (desktop.TITLE, app.server.url)
    assert options["hidden"] is True and options["confirm_close"] is False
    assert (options["width"], options["height"]) == (1200, 800)
    assert app.webview.starts == [{
        "gui": "edgechromium", "private_mode": False,
        "storage_path": str(app.data_dir / "webview"),
        "icon": str(app.data_dir.parent / "native.ico"), "debug": False,
    }]
    # Startup checks and service startup do not submit model work or change login settings.
    assert app.server.starts == 1 and not app.startup.writes


@pytest.mark.parametrize("phase", ["startup-status", "server", "create-window", "gui-loop"])
def test_startup_failure_always_releases_owned_backend_instance_and_tray(make_app, phase):
    app = make_app()
    failure = RuntimeError("Injected startup failure")
    if phase == "startup-status":
        app.startup.error = failure
    elif phase == "server":
        app.server.start_error = failure
    elif phase == "create-window":
        app.webview.create_error = failure
    else:
        app.webview.on_start = lambda _callback: (_ for _ in ()).throw(failure)
    with pytest.raises(RuntimeError, match="Injected startup failure"):
        app.run()
    assert app.server.stops == 1 and app.instance.closes == 1
    assert app.quitting.is_set()
    assert all(tray.stops == 1 for tray in app.test_trays)


@pytest.mark.parametrize("resource", ["tray", "server"])
def test_one_cleanup_failure_does_not_leak_other_owned_resources(make_app, resource):
    app = make_app()

    def fail_cleanup(callback):
        callback()
        target = app.tray if resource == "tray" else app.server
        target.stop_error = RuntimeError("Injected cleanup failure")

    app.webview.on_start = fail_cleanup
    # Logging/suppression versus propagation is a host policy; ownership cleanup is mandatory.
    try:
        app.run()
    except RuntimeError as error:
        assert "Injected cleanup failure" in str(error)
    assert app.test_trays[0].stops == 1
    assert app.server.stops == app.instance.closes == 1


def test_show_request_before_gui_ready_is_retained_and_delivered_after_start(make_app, monkeypatch):
    app = make_app()
    instance_type = desktop.SingleInstance

    def early_focus(*args):
        instance = instance_type(*args)
        instance.focus_before_gui = True
        return instance

    monkeypatch.setattr(desktop, "SingleInstance", early_focus)

    def interact(callback):
        assert app.pending_show.is_set()
        assert app.window.calls == []
        callback()
        assert [call[0] for call in app.window.calls] == ["show", "restore"]
        assert not app.pending_show.is_set()

    app.webview.on_start = interact
    assert app.run(background=True) == 0


@pytest.mark.parametrize("tray_visible,close_to_tray", [(False, True), (True, False)])
def test_x_exits_when_tray_is_invisible_or_preference_disables_hiding(make_app, tray_visible, close_to_tray):
    app = make_app(close_to_tray=close_to_tray)

    def interact(callback):
        callback()
        app.tray.visible = tray_visible
        assert app.on_closing() is True
        assert app.quitting.is_set() and not app.window.calls

    app.webview.on_start = interact
    assert app.run() == 0


def test_unavailable_tray_keeps_window_usable_and_x_exits(make_app, monkeypatch):
    app = make_app()

    def unavailable():
        raise RuntimeError("No system tray")

    monkeypatch.setattr(app, "_make_tray", unavailable)

    def interact(callback):
        callback()
        assert app.tray is None and app.on_closing() is True

    app.webview.on_start = interact
    assert app.run() == 0
    assert app.server.stops == app.instance.closes == 1


def test_tray_menu_reads_cached_startup_state_and_setting_changes_run_off_ui_thread(make_app):
    app = make_app()
    ui_thread = threading.get_ident()

    def interact(callback):
        callback()
        item = next(item for item in app.tray.menu.items if getattr(item, "text", "") == "登入 Windows 後啟動")
        assert item.checked(item) is True
        for _ in range(3):
            assert item.checked(item) is True
        assert app.startup.reads == [ui_thread]
        item.action()
        assert app.startup.updated.wait(1)
        worker = app.startup.writes[0][2]
        assert worker != ui_thread
        assert app.startup.writes[0][1] == {"enabled": False}
        # The cached menu callback never probes PowerShell or the filesystem.
        assert app.startup.reads == [ui_thread]

    app.webview.on_start = interact
    assert app.run() == 0


def test_quit_before_gui_ready_closes_immediately_after_gui_starts(make_app):
    app = make_app()
    app.request_quit()
    assert app.run() == 0
    assert app.window.destroyed.is_set()
    assert [call[0] for call in app.window.calls] == ["destroy"]


@pytest.mark.parametrize("start_minimized", [False, True])
def test_login_autostart_only_opens_or_hides_office_without_job_replay(tmp_path, monkeypatch, start_minimized):
    data_dir = tmp_path / "login-data"
    SettingsStore(data_dir).save(DesktopConfig(workspace=str(tmp_path), start_minimized=start_minimized))
    launches = []

    class StandbyApp:
        def __init__(self, config, selected_data_dir):
            assert config.workspace == str(tmp_path)
            assert selected_data_dir == data_dir

        def run(self, **options):
            launches.append(options)
            return 0

    monkeypatch.setattr(desktop, "_configure_logging", lambda _data_dir: None)
    monkeypatch.setattr(desktop.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(desktop, "DesktopApp", StandbyApp)
    assert desktop.main(["--data-dir", str(data_dir), "--autostart"]) == 0
    assert launches == [{"background": start_minimized, "smoke": False, "smoke_path": None}]


def test_importing_desktop_does_not_initialize_native_gui_packages():
    script = (
        "import desktop,json,sys; "
        "print(json.dumps([name for name in ('webview','pystray','clr','PIL') if name in sys.modules]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=True, shell=False,
    )
    assert json.loads(result.stdout) == []
