"""Conservative, per-repository Git snapshots without changing user staging.

Only an explicitly initialized exact repository is watched.  A private index
builds each snapshot; the conventional index lock protects the ref/index handoff.
No checkout, pull, reset, clean, hooks, or history rewriting is performed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

DEFAULT_CONFIG = {"enabled": False, "push": False, "poll_seconds": 2, "debounce_seconds": 8}
CONFIG_NAME = "island-autosave.json"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 2000
_PRIVATE_DIRS = {".git", ".ssh", ".aws", ".codex", ".agents", ".config", ".tools",
                 ".venv", ".venv-desktop", "venv", "node_modules", "__pycache__",
                 ".pytest_cache", ".mypy_cache", "artifacts", "dist", "build", "target",
                 "tmp", "temp", "logs", "state", "coverage", "vendor"}
SOURCE_SUFFIXES = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html",
                   ".css", ".scss", ".sass", ".less", ".vue", ".svelte", ".json", ".toml",
                   ".yaml", ".yml", ".ini", ".cfg", ".md", ".rst", ".txt", ".xml", ".sql",
                   ".ps1", ".sh", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".cs",
                   ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".svg"}
ASSET_SUFFIXES = {".ico", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
_PRIVATE_NAME = re.compile(r"^(?:\.env(?:\..*)?|office-bridge\.(?:json|lock)|id_(?:rsa|ed25519|ecdsa)(?:\.pub)?|"
                           r"(?:auth|credentials?|secrets?|tokens?|passwords?)(?:[._-].*)?)$", re.I)
_SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    rb"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{30,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b|"
    rb"(?i:(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[=:]\s*[\"']"
    rb"[^\"'\r\n]{12,}[\"'])")
_FIXTURE = re.compile(rb"(?i:synthetic|placeholder|example|dummy|fake|test[-_]?key|YOUR[_ -]|not[-_]a[-_]real|REDACTED)")


class AutosaveError(RuntimeError):
    """A safe, non-sensitive operational error suitable for the status log."""


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _json_object(text: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=pairs,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def load_config(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        if _linked(path) or path.stat().st_size > 16384:
            raise ValueError("unsafe config")
        value = _json_object(path.read_text(encoding="utf-8"))
        if set(value) - set(DEFAULT_CONFIG):
            raise ValueError("unknown config field")
        result = DEFAULT_CONFIG | value
        if any(type(result[name]) is not bool for name in ("enabled", "push")):
            raise ValueError("boolean required")
        for name, minimum, maximum in (("poll_seconds", .2, 300), ("debounce_seconds", 0, 3600)):
            number = result[name]
            if type(number) not in (int, float) or not math.isfinite(number) or not minimum <= number <= maximum:
                raise ValueError("invalid interval")
        return result
    except (OSError, UnicodeError, ValueError) as error:
        raise AutosaveError("Invalid autosave configuration; no Git changes were made.") from error


def contains_secret(content: bytes) -> bool:
    """Recognize common credentials, allowing explicitly labelled test fixtures."""
    for match in _SECRET.finditer(content):
        if not _FIXTURE.search(match.group()):
            return True
    return False


def trusted_github_url(value: str) -> bool:
    """Accept only ordinary, credential-free GitHub repository destinations."""
    if value.startswith("git@github.com:"):
        path = value[len("git@github.com:"):]
    else:
        try:
            parsed = urlsplit(value)
            if parsed.hostname != "github.com" or parsed.query or parsed.fragment or parsed.port is not None:
                return False
            if parsed.scheme == "https" and parsed.username is None and parsed.password is None:
                path = parsed.path.lstrip("/")
            elif parsed.scheme == "ssh" and parsed.username == "git" and parsed.password is None:
                path = parsed.path.lstrip("/")
            else:
                return False
        except ValueError:
            return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", path)
                and all(part not in {".", ".."} for part in path.split("/")))


class GitAutosave:
    def __init__(self, repo: Path | str, *, clock: Any = time.monotonic) -> None:
        raw = Path(repo).expanduser().absolute()
        if _linked(raw):
            raise AutosaveError("The repository root cannot be a link or junction.")
        self.repo = raw.resolve()
        self.git_dir = self.repo / ".git"
        if not self.git_dir.is_dir() or _linked(self.git_dir):
            raise AutosaveError("Choose the exact standalone Git repository root, not a parent or worktree.")
        self._clock = clock
        self._fingerprint: str | None = None
        self._changed_at = 0.0
        self._next_push = 0.0
        self._push_failures = 0
        self._watch_lock_held = False
        root = Path(self._git(["rev-parse", "--show-toplevel"]).decode().strip()).resolve()
        git_dir = Path(self._git(["rev-parse", "--absolute-git-dir"]).decode().strip()).resolve()
        if root != self.repo or git_dir != self.git_dir.resolve():
            raise AutosaveError("Git discovery did not match the exact requested repository.")
        if self._git(["rev-parse", "--is-bare-repository"]).strip() != b"false":
            raise AutosaveError("Bare repositories are not supported.")

    @property
    def config_path(self) -> Path:
        return self.git_dir / CONFIG_NAME

    def _run(self, args: list[str], *, data: bytes | None = None,
             index: Path | None = None, timeout: float = 12,
             policy_query: bool = False) -> subprocess.CompletedProcess[bytes]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_OPTIONAL_LOCKS="0",
                   GIT_LITERAL_PATHSPECS="1", GIT_SSH_COMMAND="ssh -oBatchMode=yes")
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        if args[0] == "check-ignore":
            # check-ignore accepts literal stdin paths but rejects pathspec magic.
            env.pop("GIT_LITERAL_PATHSPECS")
        try:
            config = [] if policy_query else ["-c", "core.hooksPath=" + str(self.git_dir / "island-no-hooks"),
                                              "-c", "commit.gpgSign=false"]
            return subprocess.run(["git", *config, "-C", str(self.repo), *args],
                                  input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  shell=False, env=env, timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AutosaveError("Git could not finish the requested operation within its time limit.") from error

    def _git(self, args: list[str], **kwargs: Any) -> bytes:
        result = self._run(args, **kwargs)
        if result.returncode:
            raise AutosaveError("Git refused the requested operation; repository and local commits were preserved.")
        return result.stdout

    def init_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            try:
                with self.config_path.open("x", encoding="utf-8") as stream:
                    json.dump(DEFAULT_CONFIG, stream, indent=2)
                    stream.write("\n")
            except FileExistsError:
                pass
        return load_config(self.config_path) or dict(DEFAULT_CONFIG)

    def _state(self, status: str, **extra: Any) -> dict[str, Any]:
        return {"status": status, "repo": str(self.repo), **extra}

    def status(self) -> dict[str, Any]:
        config = load_config(self.config_path)
        branch = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"])
        previous = None
        path = self.git_dir / "island-autosave-status.json"
        if path.is_file() and not _linked(path) and path.stat().st_size <= 256 * 1024:
            try:
                previous = _json_object(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                pass
        return self._state("ready" if config and config["enabled"] else "disabled",
                           configured=config is not None, config=config,
                           branch=branch.stdout.decode("utf-8", "replace").strip() if not branch.returncode else None,
                           index_locked=(self.git_dir / "index.lock").exists(), last_status=previous)

    @contextlib.contextmanager
    def _process_lock(self) -> Iterator[bool]:
        path = self.git_dir / "island-autosave.lock"
        if _linked(path):
            raise AutosaveError("The watcher lock cannot be a link.")
        with path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            locked = False
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                pass
            try:
                yield locked
            finally:
                if locked:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _busy(self) -> str | None:
        if any((self.git_dir / name).exists() for name in
               ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")):
            return "A merge, rebase, or sequenced Git operation is in progress."
        if (self.git_dir / "index.lock").exists():
            return "Git's index is locked; the existing lock was left untouched."
        hooks = self._run(["config", "--get", "core.hooksPath"], policy_query=True)
        if hooks.returncode == 0:
            return "A custom Git hooks policy is configured; automatic commits are paused."
        if hooks.returncode != 1:
            return "Git hook policy could not be checked safely."
        signed = self._run(["config", "--type=bool", "--get", "commit.gpgSign"], policy_query=True)
        if signed.returncode not in (0, 1):
            return "Git signing policy could not be checked safely."
        if signed.returncode == 0 and signed.stdout.strip() == b"true":
            return "Signed commits are required by Git configuration; automatic commits are paused."
        hook_dir = self.git_dir / "hooks"
        if _linked(hook_dir) or hook_dir.exists() and any(
                path.is_file() and not path.name.endswith(".sample") for path in hook_dir.iterdir()):
            return "An active Git hook exists; automatic commits are paused."
        return None

    def _path(self, name: str) -> Path:
        pure = PurePosixPath(name)
        if (not name or pure.is_absolute() or "\\" in name or ":" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or any(part in {".", ".."} for part in pure.parts)):
            raise AutosaveError("Unsafe candidate path.")
        if contains_secret(name.encode("utf-8")):
            raise AutosaveError("Possible credential in candidate filename.")
        lowered = [part.casefold() for part in pure.parts]
        if (any(part in _PRIVATE_DIRS or part.startswith(".") for part in lowered[:-1])
                or _PRIVATE_NAME.fullmatch(pure.name)
                or (pure.name.startswith(".") and pure.name not in {".gitignore", ".gitattributes"})
                or pure.suffix.casefold() in {".pem", ".key", ".p12", ".pfx", ".kdbx", ".log", ".sqlite", ".db"}):
            raise AutosaveError("Private or generated candidate path.")
        current = self.repo
        for index, part in enumerate(pure.parts):
            current = current / part
            if _linked(current):
                raise AutosaveError("Linked candidate paths are not backed up.")
            if index < len(pure.parts) - 1 and (current / ".git").exists():
                raise AutosaveError("Nested repositories are not backed up.")
        if not current.resolve().is_relative_to(self.repo):
            raise AutosaveError("Candidate escaped the repository.")
        if current.exists() and not current.is_file():
            raise AutosaveError("Candidate is not a regular file.")
        return current

    @staticmethod
    def _approved_new(name: str) -> bool:
        parts = PurePosixPath(name).parts
        lower = [part.casefold() for part in parts]
        return (PurePosixPath(name).suffix.casefold() in SOURCE_SUFFIXES | ASSET_SUFFIXES
                or len(parts) == 1 and lower[0] in {".gitignore", ".gitattributes", "dockerfile", "makefile"})

    def _candidates(self) -> tuple[list[str], list[dict[str, str]], str]:
        changed = set(self._git(["diff", "--name-only", "-z", "--no-renames", "HEAD", "--"]).decode("utf-8").split("\0")) - {""}
        new = set(self._git(["ls-files", "--others", "--exclude-standard", "-z"]).decode("utf-8").split("\0")) - {""}
        names = sorted(changed | new)
        if len(names) > MAX_FILES:
            raise AutosaveError("Too many changed files for an automatic snapshot.")
        ignored: set[str] = set()
        if names:
            result = self._run(["check-ignore", "--no-index", "-z", "--stdin"],
                               data=("\0".join(names) + "\0").encode())
            if result.returncode not in (0, 1):
                raise AutosaveError("Git could not verify ignore rules.")
            ignored = set(result.stdout.decode("utf-8").split("\0"))
        safe, skipped, digest, total = [], [], hashlib.sha256(), 0
        for name in names:
            try:
                if name in ignored:
                    raise AutosaveError("Ignored by Git.")
                if name in new and not self._approved_new(name):
                    raise AutosaveError("New file is outside the source allowlist.")
                path = self._path(name)
                if path.exists() and path.stat().st_size > MAX_FILE_BYTES:
                    raise AutosaveError("Automatic snapshot size limit exceeded.")
                content = path.read_bytes() if path.exists() else None
                if content is not None:
                    total += len(content)
                    if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                        raise AutosaveError("Automatic snapshot size limit exceeded.")
                    if contains_secret(content):
                        raise AutosaveError("Possible credential detected; file was not staged.")
                digest.update(name.encode() + b"\0" + (b"D" if content is None else b"F" + content))
                safe.append(name)
            except AutosaveError as error:
                # Paths are repository filenames, never matched credential content.
                public_name = _SECRET.sub(b"[redacted]", name.encode()).decode("utf-8", "replace")
                skipped.append({"path": public_name, "reason": str(error)})
        return safe, skipped, digest.hexdigest()

    def _validate_index(self, index: Path, paths: list[str]) -> None:
        staged = set(self._git(["diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--"], index=index).decode().split("\0")) - {""}
        if not staged.issubset(set(paths)):
            raise AutosaveError("Private index contained unexpected paths.")
        entries = self._git(["ls-files", "--stage", "-z", "--", *paths], index=index)
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            mode, oid, stage = metadata.split()
            if mode not in {b"100644", b"100755"} or stage != b"0":
                raise AutosaveError("Linked or conflicted staged objects are not backed up.")
            self._path(raw_name.decode("utf-8"))
            size = int(self._git(["cat-file", "-s", oid.decode()]).strip())
            if size > MAX_FILE_BYTES or contains_secret(self._git(["cat-file", "blob", oid.decode()])):
                raise AutosaveError("Staged snapshot failed its credential or size validation.")

    def _commit(self, paths: list[str]) -> str | None:
        lock = self.git_dir / "index.lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise AutosaveError("Git's index became locked; the existing lock was left untouched.")
        update_attempted = False
        new: str | None = None
        branch: str | None = None
        preserve_lock = False
        try:
            staged = self._run(["diff", "--cached", "--quiet", "HEAD", "--"])
            if staged.returncode == 1:
                raise AutosaveError("User-staged changes exist; they were left untouched.")
            if staged.returncode:
                raise AutosaveError("Git could not verify the existing index.")
            branch = self._git(["symbolic-ref", "--quiet", "HEAD"]).decode().strip()
            old = self._git(["rev-parse", "HEAD"]).decode().strip()
            with tempfile.TemporaryDirectory(prefix="island-index-", dir=self.git_dir) as temporary:
                index = Path(temporary) / "index"
                self._git(["read-tree", old], index=index)
                self._git(["add", "-A", "--", *paths], index=index)
                self._validate_index(index, paths)
                tree = self._git(["write-tree"], index=index).strip()
                if tree == self._git(["rev-parse", old + "^{tree}"]).strip():
                    return None
                message = time.strftime("Autosave: %Y-%m-%d %H:%M:%S") + "\n"
                new = self._git(["commit-tree", tree.decode(), "-p", old], data=message.encode()).decode().strip()
                with os.fdopen(fd, "wb") as stream:
                    fd = -1
                    stream.write(index.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                if self._git(["symbolic-ref", "--quiet", "HEAD"]).decode().strip() != branch:
                    raise AutosaveError("The checked-out branch changed during the snapshot.")
                # CAS refuses a concurrent ref update.  The normal index remains unchanged until success.
                update_attempted = True
                result = self._run(["update-ref", "-m", "Island automatic source snapshot", branch, new, old])
                if result.returncode:
                    raise AutosaveError("HEAD changed during the snapshot; no existing ref or index was overwritten.")
                try:
                    os.replace(lock, self.git_dir / "index")
                except OSError as error:
                    preserve_lock = True
                    raise AutosaveError("Commit saved, but index handoff failed. Prepared index.lock was retained for recovery.") from error
                return new
        finally:
            if fd >= 0:
                os.close(fd)
            if update_attempted and lock.exists() and not preserve_lock:
                # Also cover interruption immediately after Git moved the ref but
                # before Python received the return code. Never discard recovery data.
                try:
                    current = self._git(["rev-parse", str(branch)]).decode().strip()
                    preserve_lock = current == new
                except BaseException:
                    preserve_lock = True
            if not preserve_lock and lock.exists():
                # This name was exclusively created above; a foreign preexisting lock is never removed.
                lock.unlink()

    def _push(self, branch: str, *, force: bool) -> dict[str, Any]:
        if not force and self._clock() < self._next_push:
            return {"push": "retry_wait"}
        remote = self._run(["remote", "get-url", "--push", "--all", "origin"])
        if remote.returncode:
            return {"push": "blocked", "push_reason": "No origin remote is configured; local commits are preserved."}
        urls = remote.stdout.decode().splitlines()
        if len(urls) != 1 or not trusted_github_url(urls[0]):
            return {"push": "blocked", "push_reason": "Origin must be a single credential-free GitHub repository URL."}
        info = self._git(["for-each-ref", "--format=%(upstream:remotename)%00%(upstream:remoteref)%00%(upstream)", "refs/heads/" + branch]).decode().strip().split("\0")
        if len(info) != 3 or info[:2] != ["origin", "refs/heads/" + branch] or not info[2]:
            return {"push": "blocked", "push_reason": "The current branch needs a matching origin upstream."}
        ahead_result = self._run(["rev-list", "--count", info[2] + "..HEAD"])
        # A configured upstream may have no local tracking ref yet (e.g. a first
        # offline push). A normal push remains safe and must still be retried.
        if not ahead_result.returncode and not int(ahead_result.stdout.strip()):
            return {"push": "up_to_date"}
        try:
            result = self._run(["push", "--porcelain", "origin", "refs/heads/" + branch + ":refs/heads/" + branch], timeout=30)
            succeeded = result.returncode == 0
        except AutosaveError:
            succeeded = False
        if succeeded:
            self._push_failures = 0
            self._next_push = 0
            return {"push": "pushed"}
        self._push_failures += 1
        self._next_push = self._clock() + min(60, 5 * 2 ** min(self._push_failures - 1, 4))
        return {"push": "failed", "push_pending": True,
                "push_reason": "Push failed or timed out. Local commits are preserved and will be retried."}

    def _tick(self, *, force: bool = False) -> dict[str, Any]:
        try:
            config = load_config(self.config_path)
            if not config or not config["enabled"]:
                return self._state("disabled", configured=config is not None)
            with (contextlib.nullcontext(True) if self._watch_lock_held else self._process_lock()) as acquired:
                if not acquired:
                    return self._state("busy", reason="Another autosave process is active.")
                busy = self._busy()
                if busy:
                    return self._state("busy", reason=busy)
                branch_result = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"])
                if branch_result.returncode or self._run(["rev-parse", "--verify", "HEAD"]).returncode:
                    return self._state("blocked", reason="An initial commit and checked-out branch are required.")
                branch = branch_result.stdout.decode().strip()
                if self._run(["diff", "--cached", "--quiet", "HEAD", "--"]).returncode:
                    return self._state("blocked", reason="User-staged changes exist; they were left untouched.")
                paths, skipped, fingerprint = self._candidates()
                now = self._clock()
                if fingerprint != self._fingerprint:
                    self._fingerprint, self._changed_at = fingerprint, now
                if paths and not force and now - self._changed_at < config["debounce_seconds"]:
                    return self._state("debouncing", files=paths, skipped_files=skipped)
                commit = self._commit(paths) if paths else None
                result = self._state("committed" if commit else "blocked" if skipped else "clean",
                                     branch=branch, commit=commit, files=paths if commit else [], skipped_files=skipped)
                if config["push"]:
                    try:
                        result.update(self._push(branch, force=force))
                    except AutosaveError:
                        result.update(push="failed", push_pending=True,
                                      push_reason="Push checks could not finish. Local commits are preserved.")
                else:
                    result["push"] = "disabled"
                return result
        except (AutosaveError, UnicodeError, ValueError, OSError):
            # Never echo subprocess stderr, URLs, file contents, credentials, or arbitrary exception text.
            error = sys.exception()
            return self._state("error", reason=str(error) if isinstance(error, AutosaveError) else "Snapshot could not be completed safely; existing files and commits were preserved.")

    def tick(self, *, force: bool = False) -> dict[str, Any]:
        result = self._tick(force=force)
        # A background pythonw process has no console. Persist only these safe
        # public fields, never subprocess output, remote URLs, or file contents.
        path = self.git_dir / "island-autosave-status.json"
        temporary: Path | None = None
        try:
            if _linked(path):
                return result
            old: dict[str, Any] = {}
            if path.is_file() and path.stat().st_size <= 256 * 1024:
                try:
                    old = _json_object(path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeError):
                    pass
            current = {**result, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            for key in ("files", "skipped_files"):
                if isinstance(current.get(key), list):
                    values = current[key]
                    current[key + "_total"] = len(values)
                    current[key] = [({"path": str(value.get("path", ""))[:240],
                                      "reason": str(value.get("reason", ""))[:160]}
                                     if isinstance(value, dict) else str(value)[:240])
                                    for value in values[:32]]
            for key in ("last_commit", "last_commit_at", "last_push_at"):
                if key in old:
                    current[key] = old[key]
            if result.get("commit"):
                current.update(last_commit=result["commit"], last_commit_at=current["checked_at"])
            if result.get("push") == "pushed":
                current["last_push_at"] = current["checked_at"]
            fd, name = tempfile.mkstemp(prefix="island-status-", suffix=".json", dir=self.git_dir)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(current, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(temporary, path)
        except OSError:
            pass  # A diagnostic write must never turn a successful commit into a failure.
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return result

    def watch(self) -> int:
        """Hold the OS lock for the complete watcher lifetime, including sleeps."""
        with self._process_lock() as acquired:
            if not acquired:
                print(json.dumps(self._state("busy", reason="Another autosave process is active."), ensure_ascii=False), flush=True)
                return 0
            self._watch_lock_held = True
            try:
                previous = None
                while True:
                    result = self.tick()
                    encoded = json.dumps(result, ensure_ascii=False)
                    if encoded != previous:
                        print(encoded, flush=True)
                        previous = encoded
                    try:
                        config = load_config(self.config_path)
                    except AutosaveError:
                        config = None
                    time.sleep(config["poll_seconds"] if config else DEFAULT_CONFIG["poll_seconds"])
            finally:
                self._watch_lock_held = False


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    action = parser.add_mutually_exclusive_group(required=True)
    for name in ("once", "watch", "status", "init-config"):
        action.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)
    try:
        watcher = GitAutosave(args.repo)
        if args.init_config:
            print(json.dumps({"status": "configured", "config": watcher.init_config()}, ensure_ascii=False))
            return 0
        if args.status:
            print(json.dumps(watcher.status(), ensure_ascii=False))
            return 0
        if args.once:
            result = watcher.tick(force=True)
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result["status"] == "error" else 0
        return watcher.watch()
    except KeyboardInterrupt:
        return 0
    except AutosaveError as error:
        print(json.dumps({"status": "error", "reason": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
