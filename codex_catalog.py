"""Bounded, metadata-only Codex model discovery; never starts an agent turn."""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_MAX_BYTES = 2 * 1024 * 1024
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}\Z")
_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_LOCK = threading.Lock()


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def saved_reasoning_effort() -> str | None:
    """Read only a non-secret preference; do not inherit tools or permissions."""
    try:
        with (_codex_home() / "config.toml").open("rb") as source:
            raw = source.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return None
        value = tomllib.loads(raw.decode("utf-8")).get("model_reasoning_effort")
        return value if isinstance(value, str) and value in REASONING_EFFORTS else None
    except (OSError, ValueError, UnicodeError):
        return None


def parse_models(payload: Any) -> list[dict[str, Any]]:
    """Translate only picker-visible public fields from `codex debug models`."""
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    options = []
    seen = set()
    for entry in models[:200]:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        model_id = entry.get("slug")
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id) or model_id in seen:
            continue
        efforts = []
        levels = entry.get("supported_reasoning_levels", [])
        for level in levels[:16] if isinstance(levels, list) else []:
            value = level.get("effort") if isinstance(level, dict) else None
            if isinstance(value, str) and value in REASONING_EFFORTS and value not in efforts:
                efforts.append(value)
        default = entry.get("default_reasoning_level")
        default = default if default in efforts else (efforts[0] if efforts else None)
        name = entry.get("display_name")
        name = "".join(char for char in name[:240] if char.isprintable()) if isinstance(name, str) else model_id
        option = {"id": model_id, "name": name or model_id,
                  "reasoning_efforts": efforts, "default_reasoning_effort": default}
        if entry.get("is_default") is True:
            option["is_default"] = True
        options.append(option)
        seen.add(model_id)
    return options


def _load_json(source) -> list[dict[str, Any]]:
    raw = source.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        return []
    try:
        return parse_models(json.loads(raw))
    except (ValueError, UnicodeError, RecursionError):
        return []


def _read_catalog(executable: str) -> list[dict[str, Any]]:
    """Capture a short native metadata command, with an offline cache fallback."""
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        with tempfile.TemporaryDirectory(prefix="island-model-catalog-") as folder, tempfile.TemporaryFile() as output:
            result = subprocess.run([executable, "--no-daemon", "debug", "models"],
                                    cwd=folder, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=5, shell=False, **flags)
            if result.returncode == 0:
                output.seek(0)
                options = _load_json(output)
                if options:
                    return options
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        with (_codex_home() / "models_cache.json").open("rb") as source:
            return _load_json(source)
    except OSError:
        return []


def discover_models(executable: str | None, configured_model: str | None,
                    configured_effort: str | None) -> list[dict[str, Any]]:
    """Use CLI-advertised choices, retaining only saved settings if unavailable.

    Short-lived caching avoids a metadata process on every snapshot or task.
    No inference, project files, model instructions, or credentials are returned.
    """
    options = []
    if executable and Path(executable).is_file():
        key = (str(Path(executable).resolve()), str(_codex_home().resolve()))
        with _LOCK:
            cached = _CACHE.get(key)
            if cached is not None and time.monotonic() - cached[0] < 60:
                options = copy.deepcopy(cached[1])
            else:
                options = _read_catalog(executable)
                _CACHE[key] = (time.monotonic(), copy.deepcopy(options))
    if (isinstance(configured_model, str) and _MODEL_ID.fullmatch(configured_model)
            and configured_model not in {option["id"] for option in options}):
        effort = configured_effort if isinstance(configured_effort, str) and configured_effort in REASONING_EFFORTS else None
        options.insert(0, {"id": configured_model, "name": configured_model + ("（本機設定）" if options else ""),
                           "reasoning_efforts": [effort] if effort else [], "default_reasoning_effort": effort})
    return options
