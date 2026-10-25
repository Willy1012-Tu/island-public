"""Five bounded MCP stdio tools for a separately running Island office.

This server implements the initialization-based MCP revisions through
2025-11-25. It never starts the office, invokes a shell, or applies changes to
the original project. Configuration paths are fixed when the process starts.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

MAX_INPUT_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
# A result appears both as text JSON and structuredContent. Leave room for both
# representations, JSON escaping, and a request ID up to the input byte limit.
PUBLIC_RESULT_BYTES = 96 * 1024
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PROVIDERS = ("codex", "antigravity", "claude")
_JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
_PRIVATE_KEYS = frozenset({
    "token", "nonce", "bearer", "authorization", "api_key", "access_token", "refresh_token",
    "prompt", "command", "commands", "args", "arguments", "cwd", "env",
    "internal_thought", "thinking", "reasoning", "signature", "raw", "raw_output",
})
_PRIVATE_NORMALIZED = frozenset(re.sub(r"[^a-z]", "", key) for key in _PRIVATE_KEYS) | {
    "password", "secret", "bridgetoken", "bearertoken", "privatecommand", "environment",
}


class ToolError(RuntimeError):
    """An expected, already safe message suitable for a tool result."""


class InvalidParams(ValueError):
    pass


def _utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


_PROVIDER = {"type": "string", "enum": list(PROVIDERS)}
_MODEL = {"type": "string", "minLength": 1, "maxLength": 240}
_EFFORT = {"type": "string", "minLength": 1, "maxLength": 40}
_MEMBER = {"type": "object", "additionalProperties": False,
           "properties": {"provider": _PROVIDER, "model": _MODEL, "reasoning_effort": _EFFORT},
           "required": ["provider"]}
_START = {"type": "object", "additionalProperties": False,
          "properties": {
              "provider": _PROVIDER, "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
              "model": _MODEL, "reasoning_effort": _EFFORT,
              "collaboration": {"type": "object", "additionalProperties": False,
                                "properties": {"members": {"type": "array", "minItems": 1,
                                                           "maxItems": 2, "uniqueItems": True,
                                                           "items": _MEMBER}}, "required": ["members"]}},
          "required": ["provider", "prompt", "collaboration"]}
_JOB = {"type": "object", "additionalProperties": False,
        "properties": {"job_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}},
        "required": ["job_id"]}
_TOOLS = [
    {"name": "island_status", "description": "查看正在執行的 Island 辦公室及可用模型；不啟動後端。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "island_start_collaboration", "description": "交辦隔離副本中的團隊任務並立即取得任務 ID；原專案套用仍須辦公室人工確認。",
     "inputSchema": _START,
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}},
    {"name": "island_get_task", "description": "以任務 ID 查看公開狀態、結論與團隊交接。",
     "inputSchema": _JOB, "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "island_cancel_task", "description": "明確取消指定任務及其團隊子任務。",
     "inputSchema": _JOB, "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "island_review_changes", "description": "讀取團隊隔離副本的公開差異供審查；不套用修改。",
     "inputSchema": _JOB, "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}},
]


def _selection(value: Any, *, lead: bool) -> dict:
    allowed = {"provider", "model", "reasoning_effort"} | ({"prompt", "collaboration"} if lead else set())
    required = {"provider", "prompt", "collaboration"} if lead else {"provider"}
    if not isinstance(value, dict) or not required <= set(value) or not set(value) <= allowed:
        raise InvalidParams("Only the documented collaboration fields are accepted.")
    if not isinstance(value.get("provider"), str) or value["provider"] not in PROVIDERS:
        raise InvalidParams("Choose codex, antigravity, or claude.")
    for key, bound in (("model", 240), ("reasoning_effort", 40)):
        if key in value:
            item = value[key]
            if (not isinstance(item, str) or not item.strip() or len(item) > bound
                    or not _utf8(item) or any(ord(char) < 32 for char in item)):
                raise InvalidParams("Model and effort must be bounded nonempty strings.")
    if "reasoning_effort" in value and value["provider"] != "codex":
        raise InvalidParams("Reasoning effort is only supported for Codex.")
    return value


def _arguments(name: str, arguments: Any) -> dict:
    if not isinstance(arguments, dict):
        raise InvalidParams("Tool arguments must be a JSON object.")
    if name == "island_status":
        if arguments:
            raise InvalidParams("Status takes no arguments.")
    elif name == "island_start_collaboration":
        _selection(arguments, lead=True)
        prompt = arguments["prompt"]
        collaboration = arguments["collaboration"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000 or not _utf8(prompt) or "\0" in prompt:
            raise InvalidParams("Prompt must contain 1 to 8000 characters.")
        if not isinstance(collaboration, dict) or set(collaboration) != {"members"}:
            raise InvalidParams("Collaboration must contain only members.")
        members = collaboration["members"]
        if not isinstance(members, list) or not 1 <= len(members) <= 2:
            raise InvalidParams("Choose one or two teammates.")
        seen = {arguments["provider"]}
        for member in members:
            _selection(member, lead=False)
            if member["provider"] in seen:
                raise InvalidParams("Teammates must be distinct and different from the lead.")
            seen.add(member["provider"])
    else:
        if set(arguments) != {"job_id"} or not isinstance(arguments.get("job_id"), str) or not _JOB_ID.fullmatch(arguments["job_id"]):
            raise InvalidParams("A 32-character lowercase hexadecimal job_id is required.")
    return arguments


def _public(value: Any, depth: int = 0) -> Any:
    """Omit private transport/request fields; never expose a client object's state."""
    if depth > 20:
        raise ToolError("Office result nesting exceeds the supported limit.")
    if isinstance(value, dict):
        return {key: _public(item, depth + 1) for key, item in value.items()
                if isinstance(key, str) and re.sub(r"[^a-z]", "", key.casefold()) not in _PRIVATE_NORMALIZED
                and not re.sub(r"[^a-z]", "", key.casefold()).endswith("token")}
    if isinstance(value, list):
        return [_public(item, depth + 1) for item in value]
    if isinstance(value, str):
        if not _utf8(value):
            raise ToolError("Office returned invalid UTF-8 text.")
        text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        return re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})", "[REDACTED]", text)
    if value is None or type(value) in (bool, int, float):
        return value
    raise ToolError("Office returned an unsupported public result.")


def _fit_public(value: dict) -> dict:
    """Fit public telemetry into one global UTF-8 budget, preserving task identity."""
    def size(item):
        return len(json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))

    if size(value) <= PUBLIC_RESULT_BYTES:
        return value

    def text(item, limit):
        encoded = item.encode("utf-8")
        return item if len(encoded) <= limit else encoded[:limit].decode("utf-8", errors="ignore") + "…"

    def shrink(item, key=""):
        if isinstance(item, dict):
            return {name: shrink(child, name) for name, child in list(item.items())[:64]}
        if isinstance(item, list):
            limit = 0 if key == "events" else 12 if key == "changed_files" else 32
            return [shrink(child) for child in item[:limit]]
        if isinstance(item, str):
            limit = 16000 if key == "diff" else 128 if key in {
                "id", "job_id", "parent_job_id", "active_job_id", "team_run_id"} else 1000
            return text(item, limit)
        return item

    notice = "Public output was shortened to fit the MCP response budget. View full reports and diffs in the office UI."
    shortened = {**shrink(value), "truncated": True, "notice": notice}
    if size(shortened) <= PUBLIC_RESULT_BYTES:
        return shortened

    # Very large collections can still exceed the budget after text trimming.
    # Keep all supported task/child IDs and statuses, plus a small public summary.
    def job(item):
        if not isinstance(item, dict):
            return {}
        result = {key: text(item[key], 240) if isinstance(item.get(key), str) else item[key]
                  for key in ("id", "parent_job_id", "provider", "kind", "status", "role", "task_mode",
                              "cancel_requested", "elapsed_seconds", "model", "reasoning_effort") if key in item}
        for key in ("latest_message", "error"):
            if isinstance(item.get(key), str):
                result[key] = text(item[key], 400)
        if isinstance(item.get("collaboration"), dict):
            team = item["collaboration"]
            result["collaboration"] = {key: team[key] for key in ("phase", "round", "max_rounds") if key in team}
            if isinstance(team.get("summary"), str):
                result["collaboration"]["summary"] = text(team["summary"], 400)
        return result

    minimal = {"truncated": True, "notice": notice}
    for key in ("version", "type", "workspace"):
        if key in value:
            minimal[key] = text(value[key], 1000) if isinstance(value[key], str) else value[key]
    if isinstance(value.get("job"), dict):
        minimal["job"] = job(value["job"])
    for key, limit in (("children", 12), ("jobs", 20)):
        if isinstance(value.get(key), list):
            minimal[key] = [job(item) for item in value[key][:limit]]
    if isinstance(value.get("providers"), list):
        minimal["providers"] = [{key: shrink(item[key], key) for key in
                                 ("id", "name", "available", "auth", "phase", "model", "configured_model",
                                  "configured_reasoning_effort", "active_job_id", "team_run_id", "model_options")
                                 if key in item} for item in value["providers"][:3] if isinstance(item, dict)]
    if isinstance(value.get("artifact"), dict):
        minimal["artifact"] = {key: shrink(value["artifact"][key], key) for key in
                               ("id", "applied", "changed_files_total") if key in value["artifact"]}
    if size(minimal) > PUBLIC_RESULT_BYTES:
        # Provider model catalogs are optional; task identity/status above is not.
        for provider in minimal.get("providers", []):
            provider.pop("model_options", None)
    return minimal


class _OfficeClient:
    """Import/create the loopback client only when a real tool is invoked."""
    def __init__(self, project: Path, data_dir: Path | None):
        self.project, self.data_dir, self.client = project, data_dir, None

    def _call(self, method: str, *arguments):
        try:
            from office_bridge_client import BridgeClientError, OfficeBridgeClient
        except ImportError:
            raise ToolError("Island 辦公室橋接尚未就緒，請更新程式並由使用者重新開啟辦公室。") from None
        try:
            if self.client is None:
                self.client = OfficeBridgeClient(self.project, data_dir=self.data_dir)
            return getattr(self.client, method)(*arguments)
        except BridgeClientError as error:
            raise ToolError(str(error)[:1200]) from None

    def status(self): return self._call("status")
    def start(self, arguments): return self._call("start", arguments)
    def get_task(self, job_id): return self._call("get_task", job_id)
    def cancel(self, job_id): return self._call("cancel", job_id)
    def review(self, job_id): return self._call("review", job_id)


def _invoke(client, name: str, arguments: dict) -> dict:
    if name == "island_status":
        value = client.status()
    elif name == "island_start_collaboration":
        value = client.start(copy.deepcopy(arguments))
    elif name == "island_get_task":
        value = client.get_task(arguments["job_id"])
    elif name == "island_cancel_task":
        value = client.cancel(arguments["job_id"])
    else:
        value = client.review(arguments["job_id"])
    if not isinstance(value, dict):
        raise ToolError("Office returned an invalid public result.")
    if name == "island_status" and value.get("ready") is False:
        raise ToolError("Island 辦公室尚未就緒，請由使用者重新開啟辦公室；MCP 不會自動啟動後端。")
    return _fit_public(_public(value))


def _result(value: dict, version: str, *, error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))}], "isError": error}
    if not error and version in ("2025-06-18", "2025-11-25"):
        result["structuredContent"] = value
    return result


def _rpc_error(identifier, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _valid_id(value: Any) -> bool:
    if type(value) not in (str, int):
        return False
    return not isinstance(value, str) or _utf8(value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys.")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("Non-finite JSON.")


def _read_line(stream):
    """Bound both binary production streams and text streams used by tests."""
    line = stream.readline(MAX_INPUT_BYTES + 1)
    if not line:
        return None, None
    encoded = line.encode("utf-8") if isinstance(line, str) else line
    oversized = len(encoded.rstrip(b"\r\n")) > MAX_INPUT_BYTES
    while not line.endswith("\n" if isinstance(line, str) else b"\n") and len(line) >= MAX_INPUT_BYTES + 1:
        oversized = True
        line = stream.readline(MAX_INPUT_BYTES + 1)
        if not line:
            break
    terminated = line.endswith("\n" if isinstance(line, str) else b"\n")
    error = "MCP input exceeds the 128 KiB message limit." if oversized else None
    if not error and not terminated:
        error = "MCP messages must end with a newline."
    return encoded, error


def _write(stream, response: dict) -> bool:
    try:
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) + 1 > MAX_RESPONSE_BYTES:
            response = {"jsonrpc": "2.0", "id": response.get("id"), "result": _result(
                {"error": "Office result exceeds the MCP response limit; review details in the office UI."}, "2025-11-25", error=True)}
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            stream.write(encoded + b"\n")
        except TypeError:
            stream.write(encoded.decode("utf-8") + "\n")
        stream.flush()
        return True
    except (BrokenPipeError, OSError, UnicodeError):
        return False


def _serve(stdin, stdout, client) -> int:
    version = None
    while True:
        try:
            raw, framing_error = _read_line(stdin)
        except (OSError, UnicodeError):
            return 0
        if raw is None:
            return 0
        response = None
        if framing_error:
            response = _rpc_error(None, -32700, framing_error)
        else:
            try:
                request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite)
            except (ValueError, UnicodeError, RecursionError):
                request = None
                response = _rpc_error(None, -32700, "Invalid UTF-8 JSON message.")
            if response is None:
                if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                        or not isinstance(request.get("method"), str)):
                    response = _rpc_error(None, -32600, "Invalid JSON-RPC request.")
                elif "id" not in request:
                    # Never execute write tools as notifications. Disconnect or MCP
                    # cancellation also never cancels independently owned office jobs.
                    continue
                else:
                    identifier = request["id"]
                    method, params = request["method"], request.get("params", {})
                    if not _valid_id(identifier):
                        response = _rpc_error(None, -32600, "Invalid JSON-RPC request ID.")
                    elif not isinstance(params, dict):
                        response = _rpc_error(identifier, -32602, "Params must be a JSON object.")
                    elif method == "initialize":
                        if version is not None:
                            response = _rpc_error(identifier, -32600, "This MCP connection is already initialized.")
                        elif (not isinstance(params.get("protocolVersion"), str)
                              or not isinstance(params.get("capabilities"), dict)
                              or not isinstance(params.get("clientInfo"), dict)
                              or not all(isinstance(params["clientInfo"].get(key), str) for key in ("name", "version"))):
                            response = _rpc_error(identifier, -32602, "Initialize requires protocolVersion, capabilities, and clientInfo.")
                        else:
                            requested = params["protocolVersion"]
                            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
                            response = {"jsonrpc": "2.0", "id": identifier, "result": {
                                "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {"name": "island-office", "version": "1.0.0"},
                                "instructions": "Use only these five fixed office tools. Original-project changes require human review/apply in the office. MCP disconnect does not cancel office jobs."}}
                    elif method == "ping":
                        response = {"jsonrpc": "2.0", "id": identifier, "result": {}}
                    elif method not in ("tools/list", "tools/call"):
                        response = _rpc_error(identifier, -32601, "Method not found; this server supports initialization-based MCP through 2025-11-25.")
                    elif version is None:
                        response = _rpc_error(identifier, -32000, "Initialize the MCP connection first.")
                    elif method == "tools/list":
                        if set(params) - {"cursor", "_meta"} or params.get("cursor") not in (None, ""):
                            response = _rpc_error(identifier, -32602, "This fixed tool list has no pagination cursor.")
                        else:
                            tools = copy.deepcopy(_TOOLS)
                            for tool in tools:
                                if version == "2024-11-05":
                                    tool.pop("annotations", None)
                                if version in ("2025-06-18", "2025-11-25"):
                                    tool["outputSchema"] = {"type": "object", "additionalProperties": True}
                            response = {"jsonrpc": "2.0", "id": identifier, "result": {"tools": tools}}
                    else:
                        try:
                            if (set(params) - {"name", "arguments", "_meta"}
                                    or not isinstance(params.get("name"), str)
                                    or params["name"] not in {item["name"] for item in _TOOLS}):
                                raise InvalidParams("Unknown tool or unsupported tool-call fields.")
                            name = params["name"]
                            arguments = _arguments(name, params.get("arguments", {}))
                            value = _invoke(client, name, arguments)
                            result = _result(value, version)
                        except InvalidParams as error:
                            response = _rpc_error(identifier, -32602, str(error))
                        except ToolError as error:
                            message = str(error).encode("utf-8", errors="replace").decode("utf-8")
                            result = _result({"error": _public(message)}, version, error=True)
                        except Exception:
                            result = _result({"error": "Island office is not ready or returned an invalid response. Ask the user to reopen the office; no backend was started."}, version, error=True)
                        if response is None:
                            response = {"jsonrpc": "2.0", "id": identifier, "result": result}
        if response is not None and not _write(stdout, response):
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    # Preserve filesystem redirects for the bridge client's own ownership checks;
    # resolving a supplied profile first would hide a symlink/junction from it.
    project = args.project.expanduser().absolute()
    data_dir = args.data_dir.expanduser().absolute() if args.data_dir else None
    if (not project.is_dir() or project.is_symlink()
            or bool(getattr(project, "is_junction", lambda: False)())):
        print("Island MCP requires an existing fixed project directory.", file=sys.stderr)
        return 2
    try:
        return _serve(getattr(sys.stdin, "buffer", sys.stdin), getattr(sys.stdout, "buffer", sys.stdout), _OfficeClient(project, data_dir))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
