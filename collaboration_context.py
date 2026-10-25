"""Read-only host source context and strict public analysis reports.

Only an already authorized, host-owned isolated source copy is accepted by the
caller contract. No command, model, or native filesystem tool is executed here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from cli_providers import SECRET_NAME, SKIP_DIRS, redact
from collaboration_workspace import ArtifactError, _inventory, _linked, _safe_target

MAX_OUTPUT_BYTES = 32 * 1024
MAX_CONTEXT_CHARS = 12000
MAX_MANIFEST_PATHS = 100
MAX_CONTENT_FILES = 8

READ_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "complete": {"type": "boolean"},
    },
    "required": ["summary", "findings", "limitations", "complete"],
}


def _unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate report key.")
        data[key] = value
    return data


def _constant(value):
    raise ValueError("Non-finite JSON value.")


def _report_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError("Report text is missing or oversized.")
    value.encode("utf-8")
    return value.strip()


def parse_report(output: str) -> dict:
    """Validate the whole analysis response; intent-only CLI success is insufficient."""
    try:
        if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError()
        data = json.loads(output, object_pairs_hook=_unique, parse_constant=_constant)
        if not isinstance(data, dict) or set(data) != {"summary", "findings", "limitations", "complete"}:
            raise ValueError()
        summary = _report_text(data["summary"], 8000)
        if type(data["complete"]) is not bool:
            raise ValueError()
        lists = {}
        for field, maximum in (("findings", 3000), ("limitations", 1000)):
            values = data[field]
            if not isinstance(values, list) or len(values) > 12:
                raise ValueError()
            lists[field] = [_report_text(value, maximum) for value in values]
        if data["complete"] and not lists["findings"]:
            raise ValueError()
        return {"summary": summary, **lists, "complete": data["complete"]}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ArtifactError("分析回報必須是完整 JSON，包含 summary、findings、limitations、complete；完成時至少提供一項有效發現。") from None


def _size(context: dict) -> int:
    # ASCII escaping is the worst case. Both the default encoder and a readable
    # ensure_ascii=False encoder therefore stay within the advertised budget.
    return len(json.dumps(context, ensure_ascii=True))


def _mention(focus: str, term: str) -> int | None:
    match = re.search(r"(?<![a-z0-9_.-])" + re.escape(term.casefold()) + r"(?![a-z0-9_.-])", focus)
    return match.start() if match else None


def _default_priority(name: str) -> int:
    name = name.casefold()
    if name.startswith("readme."):
        return 0
    if name in {"main.py", "main.js", "main.ts", "main.rs", "main.go"}:
        return 1
    if name == "pyproject.toml":
        return 2
    if name == "package.json":
        return 3
    if name.startswith("requirements") and name.endswith(".txt"):
        return 4
    return 5


def build_context(workspace: Path, focus: str) -> dict:
    """Copy bounded real source text into data, without granting more CLI access.

    The returned object contains ``manifest`` paths and ``files`` entries with
    path/content/source_bytes/redacted/truncated. Credential forms are masked.
    Its JSON is at most 12,000 characters, even with ASCII escaping enabled.
    """
    try:
        root = Path(workspace).expanduser().absolute()
        if _linked(root) or not root.is_dir():
            raise ArtifactError("來源上下文必須來自一般協作副本目錄，不能是連結。")
        root = root.resolve()
        inventory = _inventory(root)
        focus = focus.replace("\\", "/").casefold()[:64000] if isinstance(focus, str) else ""
        ranked = []
        for relative, content in inventory.items():
            parts = PurePosixPath(relative).parts
            if any(part.casefold() in SKIP_DIRS or SECRET_NAME.search(part) for part in parts):
                continue
            target = _safe_target(root, relative)
            full = _mention(focus, relative)
            basename = _mention(focus, parts[-1])
            tier = 0 if full is not None else 1 if basename is not None else 2
            position = full if full is not None else basename if basename is not None else 0
            mtime = target.stat(follow_symlinks=False).st_mtime_ns
            rank = (tier, position, _default_priority(parts[-1]) if tier == 2 else 0, -mtime, relative.casefold())
            ranked.append((rank, relative, content))
        ranked.sort(key=lambda entry: entry[0])
        context = {"scope": "host_owned_isolated_copy", "read_only": True, "workspace": str(root),
                   "total_files": len(ranked), "manifest": [], "manifest_truncated": False,
                   "files": [], "content_truncated": False}
        # Reserve most of the prompt for real source content, not a long index.
        for _, relative, _ in ranked[:MAX_MANIFEST_PATHS]:
            context["manifest"].append(relative)
            if _size(context) > 3500:
                context["manifest"].pop()
                break
        context["manifest_truncated"] = len(context["manifest"]) < len(ranked)
        selected = []
        originals = []
        for entry in ranked[:MAX_CONTENT_FILES]:
            _, relative, content = entry
            source = content.decode("utf-8")
            cleaned = redact(source)
            context["files"].append({"path": relative, "content": "", "source_bytes": len(content),
                                     "redacted": cleaned != source[:8000], "truncated": bool(source)})
            if _size(context) > MAX_CONTEXT_CHARS - 512:
                context["files"].pop()
                break
            selected.append(entry)
            originals.append((source, cleaned))
        for index, (source, cleaned) in enumerate(originals):
            record = context["files"][index]
            share = max(0, (MAX_CONTEXT_CHARS - 32 - _size(context)) // (len(originals) - index))
            limit = min(MAX_CONTEXT_CHARS - 32, _size(context) + share)
            low, high = 0, len(cleaned)
            while low < high:
                middle = (low + high + 1) // 2
                record["content"] = cleaned[:middle]
                record["truncated"] = len(source) > 8000 or middle < len(cleaned)
                if _size(context) <= limit:
                    low = middle
                else:
                    high = middle - 1
            record["content"] = cleaned[:low]
            record["truncated"] = len(source) > 8000 or low < len(cleaned)
        context["content_truncated"] = len(selected) < len(ranked) or any(record["truncated"] for record in context["files"])
        if _size(context) > MAX_CONTEXT_CHARS:
            raise ArtifactError("來源上下文路徑或中繼資料超過大小限制。")
        return context
    except ArtifactError:
        raise
    except (OSError, ValueError, UnicodeError, RuntimeError) as error:
        raise ArtifactError("無法安全讀取協作副本的來源上下文。") from error
