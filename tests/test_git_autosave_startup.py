"""Backup-launcher checks never change current-user Startup or launch a watcher."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import git_autosave_startup as startup


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "工作 專案's $() `repo"
    (root / "tools").mkdir(parents=True)
    (root / ".git").mkdir()
    script = root / "tools" / "git_autosave.py"
    script.write_text("# Fixture, never executed.\n", encoding="utf-8")
    interpreter = root / ".venv-desktop" / "Scripts" / "pythonw.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"not executable")
    shortcut = tmp_path / "Startup 使用者" / startup.SHORTCUT_NAME
    metadata = {"value": None}
    calls = []
    monkeypatch.setattr(startup.windows, "_known_folder", lambda identifier: shortcut.parent)
    monkeypatch.setattr(startup.windows, "_read_shortcut", lambda path: metadata["value"])

    def powershell(source):
        calls.append(source)
        enabled = "if ($true)" in source
        if enabled:
            metadata["value"] = {
                "target": str(interpreter), "arguments": startup._arguments(root, script),
                "working_directory": str(root), "description": startup._description(root, script),
            }
        else:
            metadata["value"] = None
        return json.dumps({"path": str(shortcut), "enabled": enabled})

    monkeypatch.setattr(startup.windows, "_powershell", powershell)
    return SimpleNamespace(root=root, script=script, interpreter=interpreter,
                           shortcut=shortcut, metadata=metadata, calls=calls)


def owned(project, **changes):
    return {"target": str(project.interpreter), "arguments": startup._arguments(project.root, project.script),
            "working_directory": str(project.root),
            "description": startup._description(project.root, project.script), **changes}


def test_startup_path_is_separate_from_agent_desk_and_honors_native_known_folder(project, monkeypatch):
    folders = []

    def known(identifier):
        folders.append(identifier)
        return project.shortcut.parent

    monkeypatch.setattr(startup.windows, "_known_folder", known)
    assert startup.startup_shortcut_path() == project.shortcut
    assert folders == [startup.windows.FOLDERID_STARTUP]
    assert startup.startup_shortcut_path().name != startup.windows.SHORTCUT_NAME


def test_enable_uses_literal_unicode_and_fixed_watch_arguments_without_launching(project, monkeypatch):
    monkeypatch.setattr(startup.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Enable must not launch a process"))
    assert startup.install(project.root) == project.shortcut.resolve()
    assert len(project.calls) == 1
    source = project.calls[0]
    assert "$target = " + startup.windows._ps_literal(project.interpreter) in source
    argv = [str(project.script), "--repo", str(project.root), "--watch"]
    assert "$arguments = " + startup.windows._ps_literal(subprocess.list2cmdline(argv)) in source
    assert "$workingDirectory = " + startup.windows._ps_literal(project.root) in source
    assert "Assert-OwnedBackup ($shell.CreateShortcut($path))" in source
    assert source.index("Assert-OwnedBackup ($shell.CreateShortcut($path))") < source.index("$link.Save()")
    assert "$link.WindowStyle = 7" in source
    assert "Start-Process" not in source and "Agent Desk.lnk" not in source
    assert not project.shortcut.exists(), "All native settings operations were faked"
    state = startup.status(project.root)
    assert state["enabled"] is state["owned"] is state["installed"] is True


@pytest.mark.parametrize("change", [
    {"description": "Foreign application"}, {"arguments": "other.py --watch"},
    {"arguments": "git_autosave.py --repo somewhere --watch --extra"},
    {"working_directory": "C:\\another-project"}, {"target": "C:\\other.exe"},
])
def test_foreign_shortcut_is_refused_for_enable_and_disable(project, change):
    project.metadata["value"] = owned(project, **change)
    assert startup.status(project.root)["reason"] == "foreign_shortcut"
    for enabled in (True, False):
        with pytest.raises(startup.windows.ShortcutError, match="another project or application"):
            startup.install(project.root, enabled=enabled)
    assert project.calls == []


def test_marker_is_bound_to_both_repository_and_script(project, tmp_path):
    expected = startup._description(project.root, project.script)
    assert expected != startup._description(tmp_path, project.script)
    assert expected != startup._description(project.root, project.root / "another.py")
    assert startup._owned(owned(project), project.root, project.script)


def test_disable_removes_only_owned_link_even_after_watcher_or_python_is_missing(project):
    project.metadata["value"] = owned(project)
    project.script.unlink()
    project.interpreter.unlink()
    assert startup.status(project.root)["reason"] == "broken_launcher"
    assert startup.install(project.root, enabled=False) == project.shortcut.resolve()
    assert len(project.calls) == 1
    source = project.calls[0]
    assert "if ($false)" in source
    assert source.index("Assert-OwnedBackup ($shell.CreateShortcut($path))") < source.index("[System.IO.File]::Delete($path)")
    assert startup.status(project.root)["enabled"] is False


def test_status_does_not_write_shortcuts_or_launch_any_process(project, monkeypatch):
    monkeypatch.setattr(startup.windows, "_powershell", lambda *_: pytest.fail("No mutation transport on status"))
    monkeypatch.setattr(startup.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("No launch on status"))
    result = startup.status(project.root)
    assert result == {
        "repo": str(project.root), "shortcut": str(project.shortcut),
        "installed": False, "owned": False, "enabled": False,
        "reason": "not_installed", "target": None,
    }


def test_pythonw_prefers_project_venv_then_current_interpreter_sibling(project, tmp_path, monkeypatch):
    assert startup.pythonw_for(project.root) == project.interpreter
    base = tmp_path / "Python 本機"
    base.mkdir()
    console = base / "python.exe"
    windowless = base / "pythonw.exe"
    console.write_bytes(b"fake")
    windowless.write_bytes(b"fake")
    project.interpreter.unlink()
    monkeypatch.setattr(startup.sys, "executable", str(console))
    assert startup.pythonw_for(project.root) == windowless
    monkeypatch.setattr(startup.sys, "executable", str(windowless))
    assert startup.pythonw_for(project.root) == windowless
    windowless.unlink()
    with pytest.raises(startup.windows.ShortcutError, match="pythonw.exe"):
        startup.pythonw_for(project.root)


def test_immediate_launch_is_windowless_native_argv_and_does_not_stop_or_wait(project, monkeypatch):
    calls = []
    monkeypatch.setattr(startup.sys, "platform", "win32")

    def popen(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(startup.subprocess, "Popen", popen)
    assert startup.start_watcher(project.root) == {"requested": True, "pid": 12345}
    argv, options = calls[0]
    assert argv == [str(project.interpreter), str(project.script), "--repo", str(project.root), "--watch"]
    assert options["cwd"] == project.root and options["shell"] is False
    assert options["creationflags"] & startup.windows.CREATE_NO_WINDOW
    assert options["creationflags"] & 0x200
    assert all(options[key] == subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
    assert options["close_fds"] is True
    assert not project.calls, "Launching alone never rewrites Startup"


def test_cannot_manage_agent_desk_shortcut_even_with_a_forged_matching_marker(project):
    project.metadata["value"] = owned(project)
    forbidden = project.shortcut.with_name("Agent Desk.lnk")
    with pytest.raises(startup.windows.ShortcutError, match="Only the Island Git Backup"):
        startup.install(project.root, shortcut_path=forbidden)
    assert project.calls == []


@pytest.mark.parametrize("kind", ["not-repo", "missing-watcher"])
def test_enable_validates_project_before_native_actions(project, kind):
    if kind == "not-repo":
        (project.root / ".git").rmdir()
    else:
        project.script.unlink()
    with pytest.raises(startup.windows.ShortcutError):
        startup.install(project.root)
    assert not project.calls


@pytest.mark.parametrize("reply", ["invalid-json", "[]", '{"enabled":false}', '{"enabled":true,"path":"C:\\\\other.lnk"}'])
def test_enable_requires_matching_native_confirmation(project, monkeypatch, reply):
    monkeypatch.setattr(startup.windows, "_powershell", lambda _source: reply)
    with pytest.raises(startup.windows.ShortcutError, match="confirm|unexpected"):
        startup.install(project.root)


def test_cli_enable_start_and_disable_are_explicit_json_only(project, monkeypatch, capsys):
    launched = []
    monkeypatch.setattr(startup, "start_watcher", lambda repo: launched.append(repo) or {"requested": True, "pid": 88})
    assert startup.main(["--repo", str(project.root), "--enable", "--start"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["enabled"] and first["launch"] == {"requested": True, "pid": 88}
    assert launched == [project.root]
    assert startup.main(["--repo", str(project.root), "--disable"]) == 0
    disabled = json.loads(capsys.readouterr().out)
    assert not disabled["enabled"] and disabled["launch"] is None
    assert launched == [project.root], "Disabling startup never terminates or relaunches workers"


def test_cli_status_is_read_only_and_errors_are_json(project, monkeypatch, capsys):
    assert startup.main(["--repo", str(project.root), "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["launch"] is None
    assert not project.calls
    monkeypatch.setattr(startup, "install", lambda *args, **kwargs: (_ for _ in ()).throw(startup.windows.ShortcutError("Foreign shortcut refused")))
    assert startup.main(["--repo", str(project.root), "--enable"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "Foreign shortcut refused"}


@pytest.mark.parametrize("action", ["--status", "--disable"])
def test_cli_start_modifier_cannot_sneak_mutation_into_status_or_disable(project, action):
    with pytest.raises(SystemExit) as error:
        startup.main(["--repo", str(project.root), action, "--start"])
    assert error.value.code == 2
    assert not project.calls
