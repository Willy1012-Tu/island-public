"""Apply strictly validated file proposals only to a host-owned working copy.

The caller must supply its external temporary collaboration copy, never the
original project. This module executes no commands. The original project is
changed only by the separate reviewed-artifact apply operation.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from cli_providers import SECRET_NAME, SKIP_DIRS, SOURCE_SUFFIXES
from collaboration_workspace import ArtifactError, _inventory, _linked, _relative, _safe_target

MAX_OUTPUT_BYTES = 32 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 20 * MAX_FILE_BYTES
MAX_FILES = 2000
MAX_CHANGES = 12
_LOCK = threading.Lock()

PROPOSAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"},
                           "operation": {"type": "string", "enum": ["create", "replace", "delete"]},
                           "content": {"type": "string"}},
            "required": ["path", "operation", "content"]}},
    },
    "required": ["summary", "changes"],
}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite JSON value.")


def _parse(output: str) -> dict:
    try:
        if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError()
        data = json.loads(output, object_pairs_hook=_unique, parse_constant=_constant)
        if not isinstance(data, dict) or set(data) != {"summary", "changes"}:
            raise ValueError()
        if (not isinstance(data["summary"], str) or not 1 <= len(data["summary"].strip()) <= 8000
                or not isinstance(data["changes"], list) or not 1 <= len(data["changes"]) <= MAX_CHANGES):
            raise ValueError()
        return data
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ArtifactError("程式修改提案必須是完整且不超過 32 KiB 的有效 JSON，包含摘要及 1 至 12 項修改。") from None


def _scope(relative: Any) -> str:
    relative = _relative(relative)
    parts = PurePosixPath(relative).parts
    if (Path(relative).suffix.lower() not in SOURCE_SUFFIXES
            or any(part.casefold() in SKIP_DIRS or SECRET_NAME.search(part)
                   or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved() for part in parts)):
        raise ArtifactError("提案僅能修改允許的來源檔案，不能包含隱藏、機密、產出或相依套件路徑。")
    return relative


def _read_file(target: Path) -> tuple[bytes | None, int | None]:
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(metadata.st_mode) or _linked(target):
        raise ArtifactError("修改目標必須是一般檔案，不能是目錄或連結。", 409)
    with target.open("rb") as stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ArtifactError("修改目標超過單一來源檔案大小限制。")
    try:
        content.decode("utf-8")
    except UnicodeError:
        raise ArtifactError("修改目標必須是 UTF-8 文字檔案。") from None
    return content, metadata.st_mode & 0o777


def _parent_directories(root: Path, target: Path) -> None:
    for parent in target.parents:
        if parent == root:
            break
        if parent.exists() and not parent.is_dir():
            raise ArtifactError("提案的父路徑不是目錄。", 409)


def _restore(target: Path, old: bytes, mode: int | None) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".agentdesk-rollback-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(old)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def apply_proposals(workspace: Path, output: str) -> dict:
    """Preflight a complete proposal, then change the supplied owned copy only.

    Native CLI editing permissions are neither used nor changed. A matching
    existing create is an idempotent no-op; replace/delete require existing text.
    Any invalid change prevents every write. Commit failures roll back our writes.
    """
    data = _parse(output)
    try:
        root = Path(workspace).expanduser().absolute()
        if _linked(root) or not root.is_dir():
            raise ArtifactError("協作工作副本必須是既有的一般目錄，不能是連結。")
        root = root.resolve()
        with _LOCK:
            before = _inventory(root)
            final = dict(before)
            lookup = {name.casefold(): name for name in before}
            if len(lookup) != len(before):
                raise ArtifactError("副本包含大小寫衝突的來源路徑。")
            planned, seen = [], set()
            for item in data["changes"]:
                if not isinstance(item, dict) or set(item) != {"path", "operation", "content"}:
                    raise ArtifactError("每項修改必須且只能包含 path、operation、content。")
                relative = _scope(item["path"])
                relative = lookup.get(relative.casefold(), relative)
                if relative.casefold() in seen:
                    raise ArtifactError("同一份提案不能重複修改相同來源路徑。")
                seen.add(relative.casefold())
                operation, content = item["operation"], item["content"]
                if operation not in ("create", "replace", "delete") or not isinstance(content, str):
                    raise ArtifactError("不支援的修改動作或文字內容。")
                if operation == "delete" and content != "":
                    raise ArtifactError("刪除檔案的 content 必須為空字串。")
                new = None if operation == "delete" else content.encode("utf-8")
                if new is not None and len(new) > MAX_FILE_BYTES:
                    raise ArtifactError("修改內容超過單一來源檔案大小限制。")
                target = _safe_target(root, relative)
                _parent_directories(root, target)
                old, mode = _read_file(target)
                if operation == "create" and old is not None and old != new:
                    raise ArtifactError(f"新增目標已存在，請明確使用 replace：{relative}", 409)
                if operation in ("replace", "delete") and old is None:
                    raise ArtifactError(f"修改目標不存在：{relative}", 409)
                if old != before.get(relative):
                    raise ArtifactError("副本內容在提案檢查期間發生變更。", 409)
                if new is None:
                    final.pop(relative, None)
                else:
                    final[relative] = new
                if old != new:
                    planned.append((relative, target, old, new, mode))
            if len(final) > MAX_FILES or sum(len(content) for content in final.values()) > MAX_TOTAL_BYTES:
                raise ArtifactError("提案完成後會超過副本來源檔案數量或總大小限制。")

            staged, committed = [], []
            try:
                for relative, target, old, new, mode in planned:
                    if new is None:
                        staged.append((relative, target, old, new, mode, None))
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".agentdesk-proposal-", delete=False) as stream:
                        temporary = Path(stream.name)
                        staged.append((relative, target, old, new, mode, temporary))
                        stream.write(new)
                    if mode is not None:
                        os.chmod(temporary, mode)
                for relative, target, old, new, mode, temporary in staged:
                    _safe_target(root, relative)
                    current, _ = _read_file(target)
                    if current != old:
                        raise ArtifactError("套用提案期間副本內容發生變更。", 409)
                    if temporary is None:
                        target.unlink()
                    else:
                        os.replace(temporary, target)
                    committed.append((relative, target, old, new, mode))
            except (OSError, ArtifactError) as error:
                failed = False
                for relative, target, old, new, mode in reversed(committed):
                    try:
                        _safe_target(root, relative)
                        current, _ = _read_file(target)
                        if current != new:
                            failed = True  # Preserve another writer's edits.
                        elif old is None:
                            target.unlink(missing_ok=True)
                        else:
                            _restore(target, old, mode)
                    except (OSError, ArtifactError):
                        failed = True
                if failed:
                    raise ArtifactError("提案套用中斷且回復未完成，請檢查工作副本。", 500) from error
                if isinstance(error, ArtifactError):
                    raise
                raise ArtifactError("提案套用失敗，已回復工作副本原檔案。", 409) from error
            finally:
                for *_, temporary in staged:
                    if temporary is not None:
                        with contextlib.suppress(OSError):
                            temporary.unlink(missing_ok=True)
            return {"summary": data["summary"].strip(), "changed_files": [item[0] for item in planned]}
    except ArtifactError:
        raise
    except (OSError, ValueError, UnicodeError, RuntimeError) as error:
        raise ArtifactError("無法安全套用程式修改提案至工作副本。", 409) from error
