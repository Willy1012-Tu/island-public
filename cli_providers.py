"""Native coding CLI adapters: public task telemetry, not private model reasoning."""
from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from agent_brain import AgentBrain, _saved_model
from codex_catalog import discover_models, saved_reasoning_effort, REASONING_EFFORTS

PROVIDERS = {"codex": ("Codex", "codex"), "claude": ("Claude 4.6", "agy"),
             "antigravity": ("Antigravity", "agy")}
TRANSPORTS = {"codex": "codex", "claude": "antigravity", "antigravity": "antigravity"}
CLAUDE_MODELS = [
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)"},
    {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)"},
]
ANTIGRAVITY_MODELS = [
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"},
    {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)"},
    {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
    {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"},
]
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".md",
                   ".txt", ".json", ".toml", ".yaml", ".yml", ".xml", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".cs"}
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "artifacts", "dist", "build", "target"}
SECRET_NAME = re.compile(r"(?:secret|credential|token|auth|password|private|settings\.local|office-bridge\.(?:json|lock))", re.I)
MAX_PUBLIC_OUTPUT_BYTES = 32 * 1024
MAX_OUTPUT_SCHEMA_BYTES = 64 * 1024


def redact(text: Any) -> str:
    """Bound public telemetry and mask common credential forms before publishing."""
    value = str(text)[:16000]
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{12,})", "[REDACTED]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
    secret_key = r"(?<![\w])[\"']?(?:[A-Z0-9_]{0,64}api[_-]?key|access[_-]?token|password|secret|authorization)[\"']?\s*[=:]\s*"
    value = re.sub(r'(?i)(' + secret_key + r')"(?:\\.|[^"\\])*"', r'\1"[REDACTED]"', value)
    value = re.sub(r"(?i)(" + secret_key + r")'(?:\\.|[^'\\])*'", r"\1'[REDACTED]'", value)
    value = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|password)\s*[=:]\s*)[^\s,;\"']+", r"\1[REDACTED]", value)
    return value[:8000]


def _candidate_paths(provider: str) -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    project = Path(__file__).resolve().parent
    if provider in ("claude", "antigravity"):
        return [project / ".tools" / "agy.exe", project / ".tools" / "agy" / "bin" / "agy.exe",
                local / "agy" / "bin" / "agy.exe", home / ".local" / "bin" / "agy.exe"]
    return sorted((home / ".vscode" / "extensions").glob(
        "openai.chatgpt-*-win32-x64/bin/windows-x86_64/codex.exe"), reverse=True)


def discover_providers(commands: dict[str, str] | None = None) -> list[dict[str, Any]]:
    result = []
    for provider, (name, default) in PROVIDERS.items():
        requested = (commands or {}).get(provider)
        if provider == "claude" and not requested:
            requested = (commands or {}).get("antigravity")
        found = shutil.which(requested or default)
        if found is None and not requested:
            for candidate in _candidate_paths(provider):
                try:
                    if candidate.is_file():
                        found = str(candidate)
                        break
                except OSError:
                    continue
        if found and os.name == "nt" and Path(found).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            found = None  # Native executables keep user prompts out of shell command parsing.
        transport = TRANSPORTS[provider]
        options = CLAUDE_MODELS if provider == "claude" else ANTIGRAVITY_MODELS if provider == "antigravity" else []
        configured_model = options[0]["id"] if options else _saved_model()
        configured_effort = None
        if provider == "codex":
            configured_effort = saved_reasoning_effort()
            options = discover_models(found, configured_model, configured_effort)
            if options and configured_model not in {option["id"] for option in options}:
                configured_model = next((option["id"] for option in options if option.get("is_default")), options[0]["id"])
            selected = next((option for option in options if option["id"] == configured_model), {})
            if configured_effort not in selected.get("reasoning_efforts", []):
                configured_effort = selected.get("default_reasoning_effort")
        reason = "CLI 已找到；登入狀態需由實際任務確認。"
        if provider == "claude":
            reason = "透過 Antigravity 帳號執行 Claude 4.6，不需要 Claude Code。"
        if not found:
            reason = f"尚未找到 {default} CLI，請安裝或指定執行檔。"
        result.append({"id": provider, "name": name, "available": bool(found), "command": found,
                       "reason": reason, "transport": transport, "configured_model": configured_model,
                       "configured_reasoning_effort": configured_effort,
                       "model_options": [dict(option) for option in options], "auth": "unknown",
                       "execution_mode": "isolated" if transport == "antigravity" else "read_only",
                       "execution_note": "透過 agy 在暫存原始碼副本規劃，變更不套用原專案。" if transport == "antigravity" else "只讀取專案並回報任務結果。"})
    return result


def _execution_options(execution_mode: str, shared_workspace: bool) -> None:
    if type(shared_workspace) is not bool:
        raise ValueError("shared_workspace must be a boolean.")
    if execution_mode not in ("read_only", "workspace_write"):
        raise ValueError("Unsupported execution mode.")
    if execution_mode == "workspace_write" and not shared_workspace:
        raise ValueError("Writing is permitted only in a host-owned shared isolated workspace.")


def _schema_json(schema: dict | None) -> str | None:
    if schema is None:
        return None
    if not isinstance(schema, dict):
        raise ValueError("output_schema must be a JSON Schema object.")
    try:
        serialized = json.dumps(schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("output_schema must contain finite JSON-compatible values.") from error
    if len(serialized.encode("utf-8")) > MAX_OUTPUT_SCHEMA_BYTES:
        raise ValueError("output_schema exceeds the supported size.")
    return serialized


def build_command(provider: str, executable: str, workspace: Path, *, model: str | None = None,
                  timeout: float = 300, reasoning_effort: str | None = None,
                  execution_mode: str = "read_only", shared_workspace: bool = False,
                  output_schema: dict | None = None,
                  output_schema_path: Path | None = None) -> list[str]:
    """Build native *transport* argv; the Claude office uses antigravity here.

    The legacy Claude Code protocol helper remains available for existing
    integration fixtures, but logical Claude jobs never select that transport.
    """
    _execution_options(execution_mode, shared_workspace)
    schema = _schema_json(output_schema)
    if output_schema_path is not None and schema is None:
        raise ValueError("A managed schema path requires output_schema.")
    if reasoning_effort is not None and (provider != "codex" or not isinstance(reasoning_effort, str)
                                         or reasoning_effort not in REASONING_EFFORTS):
        raise ValueError("Unsupported reasoning effort for this transport.")
    if provider == "codex":
        command = [executable, "--no-daemon", "--ask-for-approval", "never", "exec", "--json",
                   "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--sandbox",
                   "workspace-write" if execution_mode == "workspace_write" else "read-only",
                   "--disable", "plugins", "--disable", "apps", "--disable", "hooks", "--disable", "multi_agent",
                   "-c", 'web_search="disabled"',
                   "-c", "project_doc_max_bytes=0", "--color", "never", "--cd", str(workspace)]
        if model:
            command += ["--model", model]
        if reasoning_effort is not None:
            command += ["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)]
        if schema is not None:
            if output_schema_path is None:
                raise ValueError("Codex structured output requires a managed schema file; use run_cli.")
            command += ["--output-schema", str(output_schema_path)]
        return command + ["-"]
    if provider == "claude":
        if execution_mode != "read_only":
            raise ValueError("The legacy Claude Code helper is read-only; the Claude office uses Antigravity.")
        command = [executable, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                   "--safe-mode", "--restricted", "--tools", "Read,Grep,Glob", "--permission-mode", "plan",
                   "--permission-prompts", "none", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--no-session-persistence", "--no-chrome"]
        if model:
            command += ["--model", model]
        if schema is not None:
            command += ["--json-schema", str(output_schema_path) if output_schema_path is not None else schema]
        return command
    if provider == "antigravity":
        command = [executable, "--input-format", "stream-json", "--output-format", "stream-json",
                   "--mode=accept-edits" if execution_mode == "workspace_write" else "--mode=plan", "--sandbox"]
        if model:
            command += ["--model", model]
        if schema is not None:
            command += ["--json-schema", str(output_schema_path) if output_schema_path is not None else schema]
        return command
    raise ValueError("Unknown provider.")


def _copy_path_allowed(path: Path, source: Path) -> bool:
    """Junctions need an explicit check: os.walk(followlinks=False) may enter them."""
    try:
        junction = getattr(path, "is_junction", None)
        if path.is_symlink() or (callable(junction) and junction()):
            return False
        return path.resolve().is_relative_to(source)
    except (OSError, RuntimeError):
        return False


def copy_source_workspace(source: Path, destination: Path) -> int:
    """Copy bounded non-secret source files; never follow links out of the project."""
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("Source and isolated workspace must be separate.")
    destination.mkdir(parents=True, exist_ok=True)
    copied = total = 0
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        if not _copy_path_allowed(current_path, source):
            directories[:] = []
            continue
        directories[:] = [name for name in directories if not name.startswith('.') and name not in SKIP_DIRS
                          and not SECRET_NAME.search(name) and _copy_path_allowed(current_path / name, source)]
        for name in sorted(files):
            path = current_path / name
            if name.startswith('.') or SECRET_NAME.search(name) or path.suffix.lower() not in SOURCE_SUFFIXES or not _copy_path_allowed(path, source):
                continue
            try:
                size = path.stat().st_size
                if size > 1024 * 1024 or total + size > 20 * 1024 * 1024 or copied >= 2000:
                    continue
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                total += size
                copied += 1
            except OSError:
                continue
    return copied


def _public_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(block.get("text", "")) for block in value
                         if isinstance(block, dict) and block.get("type") == "text")
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return ""


def _public_response(value: Any) -> str:
    """Serialize an explicitly public final response, including structured JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("type") == "text" and isinstance(value.get("text"), str):
        return value["text"]
    if isinstance(value, dict) and value.get("type") in ("thinking", "reasoning", "redacted_thinking", "tool_use", "tool_result"):
        return ""
    if isinstance(value, list) and any(isinstance(block, dict) and "type" in block for block in value):
        return _public_text(value)  # Text blocks only; never thinking/tool blocks.
    if isinstance(value, (dict, list, bool, int, float)):
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError):
            pass
    return ""


def _final_response(result: dict, field: str) -> str:
    if result.get("structured_output") is not None:
        structured = result["structured_output"]
        try:
            return json.dumps(structured, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError):
            return ""
    return _public_response(result.get(field))


class StreamNormalizer:
    """Normalize a native stream protocol, independently of logical office ID."""
    def __init__(self, provider: str):
        if provider not in PROVIDERS:
            raise ValueError("Unknown provider.")
        self.provider = provider
        self.tools: dict[str, str] = {}
        self.output = ""
        self.output_truncated = False
        self._agy_response_step: Any = None

    def _capture_output(self, text: str, *, append: bool = False) -> None:
        """Keep raw public text for the host; browser events remain redacted."""
        if not isinstance(text, str):
            return
        if append and self.output_truncated:
            return
        encoded = ((self.output + text) if append else text).encode("utf-8", errors="replace")
        self.output_truncated = (self.output_truncated if append else False) or len(encoded) > MAX_PUBLIC_OUTPUT_BYTES
        self.output = encoded[:MAX_PUBLIC_OUTPUT_BYTES].decode("utf-8", errors="ignore")

    @staticmethod
    def _event(kind: str, text: Any = "", **details: Any) -> dict[str, Any]:
        return {"kind": kind, "text": redact(text), **details}

    def parse(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        try:
            events = self._codex(payload) if self.provider == "codex" else self._claude(payload) if self.provider == "claude" else self._agy(payload)
        except (AttributeError, TypeError, ValueError, KeyError, RecursionError):
            return []
        for event in events:
            event["text"] = redact(event.get("text", ""))
            if event.get("tool"):
                event["tool"] = redact(event["tool"])[:120]
        return events

    def _codex(self, data: dict) -> list[dict]:
        kind = data.get("type")
        if kind == "thread.started":
            return [self._event("status", "工作階段已建立", session_id=data.get("thread_id"))]
        if kind == "turn.started":
            return [self._event("status", "正在處理任務")]
        if kind == "turn.completed":
            return [self._event("result", "任務完成", status="completed", usage=data.get("usage", {}))]
        if kind in ("turn.failed", "error"):
            error = data.get("error", data.get("message", "CLI 回報錯誤"))
            if isinstance(error, dict):
                error = error.get("message", "CLI 回報錯誤")
            return [self._event("error", error), *([self._event("result", error, status="failed")] if kind == "turn.failed" else [])]
        item = data.get("item", {})
        if kind not in ("item.started", "item.updated", "item.completed") or not isinstance(item, dict):
            return []
        item_type = item.get("type")
        if item_type == "agent_message" and item.get("text"):
            if kind == "item.completed":
                self._capture_output(_public_response(item["text"]))
            return [self._event("message", item["text"])]
        if item_type in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
            tool = {"command_execution": "Shell", "mcp_tool_call": str(item.get("tool", "MCP")), "web_search": "WebSearch", "file_change": "FileChange"}[item_type]
            summary = item.get("command") or item.get("query") or item.get("aggregated_output")
            if kind == "item.completed" and item.get("aggregated_output"):
                summary = ((str(item.get("command")) + "\n") if item.get("command") else "") + str(item["aggregated_output"])
            if not summary and item_type == "file_change":
                summary = ", ".join(str(change.get("path", "")) for change in item.get("changes", []) if isinstance(change, dict))
            status = "completed" if kind == "item.completed" else "running"
            if item.get("status") in ("failed", "error") or item.get("exit_code") not in (None, 0):
                status = "failed"
            return [self._event("tool", summary or tool, tool=tool, status=status)]
        if item_type == "plan":
            return [self._event("status", "正在整理任務計畫")]
        return []  # In particular: never publish reasoning items.

    def _claude(self, data: dict) -> list[dict]:
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            return [self._event("status", "工作階段已建立", session_id=data.get("session_id"), model=data.get("model"))]
        if kind == "system" and data.get("subtype") == "status":
            return [self._event("status", "正在整理上下文" if data.get("status") == "compacting" else "正在處理任務")]
        if kind == "stream_event":
            event = data.get("event", {})
            delta = event.get("delta", {})
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                self._capture_output(delta.get("text", ""), append=True)
                return [self._event("message", delta.get("text", ""), delta=True)]
            if event.get("type") == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "tool_use":
                    self.tools[block.get("id", "")] = block.get("name", "Tool")
                    return [self._event("tool", block.get("name", "Tool"), tool=block.get("name", "Tool"), status="running")]
            return []
        if kind in ("assistant", "user"):
            result = []
            content = data.get("message", {}).get("content", [])
            if kind == "assistant":
                visible = _public_text(content)
                if visible:
                    self._capture_output(visible)
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and kind == "assistant":
                    result.append(self._event("message", block.get("text", "")))
                elif block.get("type") == "tool_use":
                    name = block.get("name", "Tool")
                    self.tools[block.get("id", "")] = name
                    result.append(self._event("tool", json.dumps(block.get("input", {}), ensure_ascii=False), tool=name, status="running"))
                elif block.get("type") == "tool_result":
                    name = self.tools.get(block.get("tool_use_id", ""), "Tool")
                    result.append(self._event("tool", _public_text(block.get("content", "")), tool=name,
                                              status="failed" if block.get("is_error") else "completed"))
            if data.get("error"):
                result.append(self._event("error", data["error"]))
            return result
        if kind == "tool_progress":
            return [self._event("tool", "工具仍在執行", tool=data.get("tool_name", "Tool"), status="running")]
        if kind == "rate_limit_event":
            return [self._event("status", "服務用量限制：" + str(data.get("rate_limit_info", {}).get("status", "unknown")))]
        if kind == "result":
            failed = bool(data.get("is_error")) or data.get("subtype") not in (None, "success")
            response = _final_response(data, "result")
            message = response or "; ".join(str(e) for e in data.get("errors", []))
            if response and not failed:
                self._capture_output(response)
            return [self._event("result", message or ("任務失敗" if failed else "任務完成"),
                                status="failed" if failed else "completed", public_output=bool(message) and not failed,
                                usage=data.get("usage", {}), session_id=data.get("session_id"))]
        return []

    def _agy(self, data: dict) -> list[dict]:
        kind = data.get("event")
        if kind == "init":
            info = data.get("init", {})
            return [self._event("status", "工作階段已建立", session_id=data.get("conversation_id") or info.get("conversation_id"), model=info.get("model"))]
        if kind == "step_update":
            step = data.get("step_update", {})
            if step.get("step_type") == "agent_response":
                text = step.get("text_delta")
                if isinstance(text, str) and text:
                    step_id = step.get("step_index")
                    self._capture_output(text, append=step_id == self._agy_response_step)
                    self._agy_response_step = step_id
                return [self._event("message", text, delta=True)] if isinstance(text, str) and text else []
            if step.get("step_type") == "tool":
                tool = step.get("tool_info", {})
                name = step.get("tool_name") or tool.get("name", "Tool")
                done = step.get("state") == "DONE"
                text = _public_text(tool.get("output")) if done else json.dumps(tool.get("parameters", {}), ensure_ascii=False)
                return [self._event("tool", text or name, tool=name, status="completed" if done else "running")]
            return []
        if kind == "result":
            result = data.get("result", {})
            status = str(result.get("status", "ERROR")).upper()
            if status in ("RUNNING", "WAITING"):
                return [self._event("status", "等待服務回應")]
            normalized = "completed" if status == "SUCCESS" else "cancelled" if status in ("CANCELED", "CANCELLED", "INTERRUPTED") else "failed"
            response = _final_response(result, "response")
            if response and normalized == "completed":
                self._capture_output(response)
            return [self._event("result", response or _public_text(result.get("error")) or status,
                                status=normalized, public_output=bool(response) and normalized == "completed",
                                session_id=result.get("conversation_id"), usage=result.get("usage", {}))]
        return []


def run_cli(provider: str, prompt: str, workspace: Path, emit: Callable[[dict], None], cancel: threading.Event,
            *, command: str | None = None, timeout: float = 300, model: str | None = None,
            reasoning_effort: str | None = None, execution_mode: str = "read_only",
            shared_workspace: bool = False, output_schema: dict | None = None,
            native_schema: bool = True) -> dict[str, Any]:
    """Run one owned native process, emitting normalized public JSONL as it arrives."""
    if provider not in PROVIDERS or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A known provider and nonempty prompt are required.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive.")
    if type(native_schema) is not bool:
        raise ValueError("native_schema must be a boolean.")
    _execution_options(execution_mode, shared_workspace)
    serialized_schema = _schema_json(output_schema)
    effective_prompt = prompt
    if serialized_schema is not None and not native_schema:
        # Explicit output-format compatibility only. The host must still parse
        # and validate the whole decision before dispatching any handoff.
        effective_prompt += (
            "\n\nOUTPUT FORMAT REQUIREMENT: Return exactly one JSON object matching the JSON Schema below. "
            "Do not include Markdown, code fences, commentary outside the JSON, or extra keys. "
            "This changes only output formatting: retain all workspace/tool permissions, "
            "do not bypass restrictions or retry denied operations. Describe any inability "
            "truthfully within the schema's allowed public summary fields.\nJSON Schema:\n"
            + serialized_schema
        )
    if reasoning_effort is not None and (provider != "codex" or not isinstance(reasoning_effort, str)
                                         or reasoning_effort not in REASONING_EFFORTS):
        raise ValueError("Unsupported reasoning effort for this office.")
    result: dict[str, Any] = {"status": "failed", "exit_code": None, "model": model,
                              "session_id": None, "usage": {}, "error": None, "output": "",
                              "output_truncated": False, "execution_mode": execution_mode,
                              "shared_workspace": shared_workspace}
    info = next(p for p in discover_providers({provider: command} if command else None) if p["id"] == provider)
    if not info["available"]:
        result["error"] = info["reason"]
        emit({"kind": "error", "text": result["error"]})
        return result
    if cancel.is_set():
        return {**result, "status": "cancelled"}
    transport = info.get("transport", TRANSPORTS[provider])
    result["transport"] = transport
    allowed_models = {option["id"] for option in info.get("model_options", [])}
    if model is not None and allowed_models and model not in allowed_models:
        raise ValueError("Model is not configured for this office.")
    selected_model = model or info.get("configured_model") or (_saved_model() if provider == "codex" else None)
    selected_effort = None
    if provider == "codex":
        option = next((item for item in info.get("model_options", []) if item["id"] == selected_model), {})
        supported = option.get("reasoning_efforts", [])
        if reasoning_effort is not None and reasoning_effort not in supported:
            raise ValueError("Reasoning effort is not supported by the selected model.")
        configured_effort = info.get("configured_reasoning_effort")
        selected_effort = reasoning_effort or (configured_effort if configured_effort in supported
                                               else option.get("default_reasoning_effort"))
    result["reasoning_effort"] = selected_effort
    proc = None
    stop_pumps = threading.Event()
    with contextlib.ExitStack() as stack:
        run_root = Path(workspace).resolve()
        if not run_root.is_dir():
            raise ValueError("Workspace does not exist.")
        if transport == "antigravity" and not shared_workspace:
            run_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="island-monitor-agy-")))
            count = copy_source_workspace(Path(workspace), run_root)
            emit({"kind": "status", "text": f"已建立隔離副本（{count} 個原始碼檔案），不套用變更到原專案。"})
        elif shared_workspace:
            emit({"kind": "status", "text": "使用主程式建立的共享隔離工作區；變更仍需審閱後才套用原專案。"})
        result["model"] = selected_model
        schema_path = None
        if native_schema and serialized_schema is not None and (transport == "codex" or len(serialized_schema) > 8192):
            schema_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="island-cli-schema-")))
            schema_path = schema_root / "output-schema.json"
            schema_path.write_text(serialized_schema, encoding="utf-8")
        argv = build_command(transport, info["command"], run_root, model=selected_model,
                             timeout=timeout, reasoning_effort=selected_effort,
                             execution_mode=execution_mode, shared_workspace=shared_workspace,
                             output_schema=output_schema if native_schema else None, output_schema_path=schema_path)
        incoming: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=256)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}

        def pump(stream, label):
            try:
                while not stop_pumps.is_set():
                    line = stream.readline(1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 1024 * 1024:
                        continue
                    while not stop_pumps.is_set():
                        try:
                            incoming.put((label, line), timeout=0.1)
                            break
                        except queue.Full:
                            pass
            except (OSError, ValueError):
                pass
            finally:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
                while not stop_pumps.is_set():
                    try:
                        incoming.put((label, None), timeout=0.1)
                        break
                    except queue.Full:
                        pass

        try:
            proc = subprocess.Popen(argv, cwd=run_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=False, **options)
            emit({"kind": "status", "text": "CLI 程序已啟動，等待任務事件。"})
            for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                threading.Thread(target=pump, args=(stream, label), daemon=True).start()
            stdin_text = (json.dumps({"event": "user", "message": {"content": effective_prompt}}, ensure_ascii=False) + "\n"
                          if transport == "antigravity" else effective_prompt + "\n")

            def write_prompt():
                try:
                    proc.stdin.write(stdin_text)
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass

            threading.Thread(target=write_prompt, daemon=True).start()
            deadline = time.monotonic() + timeout
            normalizer = StreamNormalizer(transport)
            ended = set()
            terminal = None
            missing_schema_output = False
            last_diagnostic = ""
            exited_at = None
            while True:
                if cancel.is_set():
                    AgentBrain._stop_process(proc)
                    result["status"] = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    AgentBrain._stop_process(proc)
                    result.update(status="timed_out", error=f"任務超過 {timeout:g} 秒，已停止。")
                    break
                if proc.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    if len(ended) == 2 or time.monotonic() - exited_at > 2:
                        break
                try:
                    label, line = incoming.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    ended.add(label)
                    continue
                if label == "stderr":
                    if line.strip():
                        last_diagnostic = redact(line.strip())[:1000]
                    continue
                try:
                    payload = json.loads(line)
                    events = normalizer.parse(payload)
                    result["output"] = normalizer.output
                    result["output_truncated"] = normalizer.output_truncated
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
                for event in events:
                    if event.get("session_id"):
                        result["session_id"] = event["session_id"]
                    if event.get("model"):
                        result["model"] = event["model"]
                    if isinstance(event.get("usage"), dict):
                        result["usage"] = {k: v for k, v in event["usage"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                        event["usage"] = result["usage"]
                    if event["kind"] == "error":
                        result["error"] = event["text"]
                    if event["kind"] == "result":
                        terminal = event.get("status")
                        if (transport == "antigravity" and output_schema is not None and terminal == "completed"
                                and not result["output"].strip()):
                            # Some native versions report SUCCESS after a schema
                            # failure without returning any public payload.
                            missing_schema_output = True
                            terminal = "failed"
                            continue  # Never publish that false SUCCESS event.
                        if terminal == "completed":
                            missing_schema_output = False
                        if terminal == "failed":
                            result["error"] = event["text"]
                    emit(event)
            result["exit_code"] = proc.poll()
            if result["status"] not in ("cancelled", "timed_out"):
                if missing_schema_output:
                    result.update(status="failed", error=last_diagnostic or "CLI 未提供結構化結果，無法執行後續委派。")
                elif proc.returncode == 0 and terminal in ("completed", "cancelled"):
                    result["status"] = terminal
                    if terminal == "completed":
                        result["error"] = None
                else:
                    result["status"] = "failed"
                    result["error"] = result["error"] or last_diagnostic or "CLI 未回報完整任務結果，請確認安裝版本與登入狀態。"
        except OSError as exc:
            result["error"] = redact(exc.strerror or type(exc).__name__)
        finally:
            stop_pumps.set()
            if proc is not None:
                if proc.poll() is None:
                    AgentBrain._stop_process(proc)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                result["exit_code"] = proc.poll()
        if result["error"]:
            emit({"kind": "error", "text": result["error"]})
        return result
