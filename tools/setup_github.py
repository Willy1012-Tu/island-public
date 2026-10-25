"""Connect an already committed local repository to a private GitHub repository.

Authentication stays with the native GitHub CLI. This helper never requests,
reads, or prints an access token, and never force-pushes or changes user identity.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_FIELDS = "nameWithOwner,isPrivate,viewerPermission,isEmpty,isArchived,url"


class SetupError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _redact(value: str) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"\b(?:github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|sk-(?:proj-)?[A-Za-z0-9_-]{12,})", "[REDACTED]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
    value = re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1[REDACTED]@", value)
    return "".join(character for character in value[:4096] if ord(character) >= 32 or character in "\n\t")


def _safe(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _run_command(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
                 capture: bool = True) -> subprocess.CompletedProcess[str]:
    options: dict[str, Any] = {"cwd": cwd, "env": env, "timeout": timeout,
                               "check": False, "shell": False}
    if capture:
        options.update(capture_output=True, text=True, encoding="utf-8", errors="replace")
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(argv, **options)


def _native_tool(value: str | None) -> str | None:
    if value and os.name == "nt" and Path(value).suffix.lower() in (".cmd", ".bat", ".ps1"):
        return None
    return value


def discover_gh(project_root: Path = PROJECT_ROOT) -> str | None:
    found = _native_tool(shutil.which("gh"))
    if found:
        return found
    candidates = [*project_root.glob(".tools/gh-*/bin/gh.exe"), *project_root.glob(".tools/gh-*/bin/gh")]
    candidates.sort(key=lambda path: tuple(int(part) for part in re.findall(r"\d+", path.parents[1].name)), reverse=True)
    return str(next((path for path in candidates if path.is_file()), "")) or None


def _repository(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or value.count("/") != 1:
        raise SetupError("invalid_repository_name", "Use a GitHub repository name in owner/name format.")
    owner, name = value.split("/", 1)
    if (not _OWNER.fullmatch(owner) or not _NAME.fullmatch(name) or name in (".", "..")
            or name.lower().endswith(".git")):
        raise SetupError("invalid_repository_name", "Use owner/name without URLs, credentials, or a .git suffix.")
    return owner, name


def _remote_identity(value: str) -> tuple[str, str] | None:
    if any(character.isspace() or ord(character) < 32 for character in value):
        return None
    if value.startswith("git@github.com:"):
        path = value[len("git@github.com:"):]
    else:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            return None
        path = parsed.path.removeprefix("/")
    if path.endswith(".git"):
        path = path[:-4]
    try:
        owner, name = _repository(path)
    except SetupError:
        return None
    return owner.lower(), name.lower()


class GitHubSetup:
    def __init__(self, repo: Path | str = PROJECT_ROOT, *, runner: Callable[..., Any] | None = None,
                 gh: str | None = None, git: str | None = None):
        self.repo = Path(repo).resolve()
        self.runner = runner or _run_command
        self.gh = _native_tool(gh) if gh is not None else discover_gh()
        self.git = _native_tool(git if git is not None else shutil.which("git"))

    def _env(self, *, interactive: bool = False) -> dict[str, str]:
        env = dict(os.environ)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                     "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            env.pop(name, None)
        env["GH_HOST"] = "github.com"
        if interactive:
            env.pop("GH_PROMPT_DISABLED", None)
        else:
            env.update(GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
            env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oStrictHostKeyChecking=yes")
        return env

    def _run(self, executable: str | None, *args: str, timeout: float = 30,
             capture: bool = True) -> Any:
        if not executable:
            raise SetupError("tool_missing", "Git and GitHub CLI must be installed before connecting.")
        try:
            return self.runner([executable, *args], cwd=self.repo, env=self._env(interactive=not capture),
                               timeout=timeout, capture=capture)
        except subprocess.TimeoutExpired:
            raise SetupError("command_timed_out", "The operation timed out. Local commits were retained; retry when the connection is ready.") from None
        except OSError:
            raise SetupError("command_unavailable", "Git or GitHub CLI could not be started. Check its installation and retry.") from None

    def _git(self, *args: str, timeout: float = 30) -> Any:
        return self._run(self.git, *args, timeout=timeout)

    def _config(self, key: str) -> str | None:
        result = self._git("config", "--local", "--get-all", key)
        if result.returncode == 1:
            return None
        values = (result.stdout or "").splitlines()
        if result.returncode or len(values) != 1:
            raise SetupError("git_config_conflict", "Git contains an ambiguous branch or remote setting; review it before connecting.")
        return values[0]

    def _origins(self) -> tuple[str | None, tuple[str, str] | None]:
        names = self._git("remote")
        if names.returncode:
            raise SetupError("git_metadata_failed", "Could not inspect local Git remotes.")
        if "origin" not in (names.stdout or "").splitlines():
            return None, None
        urls = []
        for arguments in (("remote", "get-url", "--all", "origin"),
                          ("remote", "get-url", "--push", "--all", "origin")):
            result = self._git(*arguments)
            values = (result.stdout or "").splitlines()
            if result.returncode or len(values) != 1:
                raise SetupError("origin_conflict", "Origin has multiple or unreadable URLs; review it before connecting.")
            urls.append(values[0])
        identities = [_remote_identity(url) for url in urls]
        if identities[0] is None or identities[0] != identities[1]:
            raise SetupError("unsafe_origin", "Origin must use one matching, credential-free GitHub fetch and push destination.")
        owner, name = identities[0]
        return f"https://github.com/{owner}/{name}.git", identities[0]

    def _git_metadata(self, *, require_commit: bool) -> dict[str, Any]:
        marker = self.repo / ".git"
        if (not self.repo.is_dir() or not marker.is_dir() or marker.is_symlink()
                or getattr(marker, "is_junction", lambda: False)()):
            raise SetupError("invalid_git_root", "Choose the exact primary repository root, with its own .git directory.")
        checks = (("--show-toplevel", self.repo), ("--absolute-git-dir", marker), ("--git-common-dir", marker))
        for option, expected in checks:
            result = self._git("rev-parse", option)
            if result.returncode:
                raise SetupError("invalid_git_root", "The selected folder is not a usable primary Git repository.")
            reported = Path((result.stdout or "").strip())
            if not reported.is_absolute():
                reported = self.repo / reported
            if reported.resolve() != expected.resolve():
                raise SetupError("invalid_git_root", "Parent repositories, bare repositories, and linked worktrees are not supported.")
        bare = self._git("rev-parse", "--is-bare-repository")
        if bare.returncode or (bare.stdout or "").strip() != "false":
            raise SetupError("invalid_git_root", "Connecting requires a non-bare primary repository.")
        branch_result = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
        branch = (branch_result.stdout or "").strip() if branch_result.returncode == 0 else None
        head_result = self._git("rev-parse", "--verify", "HEAD^{commit}")
        head = (head_result.stdout or "").strip() if head_result.returncode == 0 else None
        if head is not None and not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
            raise SetupError("git_metadata_failed", "Git returned an unexpected commit identifier.")
        if require_commit and (not branch or not head):
            raise SetupError("local_commit_required", "Create a local commit on a named branch before connecting to GitHub.")
        origin, identity = self._origins()
        remote = self._config(f"branch.{branch}.remote") if branch else None
        merge = self._config(f"branch.{branch}.merge") if branch else None
        if require_commit and ((remote is not None and remote != "origin")
                               or (merge is not None and merge != f"refs/heads/{branch}")):
            raise SetupError("upstream_conflict", "This branch already tracks a different destination; review it before connecting.")
        return {"valid": True, "branch": branch, "head": head, "origin": origin,
                "origin_identity": identity, "upstream_remote": remote, "upstream_merge": merge}

    def _login(self, *, required: bool) -> str | None:
        if not self.gh:
            if required:
                raise SetupError("github_cli_missing", "Install GitHub CLI before connecting.")
            return None
        result = self._run(self.gh, "api", "user", "--jq", ".login")
        login = (result.stdout or "").strip()
        if result.returncode or not _OWNER.fullmatch(login):
            if required:
                raise SetupError("github_login_required", "GitHub login could not be confirmed. Run this helper's login command, then retry.")
            return None
        return login

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "repository_path": str(self.repo),
                                  "github": {"available": bool(self.gh), "authenticated": False, "login": None}}
        try:
            metadata = self._git_metadata(require_commit=False)
            metadata.pop("origin_identity")
            result["git"] = metadata
        except SetupError as error:
            result.update(ok=False, git={"valid": False, "code": error.code, "message": str(error)})
        try:
            login = self._login(required=False)
            result["github"].update(authenticated=login is not None, login=login)
        except SetupError:
            result["github"]["message"] = "GitHub login could not be confirmed; check login and network availability."
        return _safe(result)

    def login(self) -> dict[str, Any]:
        result = self._run(self.gh, "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web",
                           timeout=900, capture=False)
        if result.returncode:
            raise SetupError("github_login_failed", "GitHub CLI login did not complete. Run login again when ready.")
        return _safe({"ok": True, "login": self._login(required=True), "message": "GitHub CLI login confirmed."})

    def _view(self, repository: str, *, missing_ok: bool) -> dict[str, Any] | None:
        result = self._run(self.gh, "repo", "view", repository, "--json", _FIELDS)
        if result.returncode:
            diagnostic = ((result.stderr or "") + " " + (result.stdout or "")).lower()
            if missing_ok and ("http 404" in diagnostic or "could not resolve to a repository" in diagnostic):
                return None
            raise SetupError("github_repository_unavailable", "Could not verify the GitHub repository. Check login, access, and network availability.")
        try:
            metadata = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise SetupError("github_metadata_invalid", "GitHub CLI returned unexpected repository metadata.") from None
        if not isinstance(metadata, dict) or str(metadata.get("nameWithOwner", "")).lower() != repository.lower():
            raise SetupError("github_repository_mismatch", "GitHub resolved a different repository; no commits were pushed.")
        if metadata.get("isPrivate") is not True or metadata.get("viewerPermission") not in ("ADMIN", "MAINTAIN", "WRITE"):
            raise SetupError("github_repository_permissions", "The repository must be private and writable by the authenticated account.")
        if metadata.get("isArchived") is True or type(metadata.get("isEmpty")) is not bool:
            raise SetupError("github_metadata_invalid", "The repository is archived or its state could not be verified.")
        return metadata

    def connect(self, repository: str) -> dict[str, Any]:
        owner, name = _repository(repository)
        metadata = self._git_metadata(require_commit=True)
        identity = (owner.lower(), name.lower())
        if metadata["origin_identity"] is not None and metadata["origin_identity"] != identity:
            raise SetupError("origin_conflict", "Origin already points to another repository; it was left unchanged.")
        login = self._login(required=True)
        if login.lower() != owner.lower():
            raise SetupError("github_owner_mismatch", "Choose a personal repository owned by the authenticated GitHub account.")
        remote = self._view(repository, missing_ok=metadata["origin"] is None)
        created = False
        if metadata["origin"] is None:
            if remote is not None and not remote["isEmpty"]:
                raise SetupError("github_repository_not_empty", "An existing nonempty repository cannot be adopted automatically. Choose a new repository name.")
            if remote is None:
                creation = self._run(self.gh, "repo", "create", repository, "--private", "--source", str(self.repo),
                                     "--remote", "origin", timeout=60)
                if creation.returncode:
                    raise SetupError("github_create_failed", "The private repository could not be created. Local commits were retained; check GitHub before retrying.")
                created = True
                remote = self._view(repository, missing_ok=False)
            else:
                addition = self._git("remote", "add", "origin", f"https://github.com/{owner}/{name}.git")
                if addition.returncode:
                    raise SetupError("origin_setup_failed", "Could not add origin. Existing repository settings were retained.")
        origin, actual_identity = self._origins()
        if actual_identity != identity:
            raise SetupError("origin_conflict", "The resulting origin did not match the requested repository; no commits were pushed.")
        branch = metadata["branch"]
        if not remote["isEmpty"]:
            refs = self._git("ls-remote", "--heads", "origin", f"refs/heads/{branch}", timeout=90)
            if refs.returncode:
                raise SetupError("remote_verification_failed", "Could not verify the existing remote branch. Local commits were retained; retry when connected.")
            if not any(line.split()[-1:] == [f"refs/heads/{branch}"] for line in (refs.stdout or "").splitlines()):
                raise SetupError("remote_branch_missing", "The nonempty remote has no matching branch; review its history before connecting.")
        for key, value in ((f"branch.{branch}.remote", "origin"), (f"branch.{branch}.merge", f"refs/heads/{branch}")):
            if self._config(key) is None and self._git("config", "--local", key, value).returncode:
                raise SetupError("upstream_setup_failed", "Could not configure this branch's upstream. Local commits were retained.")
        result: dict[str, Any] = {"ok": True, "repository": repository, "url": f"https://github.com/{owner}/{name}",
                                  "branch": branch, "created": created, "origin": origin,
                                  "upstream_configured": True, "pushed": False, "push_pending": False}
        try:
            pushed = self._git("push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}", timeout=90)
        except SetupError:
            return _safe({**result, "ok": False, "code": "push_pending", "push_pending": True, "retryable": True,
                          "message": "Local commits and upstream are saved. GitHub push is pending; retry when the connection is ready."})
        if pushed.returncode:
            diagnostic = ((pushed.stderr or "") + " " + (pushed.stdout or "")).lower()
            rejected = "non-fast-forward" in diagnostic or "fetch first" in diagnostic or "[rejected]" in diagnostic
            return _safe({**result, "ok": False, "code": "push_rejected" if rejected else "push_pending",
                          "push_pending": not rejected, "retryable": not rejected,
                          "message": "Remote history differs; review it before retrying. Local commits were retained." if rejected else
                          "Local commits and upstream are saved. GitHub push is pending; check login and network, then retry."})
        return _safe({**result, "pushed": True, "message": "Private GitHub repository connected; committed history pushed."})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Connect committed source history to a private personal GitHub repository.")
    parser.add_argument("--repo", type=Path, default=PROJECT_ROOT, help="exact primary Git repository root")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "login", "connect"):
        command = commands.add_parser(name)
        command.add_argument("--repo", dest="sub_repo", type=Path, help=argparse.SUPPRESS)
        if name == "connect":
            command.add_argument("--repository", required=True, help="personal owner/name, for example example-user/island")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        setup = GitHubSetup(args.sub_repo or args.repo)
        result = setup.connect(args.repository) if args.command == "connect" else getattr(setup, args.command)()
    except SetupError as error:
        result = {"ok": False, "code": error.code, "message": str(error)}
    print(json.dumps(_safe(result), ensure_ascii=False, allow_nan=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
