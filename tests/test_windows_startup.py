"""Shortcut tests never write current-user Desktop/Startup or launch an app."""

from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import windows_startup as startup


@pytest.fixture
def launcher(tmp_path):
    directory = tmp_path / "測試 工作室's $() `目錄"
    directory.mkdir()
    script = directory / "desktop.py"
    script.write_text("# not executed\n", encoding="utf-8")
    python = directory / "python.exe"
    python.write_bytes(b"not executable")
    pythonw = directory / "pythonw.exe"
    pythonw.write_bytes(b"not executable")
    return SimpleNamespace(root=directory, script=script, python=python, pythonw=pythonw,
                           shortcut=directory / "Agent Desk.lnk")


def metadata(launcher, **overrides):
    return {"target": str(launcher.pythonw), "arguments": startup._arguments(launcher.script, startup=True),
            "working_directory": str(launcher.root),
            "description": startup._description(launcher.script, startup=True), "icon": "", **overrides}


def capture_powershell(monkeypatch, output):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(output, ensure_ascii=False), stderr="")

    monkeypatch.setattr(startup.subprocess, "run", run)
    return calls


def decoded(call):
    argv, _ = call
    return base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16le")


def test_known_folder_ids_honor_redirected_desktop_without_english_path_guess(monkeypatch, tmp_path):
    requested = []
    redirected = tmp_path / "OneDrive" / "桌面"
    start_folder = tmp_path / "使用者啟動"

    def known(identifier):
        requested.append(identifier)
        return redirected if identifier == startup.FOLDERID_DESKTOP else start_folder

    monkeypatch.setattr(startup, "_known_folder", known)
    assert startup.desktop_shortcut_path() == redirected / "Agent Desk.lnk"
    assert startup.startup_shortcut_path() == start_folder / "Agent Desk.lnk"
    assert requested == [startup.FOLDERID_DESKTOP, startup.FOLDERID_STARTUP]


def test_native_known_folder_reads_unicode_and_frees_even_failure_buffer(monkeypatch, tmp_path):
    path = str(tmp_path / "OneDrive" / "桌面")
    buffer = ctypes.create_unicode_buffer(path)
    freed = []
    calls = []
    result = [0]

    class Function:
        def __init__(self, callback): self.callback = callback
        def __call__(self, *args): return self.callback(*args)

    def get_path(folder, flags, token, output):
        calls.append((bytes(ctypes.cast(folder, ctypes.POINTER(startup._GUID)).contents), flags, token))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        return result[0]

    shell = SimpleNamespace(SHGetKnownFolderPath=Function(get_path))
    ole = SimpleNamespace(CoTaskMemFree=Function(lambda pointer: freed.append(pointer.value)))
    monkeypatch.setattr(startup.sys, "platform", "win32")
    monkeypatch.setattr(startup.ctypes, "WinDLL", lambda name, **kwargs: shell if name == "shell32" else ole, raising=False)
    assert startup._known_folder(startup.FOLDERID_DESKTOP) == Path(path).resolve()
    assert calls[0] == (uuid.UUID(startup.FOLDERID_DESKTOP).bytes_le, 0, None)
    assert freed == [ctypes.addressof(buffer)]
    result[0] = -2147467259
    with pytest.raises(startup.ShortcutError, match="could not locate"):
        startup._known_folder(startup.FOLDERID_STARTUP)
    assert freed == [ctypes.addressof(buffer), ctypes.addressof(buffer)]


def test_startup_uses_pythonw_autostart_absolute_argv_and_literal_unicode(monkeypatch, launcher):
    calls = capture_powershell(monkeypatch, {"path": str(launcher.shortcut), "enabled": True})
    result = startup.install_startup(launcher.python, launcher.script, shortcut_path=launcher.shortcut)
    assert result == launcher.shortcut.resolve()
    assert len(calls) == 1
    argv, options = calls[0]
    assert isinstance(argv, list) and argv[-2] == "-EncodedCommand"
    assert options["shell"] is False
    assert options["timeout"] == 20
    script = decoded(calls[0])
    assert str(launcher.pythonw).replace("'", "''") in script
    assert str(launcher.root).replace("'", "''") in script
    assert startup._ps_literal(startup._arguments(launcher.script, startup=True)) in script
    assert "--autostart" in script and "--background" not in script
    assert "Assert-OwnedShortcut ($shell.CreateShortcut($path))" in script
    assert "$link.WindowStyle = 1" in script
    assert "[System.IO.File]::Delete($path)" in script
    assert "-ExecutionPolicy" not in argv
    assert not launcher.shortcut.exists(), "mocked shortcut creation must not touch any actual Startup folder"


def test_desktop_has_no_startup_flag_and_uses_portable_icon_when_present(monkeypatch, launcher):
    icons = launcher.root / "assets"
    icons.mkdir()
    icon = icons / "agentdesk.ico"
    icon.write_bytes(b"icon fixture only")
    calls = capture_powershell(monkeypatch, {"path": str(launcher.shortcut), "enabled": True})
    startup.install_desktop_shortcut(launcher.pythonw, launcher.script, shortcut_path=launcher.shortcut)
    script = decoded(calls[0])
    assert "$arguments = " + startup._ps_literal(startup._arguments(launcher.script, startup=False)) in script
    assert "--autostart" not in script
    assert startup._ps_literal(str(icon) + ",0") in script


def test_disabling_keeps_ownership_checks_and_can_remove_a_broken_owned_launcher(monkeypatch, launcher):
    calls = capture_powershell(monkeypatch, {"path": str(launcher.shortcut), "enabled": False})
    launcher.script.unlink()
    launcher.pythonw.unlink()
    assert startup.install_startup(launcher.python, launcher.script, enabled=False,
                                   shortcut_path=launcher.shortcut) == launcher.shortcut.resolve()
    script = decoded(calls[0])
    assert "if ($false)" in script
    assert "$link.Description -cne $description" in script
    assert "$link.Arguments -cne $arguments" in script
    assert "GetFullPath($link.TargetPath), $target" in script
    assert "GetFullPath($link.WorkingDirectory), $workingDirectory" in script
    assert script.index("Assert-OwnedShortcut ($shell.CreateShortcut($path))") < script.index("[System.IO.File]::Delete($path)")


@pytest.mark.parametrize("changed", [
    {"description": "Some other application's shortcut"},
    {"arguments": "desktop.py --background"},
    {"arguments": "desktop.py --autostart --unexpected"},
    {"target": "not-python.exe"},
    {"working_directory": "not-the-workspace"},
])
def test_enabled_rejects_foreign_wrong_target_or_wrong_flags(monkeypatch, launcher, changed):
    launcher.shortcut.write_bytes(b"shortcut fixture")
    monkeypatch.setattr(startup, "_read_shortcut", lambda path: metadata(launcher, **changed))
    assert startup.startup_enabled(launcher.shortcut) is False


def test_enabled_checks_actual_owned_metadata_and_never_launches_the_target(monkeypatch, launcher):
    launcher.shortcut.write_bytes(b"shortcut fixture")
    calls = capture_powershell(monkeypatch, metadata(launcher))
    assert startup.startup_enabled(launcher.shortcut) is True
    assert len(calls) == 1
    script = decoded(calls[0])
    assert "CreateShortcut($path)" in script
    assert ".Save()" not in script and "Start-Process" not in script
    assert "ConvertTo-Json" in script


def test_conflict_or_timeout_is_reported_without_fallback_delete(monkeypatch, launcher):
    calls = []

    def foreign(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="Refusing to change a shortcut owned by another application.")

    monkeypatch.setattr(startup.subprocess, "run", foreign)
    with pytest.raises(startup.ShortcutError, match="another application"):
        startup.install_startup(launcher.python, launcher.script, enabled=False,
                                shortcut_path=launcher.shortcut)
    assert len(calls) == 1

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(startup.subprocess, "run", timeout)
    with pytest.raises(startup.ShortcutError, match="could not run"):
        startup.install_desktop_shortcut(launcher.python, launcher.script, shortcut_path=launcher.shortcut)


def test_invalid_paths_and_missing_pythonw_fail_before_any_subprocess(monkeypatch, launcher):
    monkeypatch.setattr(startup.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="extension"):
        startup.install_startup(launcher.python, launcher.script, shortcut_path=launcher.root / "wrong.txt")
    with pytest.raises(ValueError, match="NUL"):
        startup._ps_literal("bad\x00name")
    launcher.pythonw.unlink()
    with pytest.raises(startup.ShortcutError, match="windowless"):
        startup.install_startup(launcher.python, launcher.script, shortcut_path=launcher.shortcut)
