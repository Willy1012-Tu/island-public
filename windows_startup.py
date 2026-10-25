"""Per-user Windows shortcuts for Agent Desk; importing this module changes nothing.

Known Folder IDs honor redirected/OneDrive desktops. WScript.Shell writes only a
single explicit .lnk, without registry changes. Existing shortcuts must match our
ownership marker, executable, arguments, and working directory before mutation.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

SHORTCUT_NAME = "Agent Desk.lnk"
FOLDERID_STARTUP = "B97D20BB-F46A-4C97-BA10-5E3608430854"
FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
DESCRIPTION_PREFIX = "Agent Desk managed desktop launcher v1:"
AUTOSTART_FLAG = "--autostart"
CREATE_NO_WINDOW = 0x08000000


class ShortcutError(RuntimeError):
    """Windows could not safely inspect, create, or remove our shortcut."""


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
    ]


def _known_folder(identifier: str) -> Path:
    if sys.platform != "win32":
        raise ShortcutError("Windows shortcuts require Windows.")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    get_path = shell32.SHGetKnownFolderPath
    get_path.argtypes = [ctypes.POINTER(_GUID), ctypes.c_uint32,
                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get_path.restype = ctypes.c_long
    free = ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    folder = _GUID.from_buffer_copy(uuid.UUID(identifier).bytes_le)
    buffer = ctypes.c_void_p()
    result = get_path(ctypes.byref(folder), 0, None, ctypes.byref(buffer))
    try:
        if result != 0 or not buffer.value:
            raise ShortcutError(f"Windows could not locate the requested user folder (0x{result & 0xffffffff:08x}).")
        return Path(ctypes.wstring_at(buffer)).resolve()
    finally:
        # The Windows API requires freeing even a failed call's non-null buffer.
        if buffer.value:
            free(buffer)


def startup_shortcut_path() -> Path:
    """Return the current user's Startup shortcut path, without creating it."""
    return _known_folder(FOLDERID_STARTUP) / SHORTCUT_NAME


def desktop_shortcut_path() -> Path:
    """Return the actual Windows Desktop path, including OneDrive redirection."""
    return _known_folder(FOLDERID_DESKTOP) / SHORTCUT_NAME


def _ps_literal(value: str | Path) -> str:
    """A PowerShell single-quoted literal: no interpolation or command execution."""
    value = str(value)
    if "\x00" in value:
        raise ValueError("A shortcut path cannot contain a NUL character.")
    return "'" + value.replace("'", "''") + "'"


def _path(value: Path, *, suffix: str | None = None) -> Path:
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise ShortcutError("Refusing to change a symbolic-link shortcut path.")
    path = path.resolve()
    if suffix and path.suffix.lower() != suffix:
        raise ValueError(f"The shortcut must use the {suffix} extension.")
    if path.exists() and not path.is_file():
        raise ShortcutError("The shortcut path is occupied by a directory.")
    return path


def _windowless_python(executable: Path, *, require_exists: bool) -> Path:
    executable = Path(executable).expanduser().resolve()
    if executable.name.lower() == "python.exe":
        executable = executable.with_name("pythonw.exe")
    if executable.name.lower() != "pythonw.exe":
        raise ValueError("Choose python.exe or pythonw.exe for the desktop launcher.")
    if require_exists and not executable.is_file():
        raise ShortcutError(f"The windowless Python executable was not found: {executable}")
    return executable


def _description(script: Path, *, startup: bool) -> str:
    identity = os.path.normcase(str(script.resolve())).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return DESCRIPTION_PREFIX + ("startup:" if startup else "desktop:") + digest


def _arguments(script: Path, *, startup: bool) -> str:
    return subprocess.list2cmdline([str(script)] + ([AUTOSTART_FLAG] if startup else []))


def _powershell(script: str) -> str:
    """Run an encoded, noninteractive script; never interpolate through cmd.exe."""
    header = "$ErrorActionPreference = 'Stop'\n[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    encoded = base64.b64encode((header + script).encode("utf-16le")).decode("ascii")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    argv = [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    try:
        result = subprocess.run(
            argv, shell=False, capture_output=True, encoding="utf-8", errors="replace",
            timeout=20, check=False,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ShortcutError("Windows shortcut management could not run.") from error
    if result.returncode:
        message = result.stderr.strip() or "Windows rejected the shortcut operation."
        raise ShortcutError(message[:1200])
    return result.stdout.strip().lstrip("\ufeff")


def _read_shortcut(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    output = _powershell(f"""
$path = {_ps_literal(path)}
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {{ Write-Output 'null'; exit }}
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($path)
[pscustomobject]@{{ target = [string]$link.TargetPath; arguments = [string]$link.Arguments;
    working_directory = [string]$link.WorkingDirectory; description = [string]$link.Description;
    icon = [string]$link.IconLocation }} | ConvertTo-Json -Compress
""")
    try:
        data = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise ShortcutError("Windows returned invalid shortcut metadata.") from error
    if data is None:
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(key), str) for key in
                                            ("target", "arguments", "working_directory", "description")):
        raise ShortcutError("Windows returned incomplete shortcut metadata.")
    return data


def _same_path(first: str | Path, second: str | Path) -> bool:
    return os.path.normcase(str(Path(first).resolve())) == os.path.normcase(str(Path(second).resolve()))


def startup_enabled(pathoverride: Path | None = None) -> bool:
    """Report a valid managed autostart link, rather than mere .lnk existence.

    The installed script may live in a relocated copy. Its recorded path must
    still exist, match our script-specific marker, and use the autostart flag.
    No program is launched while checking this setting.
    """
    try:
        path = _path(pathoverride if pathoverride is not None else startup_shortcut_path(), suffix=".lnk")
        metadata = _read_shortcut(path)
        if not metadata:
            return False
        arguments = metadata["arguments"]
        suffix = " " + AUTOSTART_FLAG
        if not arguments.endswith(suffix):
            return False
        recorded = arguments[:-len(suffix)]
        if recorded.startswith('"') and recorded.endswith('"'):
            recorded = recorded[1:-1]
        script = Path(recorded).resolve()
        target = Path(metadata["target"])
        return (
            script.suffix.lower() == ".py" and script.is_file()
            and target.name.lower() == "pythonw.exe" and target.is_file()
            and arguments == _arguments(script, startup=True)
            and metadata["description"] == _description(script, startup=True)
            and _same_path(metadata["working_directory"], script.parent)
        )
    except (OSError, ValueError, ShortcutError):
        return False


def _install(python_executable: Path, script: Path, path: Path, *, startup: bool, enabled: bool) -> Path:
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean.")
    path = _path(path, suffix=".lnk")
    script = Path(script).expanduser().resolve()
    if script.suffix.lower() != ".py":
        raise ValueError("The desktop launcher must be a Python script.")
    if enabled and not script.is_file():
        raise ShortcutError(f"The desktop launcher was not found: {script}")
    target = _windowless_python(python_executable, require_exists=enabled)
    arguments = _arguments(script, startup=startup)
    description = _description(script, startup=startup)
    # Icon assets travel with the app; fallback works before an icon is created.
    icon = script.parent / "assets" / "agentdesk.ico"
    icon_location = str(icon if icon.is_file() else target) + ",0"
    operation = "create" if enabled else "remove"
    output = _powershell(f"""
$path = {_ps_literal(path)}
$target = {_ps_literal(target)}
$arguments = {_ps_literal(arguments)}
$workingDirectory = {_ps_literal(script.parent)}
$description = {_ps_literal(description)}
$shell = New-Object -ComObject WScript.Shell
function Assert-OwnedShortcut($link) {{
    $pathsEqual = [System.StringComparer]::OrdinalIgnoreCase
    if ($link.Description -cne $description -or $link.Arguments -cne $arguments -or
        -not $pathsEqual.Equals([System.IO.Path]::GetFullPath($link.TargetPath), $target) -or
        -not $pathsEqual.Equals([System.IO.Path]::GetFullPath($link.WorkingDirectory), $workingDirectory)) {{
        throw 'Refusing to change a shortcut owned by another application or launcher.'
    }}
}}
$exists = Test-Path -LiteralPath $path
if ($exists) {{
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {{ throw 'Shortcut path is not a file.' }}
    # Cloud-synced OneDrive files may also have ReparsePoint attributes. Reject
    # filesystem redirects, not ordinary hydrated cloud shortcut files.
    if ((Get-Item -LiteralPath $path -Force).LinkType -in @('SymbolicLink', 'Junction')) {{
        throw 'Refusing to change a symbolic-link shortcut.'
    }}
    Assert-OwnedShortcut ($shell.CreateShortcut($path))
}}
if ({'$true' if enabled else '$false'}) {{
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path)) | Out-Null
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $target
    $link.Arguments = $arguments
    $link.WorkingDirectory = $workingDirectory
    $link.Description = $description
    $link.WindowStyle = 1
    $link.IconLocation = {_ps_literal(icon_location)}
    $link.Save()
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {{ throw 'Windows did not save the shortcut.' }}
    Assert-OwnedShortcut ($shell.CreateShortcut($path))
}} elseif ($exists) {{
    [System.IO.File]::Delete($path)
    if (Test-Path -LiteralPath $path) {{ throw 'Windows did not remove the shortcut.' }}
}}
[pscustomobject]@{{ path = $path; operation = '{operation}'; enabled = {'$true' if enabled else '$false'} }} | ConvertTo-Json -Compress
""")
    try:
        result: Any = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise ShortcutError("Windows did not confirm the shortcut operation.") from error
    if (not isinstance(result, dict) or result.get("enabled") is not enabled
            or not isinstance(result.get("path"), str) or not _same_path(result["path"], path)):
        raise ShortcutError("Windows returned an unexpected shortcut result.")
    return path


def install_startup(python_executable: Path, script: Path, *, enabled: bool = True,
                    shortcut_path: Path | None = None) -> Path:
    """Enable/disable this launcher at user login; no task starts automatically.

    The shortcut invokes ``pythonw.exe desktop.py --autostart``. The desktop app
    decides whether its window is shown; this helper never launches the app.
    A conflicting shortcut is refused, including when disabling startup.
    """
    return _install(python_executable, script,
                    shortcut_path if shortcut_path is not None else startup_shortcut_path(),
                    startup=True, enabled=enabled)


def install_desktop_shortcut(python_executable: Path, script: Path, *,
                             shortcut_path: Path | None = None) -> Path:
    """Create the normal foreground desktop launcher, without startup flags."""
    return _install(python_executable, script,
                    shortcut_path if shortcut_path is not None else desktop_shortcut_path(),
                    startup=False, enabled=True)
