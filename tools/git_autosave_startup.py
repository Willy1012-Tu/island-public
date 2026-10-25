"""Manage only this project's current-user Git backup Startup shortcut.

Importing this module changes nothing. Enabling login startup and immediately
launching the watcher are separate explicit actions. No process is ever stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import windows_startup as windows

SHORTCUT_NAME = "Island Git Backup.lnk"
DESCRIPTION_PREFIX = "Island Git Backup managed watcher v1:"


def startup_shortcut_path() -> Path:
    return windows._known_folder(windows.FOLDERID_STARTUP) / SHORTCUT_NAME


def _paths(repo: Path | str, *, require_exists: bool) -> tuple[Path, Path]:
    root = Path(repo).expanduser().resolve()
    script = (root / "tools" / "git_autosave.py").resolve()
    if not script.is_relative_to(root):
        raise windows.ShortcutError("The Git backup watcher must remain inside its project.")
    if require_exists:
        if not root.is_dir() or not ((root / ".git").is_dir() or (root / ".git").is_file()):
            raise windows.ShortcutError("Choose the exact existing Git repository root.")
        if not script.is_file():
            raise windows.ShortcutError("The project's tools/git_autosave.py watcher was not found.")
    return root, script


def pythonw_for(repo: Path | str) -> Path:
    root = Path(repo).expanduser().resolve()
    preferred = root / ".venv-desktop" / "Scripts" / "pythonw.exe"
    if preferred.is_file():
        return preferred.resolve()
    current = Path(sys.executable).resolve()
    candidate = current if current.name.casefold() == "pythonw.exe" else current.with_name("pythonw.exe")
    if not candidate.is_file():
        raise windows.ShortcutError("A native pythonw.exe is required for the background Git watcher.")
    return candidate


def _arguments(root: Path, script: Path) -> str:
    return subprocess.list2cmdline([str(script), "--repo", str(root), "--watch"])


def _description(root: Path, script: Path) -> str:
    identity = os.path.normcase(str(root)) + "\n" + os.path.normcase(str(script))
    return DESCRIPTION_PREFIX + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _shortcut(path: Path | None) -> Path:
    selected = windows._path(path if path is not None else startup_shortcut_path(), suffix=".lnk")
    if selected.name.casefold() != SHORTCUT_NAME.casefold():
        raise windows.ShortcutError("Only the Island Git Backup shortcut may be managed by this helper.")
    return selected


def _owned(metadata: Any, root: Path, script: Path) -> bool:
    if not isinstance(metadata, dict):
        return False
    try:
        return (
            metadata.get("description") == _description(root, script)
            and metadata.get("arguments") == _arguments(root, script)
            and isinstance(metadata.get("target"), str)
            and Path(metadata["target"]).name.casefold() == "pythonw.exe"
            and isinstance(metadata.get("working_directory"), str)
            and windows._same_path(metadata["working_directory"], root)
        )
    except (OSError, ValueError):
        return False


def status(repo: Path | str = PROJECT, *, shortcut_path: Path | None = None) -> dict[str, Any]:
    root, script = _paths(repo, require_exists=False)
    path = _shortcut(shortcut_path)
    metadata = windows._read_shortcut(path)
    owned = _owned(metadata, root, script)
    usable = owned and script.is_file() and Path(metadata["target"]).is_file() and (
        (root / ".git").is_dir() or (root / ".git").is_file())
    reason = "" if usable else "foreign_shortcut" if metadata and not owned else "broken_launcher" if owned else "not_installed"
    return {"repo": str(root), "shortcut": str(path), "installed": bool(metadata),
            "owned": owned, "enabled": bool(usable), "reason": reason,
            "target": metadata["target"] if owned else None}


def install(repo: Path | str = PROJECT, *, enabled: bool = True,
            shortcut_path: Path | None = None) -> Path:
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean.")
    root, script = _paths(repo, require_exists=enabled)
    path = _shortcut(shortcut_path)
    metadata = windows._read_shortcut(path)
    if metadata is not None and not _owned(metadata, root, script):
        raise windows.ShortcutError("Refusing to change a Git backup shortcut owned by another project or application.")
    target = pythonw_for(root) if enabled else None
    arguments, description = _arguments(root, script), _description(root, script)
    literal = windows._ps_literal
    output = windows._powershell(f"""
$path = {literal(path)}
$target = {literal(target or '')}
$arguments = {literal(arguments)}
$workingDirectory = {literal(root)}
$description = {literal(description)}
$shell = New-Object -ComObject WScript.Shell
function Assert-OwnedBackup($link) {{
    $equal = [System.StringComparer]::OrdinalIgnoreCase
    if ($link.Description -cne $description -or $link.Arguments -cne $arguments -or
        -not $equal.Equals([System.IO.Path]::GetFullPath($link.WorkingDirectory), $workingDirectory) -or
        -not $equal.Equals([System.IO.Path]::GetFileName($link.TargetPath), 'pythonw.exe')) {{
        throw 'Refusing to change a Git backup shortcut owned by another project or application.'
    }}
}}
$exists = Test-Path -LiteralPath $path
if ($exists) {{
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {{ throw 'Git backup shortcut path is not a file.' }}
    if ((Get-Item -LiteralPath $path -Force).LinkType -in @('SymbolicLink', 'Junction')) {{
        throw 'Refusing to change a symbolic-link Git backup shortcut.'
    }}
    Assert-OwnedBackup ($shell.CreateShortcut($path))
}}
if ({'$true' if enabled else '$false'}) {{
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path)) | Out-Null
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $target
    $link.Arguments = $arguments
    $link.WorkingDirectory = $workingDirectory
    $link.Description = $description
    $link.WindowStyle = 7
    $link.Save()
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {{ throw 'Windows did not save the Git backup shortcut.' }}
    $confirmed = $shell.CreateShortcut($path)
    Assert-OwnedBackup $confirmed
    if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals([System.IO.Path]::GetFullPath($confirmed.TargetPath), $target)) {{
        throw 'Windows did not preserve the Git backup interpreter path.'
    }}
}} elseif ($exists) {{
    [System.IO.File]::Delete($path)
    if (Test-Path -LiteralPath $path) {{ throw 'Windows did not remove the Git backup shortcut.' }}
}}
[pscustomobject]@{{ path = $path; enabled = {'$true' if enabled else '$false'} }} | ConvertTo-Json -Compress
""")
    try:
        result = json.loads(output)
    except (TypeError, ValueError) as error:
        raise windows.ShortcutError("Windows did not confirm the Git backup shortcut change.") from error
    if (not isinstance(result, dict) or result.get("enabled") is not enabled
            or not isinstance(result.get("path"), str) or not windows._same_path(result["path"], path)):
        raise windows.ShortcutError("Windows returned an unexpected Git backup shortcut result.")
    return path


def start_watcher(repo: Path | str = PROJECT) -> dict[str, Any]:
    """Request a windowless launch; the watcher's own Git lock rejects duplicates."""
    if sys.platform != "win32":
        raise windows.ShortcutError("The background Git startup launcher requires Windows.")
    root, script = _paths(repo, require_exists=True)
    target = pythonw_for(root)
    try:
        process = subprocess.Popen(
            [str(target), str(script), "--repo", str(root), "--watch"], cwd=root, shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=windows.CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200),
        )
    except OSError as error:
        raise windows.ShortcutError("Windows could not launch the Git backup watcher.") from error
    return {"requested": True, "pid": process.pid}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=PROJECT, help="exact project Git repository root")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--enable", action="store_true", help="create this project's current-user login shortcut")
    action.add_argument("--disable", action="store_true", help="remove only this project's owned login shortcut")
    action.add_argument("--status", action="store_true", help="inspect startup without launching or changing it")
    parser.add_argument("--start", action="store_true", help="also request a hidden watcher launch with --enable")
    args = parser.parse_args(argv)
    if args.start and not args.enable:
        parser.error("--start is only supported together with --enable")
    try:
        if args.enable or args.disable:
            install(args.repo, enabled=args.enable)
        result = status(args.repo)
        result["launch"] = start_watcher(args.repo) if args.start else None
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, windows.ShortcutError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
