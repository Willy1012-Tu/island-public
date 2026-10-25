"""Shared source copies and reviewable, conflict-checked collaboration patches."""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from cli_providers import copy_source_workspace, SOURCE_SUFFIXES, SKIP_DIRS, SECRET_NAME
from desktop_instance import _try_lock, _unlock

_ID = re.compile(r"[a-f0-9]{32}\Z")
_MAX_FILE = 1024 * 1024
_MAX_TOTAL = 20 * _MAX_FILE
_APPLY_LOCK = threading.Lock()


class ArtifactError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _relative(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > 600 or "\\" in value
            or ":" in value or any(not char.isprintable() for char in value)):
        raise ArtifactError("不支援的修改路徑。")
    parts = PurePosixPath(value).parts
    if (not parts or PurePosixPath(value).is_absolute() or any(part in {".", ".."} or part.startswith(".") for part in parts)
            or PurePosixPath(value).as_posix() != value):
        raise ArtifactError("修改路徑必須位於專案內。")
    return value


def _safe_target(root: Path, relative: str) -> Path:
    relative = _relative(relative)
    if _linked(root):
        raise ArtifactError("產出目錄包含連結，無法使用。", 409)
    target = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if _linked(current):
            raise ArtifactError(f"路徑包含連結，無法套用：{relative}", 409)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ArtifactError("修改路徑超出專案。", 409)
    return target


def _inventory(root: Path) -> dict[str, bytes]:
    """Match the bounded source-copy scope, never follow generated links."""
    if _linked(root):
        raise ArtifactError("協作副本根目錄不能是連結。")
    canonical_root = root.resolve()
    result: dict[str, bytes] = {}
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        if _linked(base) or not base.resolve().is_relative_to(canonical_root):
            raise ArtifactError("協作來源超出共用副本範圍。")
        if any(_linked(base / name) for name in directories if not name.startswith(".")
               and name not in SKIP_DIRS and not SECRET_NAME.search(name)):
            raise ArtifactError("協作副本包含目錄連結，請改用一般來源檔案。")
        directories[:] = sorted(name for name in directories if not name.startswith(".")
                                and name not in SKIP_DIRS and not SECRET_NAME.search(name)
                                and not _linked(base / name))
        for name in sorted(files):
            path = base / name
            if name.startswith(".") or SECRET_NAME.search(name) or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if _linked(path):
                raise ArtifactError("協作副本包含檔案連結，無法套用。")
            if not path.resolve().is_relative_to(canonical_root):
                raise ArtifactError("協作檔案超出共用副本範圍。")
            if not path.is_file():
                continue
            relative = _relative(path.relative_to(root).as_posix())
            try:
                with path.open("rb") as stream:
                    content = stream.read(_MAX_FILE + 1)
            except OSError as error:
                raise ArtifactError(f"無法讀取協作檔案：{relative}") from error
            if len(content) > _MAX_FILE or total + len(content) > _MAX_TOTAL or len(result) >= 2000:
                raise ArtifactError("協作輸出超過來源檔案大小限制。")
            try:
                content.decode("utf-8")
            except UnicodeError:
                raise ArtifactError(f"協作修改必須是 UTF-8 文字：{relative}") from None
            result[relative] = content
            total += len(content)
    return result


def _file_patch(relative: str, old: bytes | None, new: bytes | None) -> tuple[str, int, int]:
    old_name = json.dumps(f"a/{relative}", ensure_ascii=False)
    new_name = json.dumps(f"b/{relative}", ensure_ascii=False)
    diff = list(difflib.unified_diff(
        (old or b"").decode("utf-8").splitlines(keepends=True),
        (new or b"").decode("utf-8").splitlines(keepends=True),
        fromfile=old_name if old is not None else "/dev/null",
        tofile=new_name if new is not None else "/dev/null"))
    lines = [f"diff --git {old_name} {new_name}\n"]
    if old is None:
        lines.append("new file mode 100644\n")
    elif new is None:
        lines.append("deleted file mode 100644\n")
    for line in diff:
        lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return ("".join(lines), sum(line.startswith("+") and not line.startswith("+++") for line in diff),
            sum(line.startswith("-") and not line.startswith("---") for line in diff))


class CollaborationWorkspace:
    """One host-owned temporary tree shared by sequential model turns."""
    def __init__(self, source: Path | str):
        self.source = Path(source).resolve()
        self._temporary: tempfile.TemporaryDirectory | None = None
        self.baseline: Path
        self.working: Path

    def __enter__(self) -> "CollaborationWorkspace":
        self._temporary = tempfile.TemporaryDirectory(prefix="island-team-")
        root = Path(self._temporary.name).resolve()
        if root.is_relative_to(self.source) or self.source.is_relative_to(root):
            self._temporary.cleanup()
            raise ArtifactError("協作副本必須與原專案分開。")
        self.baseline, self.working = root / "baseline", root / "working"
        try:
            copy_source_workspace(self.source, self.baseline)
            copy_source_workspace(self.baseline, self.working)
        except BaseException:
            self._temporary.cleanup()
            raise
        return self

    def __exit__(self, *_):
        if self._temporary is not None:
            self._temporary.cleanup()


class ArtifactStore:
    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve()
        self.root = _safe_target(self.workspace, "artifacts/collaborations")

    def _folder(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise ArtifactError("無效的協作產出識別碼。")
        # Check again at use time in case directories changed after construction.
        return _safe_target(self.workspace, f"artifacts/collaborations/{run_id}")

    @staticmethod
    def _public(manifest: dict) -> dict:
        run_id = manifest["id"]
        return {"id": run_id, "changed_files": [
                    {key: item[key] for key in ("path", "status", "additions", "deletions")}
                    for item in manifest["files"]],
                "diff_url": f"/api/monitor/jobs/{run_id}/changes",
                "download_url": f"/api/monitor/jobs/{run_id}/patch",
                "applied": manifest.get("applied") is True}

    @staticmethod
    def _write_manifest(folder: Path, manifest: dict) -> None:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix="manifest-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        try:
            os.replace(temporary, folder / "manifest.json")
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, run_id: str, baseline: Path, working: Path) -> dict:
        before, after = _inventory(baseline), _inventory(working)
        folder = self._folder(run_id)
        if folder.exists():
            raise ArtifactError("此協作產出已存在。", 409)
        folder.mkdir(parents=True)
        files, diffs = [], []
        for relative in sorted(before.keys() | after.keys()):
            old, new = before.get(relative), after.get(relative)
            if old == new:
                continue
            index = len(files)
            patch, additions, deletions = _file_patch(relative, old, new)
            diffs.append(patch)
            files.append({"path": relative, "status": "added" if old is None else "deleted" if new is None else "modified",
                          "additions": additions, "deletions": deletions,
                          "before_hash": _hash(old) if old is not None else None,
                          "after_hash": _hash(new) if new is not None else None,
                          "payload": f"{index}.new" if new is not None else None})
            if new is not None:
                (folder / f"{index}.new").write_bytes(new)
        patch = "".join(diffs)
        (folder / "changes.patch").write_bytes(patch.encode("utf-8"))
        manifest = {"version": 1, "id": run_id, "files": files, "applied": False,
                    "patch_hash": _hash(patch.encode("utf-8"))}
        self._write_manifest(folder, manifest)
        return self._public(manifest)

    def _load(self, run_id: str) -> tuple[Path, dict, str]:
        folder = self._folder(run_id)
        try:
            manifest_file = _safe_target(folder, "manifest.json")
            with manifest_file.open("rb") as stream:
                raw = stream.read(2 * _MAX_FILE + 1)
            if len(raw) > 2 * _MAX_FILE:
                raise ValueError()
            manifest = json.loads(raw)
            if (not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("id") != run_id
                    or not isinstance(manifest.get("files"), list) or len(manifest["files"]) > 2000):
                raise ValueError()
            seen = set()
            for index, record in enumerate(manifest["files"]):
                path = _relative(record["path"])
                if path in seen or record["status"] not in {"added", "modified", "deleted"}:
                    raise ValueError()
                seen.add(path)
                if any(type(record.get(key)) is not int or record[key] < 0 for key in ("additions", "deletions")):
                    raise ValueError()
                if record.get("payload") != (f"{index}.new" if record["status"] != "deleted" else None):
                    raise ValueError()
                for key, missing in (("before_hash", record["status"] == "added"),
                                     ("after_hash", record["status"] == "deleted")):
                    digest = record.get(key)
                    if (missing and digest is not None) or (not missing and (
                            not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest))):
                        raise ValueError()
            patch_path = _safe_target(folder, "changes.patch")
            with patch_path.open("rb") as stream:
                patch = stream.read(2 * _MAX_TOTAL + 1)
            if len(patch) > 2 * _MAX_TOTAL or _hash(patch) != manifest.get("patch_hash"):
                raise ValueError()
            return folder, manifest, patch.decode("utf-8")
        except FileNotFoundError:
            raise ArtifactError("找不到協作修改；請重新執行協作任務。", 404) from None
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            raise ArtifactError("協作產出資料已變更或損壞。", 409) from None
        except OSError:
            raise ArtifactError("無法讀取協作產出檔案。", 409) from None

    def review(self, run_id: str) -> dict:
        _, manifest, patch = self._load(run_id)
        return {"artifact": self._public(manifest), "diff": patch}

    def patch(self, run_id: str) -> str:
        return self._load(run_id)[2]

    @contextlib.contextmanager
    def _workspace_lock(self):
        """Desktop and browser servers must not commit the same source together."""
        lock_path = _safe_target(self.root, "apply.lock")
        try:
            with lock_path.open("a+b") as stream:
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                if not _try_lock(stream):
                    raise ArtifactError("另一個辦公室正在套用此專案，請稍後重試。", 409)
                try:
                    yield
                finally:
                    _unlock(stream)
        except OSError:
            raise ArtifactError("無法取得專案套用鎖，請確認產出目錄可寫入。", 409) from None

    def apply(self, run_id: str) -> dict:
        """Preflight every baseline and payload before touching original files."""
        with _APPLY_LOCK, self._workspace_lock():
            folder, manifest, reviewed_patch = self._load(run_id)
            if manifest.get("applied") is True:
                return self._public(manifest)
            changes, expected_patch = [], []
            for record in manifest["files"]:
                target = _safe_target(self.workspace, record["path"])
                if target.exists() and not target.is_file():
                    raise ArtifactError(f"目標不是檔案：{record['path']}", 409)
                if target.is_file() and target.stat().st_size > _MAX_FILE:
                    raise ArtifactError(f"原專案檔案已變更或超過限制：{record['path']}", 409)
                old = target.read_bytes() if target.is_file() else None
                if (_hash(old) if old is not None else None) != record.get("before_hash"):
                    raise ArtifactError(f"原專案已修改，請重新產生差異：{record['path']}", 409)
                new = None
                if record["payload"]:
                    payload = _safe_target(folder, record["payload"])
                    with payload.open("rb") as stream:
                        new = stream.read(_MAX_FILE + 1)
                    if len(new) > _MAX_FILE or _hash(new) != record.get("after_hash"):
                        raise ArtifactError("協作修改內容已變更，無法套用。", 409)
                changes.append((target, old, new))
                expected_patch.append(_file_patch(record["path"], old, new)[0])
            if "".join(expected_patch) != reviewed_patch:
                raise ArtifactError("檢視的差異與實際修改內容不一致，請重新產生協作產出。", 409)
            committed, staged = [], []
            new_contents = {target: new for target, _, new in changes}
            try:
                for target, old, new in changes:
                    if new is None:
                        staged.append((target, old, None))
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".agentdesk-", delete=False) as stream:
                        stream.write(new)
                        temporary = Path(stream.name)
                    staged.append((target, old, temporary))
                    if old is not None:
                        os.chmod(temporary, target.stat().st_mode & 0o777)
                for target, old, temporary in staged:
                    _safe_target(self.workspace, target.relative_to(self.workspace).as_posix())
                    current = target.read_bytes() if target.is_file() else None
                    if current != old:
                        raise ArtifactError("套用期間檔案已變更，已取消。", 409)
                    if temporary is None:
                        target.unlink()
                    else:
                        os.replace(temporary, target)
                    committed.append((target, old, new_contents[target]))
                manifest["applied"] = True
                self._write_manifest(folder, manifest)
            except (OSError, ArtifactError) as error:
                rollback_failed = False
                for target, old, new in reversed(committed):
                    try:
                        _safe_target(self.workspace, target.relative_to(self.workspace).as_posix())
                        if target.is_file():
                            with target.open("rb") as stream:
                                current = stream.read(_MAX_FILE + 1)
                        else:
                            current = None if not target.exists() else object()
                        if current != new:
                            rollback_failed = True
                            continue  # Never overwrite another writer's newer content.
                        if old is None:
                            target.unlink(missing_ok=True)
                        else:
                            target.write_bytes(old)
                    except (OSError, ArtifactError):
                        rollback_failed = True
                if rollback_failed:
                    raise ArtifactError("套用中斷且回復未完成，請檢查原專案檔案。", 500) from error
                if isinstance(error, ArtifactError):
                    raise
                raise ArtifactError("套用失敗，已回復原檔案。", 409) from error
            finally:
                for _, _, temporary in staged:
                    if temporary is not None:
                        with contextlib.suppress(OSError):
                            temporary.unlink(missing_ok=True)
            return self._public(manifest)
