"""MCP transport/contracts use a fake office; no daemon, GUI, or model starts."""
from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import island_mcp as mcp

JOB_ID = "a" * 32
ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self):
        self.calls = []
        self.created = []
        self.response = {"ready": True, "message": "辦公室已就緒"}
        self.error = None

    def call(self, method, value=None):
        self.calls.append((method, value))
        if self.error:
            raise self.error
        if method == "start":
            self.created.append(copy.deepcopy(value))
        return copy.deepcopy(self.response)

    def status(self): return self.call("status")
    def start(self, arguments): return self.call("start", arguments)
    def get_task(self, job_id): return self.call("get_task", job_id)
    def cancel(self, job_id): return self.call("cancel", job_id)
    def review(self, job_id): return self.call("review", job_id)


def rpc(method, params=None, identifier=2):
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": {} if params is None else params}


def initialize(version="2025-11-25"):
    return rpc("initialize", {"protocolVersion": version, "capabilities": {},
                              "clientInfo": {"name": "unit-test", "version": "1"}}, identifier=1)


def run(messages, client=None, *, handshake=True):
    client = client or Client()
    requests = ([initialize()] if handshake else []) + messages
    source = io.BytesIO(b"".join(json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n" for item in requests))
    output = io.BytesIO()
    assert mcp._serve(source, output, client) == 0
    lines = output.getvalue().splitlines()
    assert all(len(line) + 1 <= mcp.MAX_RESPONSE_BYTES for line in lines)
    return [json.loads(line) for line in lines], client


def valid_start():
    return {"provider": "antigravity", "prompt": "請分析並在副本提案修改。", "model": "gemini-3.8-flash-high",
            "collaboration": {"members": [{"provider": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "low"},
                                          {"provider": "claude", "model": "claude-sonnet-4-6"}]}}


def test_initialize_list_and_ping_are_offline_and_only_five_fixed_tools_exist():
    messages, client = run([{"jsonrpc": "2.0", "method": "notifications/initialized"}, rpc("tools/list"), rpc("ping", identifier=3)])
    assert messages[0]["result"]["protocolVersion"] == "2025-11-25"
    assert messages[0]["result"]["capabilities"] == {"tools": {"listChanged": False}}
    tools = messages[1]["result"]["tools"]
    assert [item["name"] for item in tools] == [
        "island_status", "island_start_collaboration", "island_get_task", "island_cancel_task", "island_review_changes"]
    assert not any("apply" in item["name"] or "shell" in item["name"] for item in tools)
    assert tools[0]["inputSchema"]["additionalProperties"] is False
    start = tools[1]["inputSchema"]
    assert set(start["required"]) == {"provider", "prompt", "collaboration"}
    assert start["additionalProperties"] is False
    assert set(start["properties"]) == {"provider", "prompt", "model", "reasoning_effort", "collaboration"}
    assert messages[-1]["result"] == {} and not client.calls


@pytest.mark.parametrize("requested,negotiated", [(item, item) for item in mcp.SUPPORTED_VERSIONS] + [("2026-07-28", "2025-11-25")])
def test_supported_handshake_versions_and_structured_result_compatibility(requested, negotiated):
    messages, client = run([initialize(requested), rpc("tools/call", {"name": "island_status", "arguments": {}})], handshake=False)
    assert messages[0]["result"]["protocolVersion"] == negotiated
    result = messages[1]["result"]
    assert json.loads(result["content"][0]["text"]) == client.response
    assert ("structuredContent" in result) is (negotiated in ("2025-06-18", "2025-11-25"))
    assert result["isError"] is False


def test_modern_discovery_probe_gets_legacy_error_then_handshake_can_continue():
    messages, client = run([rpc("server/discover"), initialize(), rpc("tools/list", identifier=3)], handshake=False)
    assert messages[0]["error"]["code"] == -32601
    assert messages[1]["result"]["protocolVersion"] == "2025-11-25"
    assert len(messages[2]["result"]["tools"]) == 5 and not client.calls


def test_start_passes_only_validated_team_data_and_stdio_shutdown_does_not_cancel_owner_job():
    arguments = valid_start()
    messages, client = run([rpc("tools/call", {"name": "island_start_collaboration", "arguments": arguments}),
                            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}},
                            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "island_cancel_task", "arguments": {"job_id": JOB_ID}}}])
    assert client.calls == [("start", arguments)]
    assert client.created == [arguments]
    assert len(messages) == 2
    assert arguments["prompt"] not in json.dumps(messages, ensure_ascii=False)


@pytest.mark.parametrize("name,method", [("island_get_task", "get_task"), ("island_cancel_task", "cancel"), ("island_review_changes", "review")])
def test_job_tools_use_only_exact_job_id(name, method):
    messages, client = run([rpc("tools/call", {"name": name, "arguments": {"job_id": JOB_ID}})])
    assert client.calls == [(method, JOB_ID)]
    assert messages[-1]["result"]["isError"] is False


@pytest.mark.parametrize("changes", [
    {"cwd": "C:\\other"}, {"url": "https://outside.invalid"}, {"command": "anything"},
    {"token": "DO_NOT_ECHO"}, {"provider": "unknown"}, {"provider": True},
    {"prompt": " "}, {"prompt": "x" * 8001}, {"prompt": True}, {"prompt": "bad\0text"},
    {"model": None}, {"model": ""}, {"model": "x" * 241}, {"reasoning_effort": "high"},
    {"collaboration": {}}, {"collaboration": {"members": []}},
    {"collaboration": {"members": [{"provider": "codex"}], "max_rounds": 20}},
    {"collaboration": {"members": [{"provider": "antigravity"}]}},
    {"collaboration": {"members": [{"provider": "codex"}, {"provider": "codex", "model": "another"}]}},
    {"collaboration": {"members": [{"provider": "codex"}, {"provider": "claude"}, {"provider": "antigravity"}]}},
    {"collaboration": {"members": [{"provider": "codex", "task_mode": "write"}]}},
    {"collaboration": {"members": [{"provider": "claude", "reasoning_effort": "high"}]}},
], ids=lambda item: next(iter(item)))
def test_invalid_start_parameters_never_call_the_bridge_or_echo_input(changes):
    arguments = {**valid_start(), **changes}
    messages, client = run([rpc("tools/call", {"name": "island_start_collaboration", "arguments": arguments})])
    assert messages[-1]["error"]["code"] == -32602
    assert not client.calls and "DO_NOT_ECHO" not in json.dumps(messages)


@pytest.mark.parametrize("arguments", [[], None, {"job_id": "../../bad"}, {"job_id": "A" * 32}, {"job_id": True}, {"job_id": JOB_ID, "url": "http://outside"}])
def test_invalid_job_ids_and_extra_parameters_never_call_bridge(arguments):
    messages, client = run([rpc("tools/call", {"name": "island_cancel_task", "arguments": arguments})])
    assert messages[-1]["error"]["code"] == -32602 and not client.calls


def test_status_and_unknown_tool_inputs_are_protocol_errors_without_bridge_activity():
    requests = [rpc("tools/call", {"name": "island_status", "arguments": {"url": "http://outside"}}),
                rpc("tools/call", {"name": "island_apply_changes", "arguments": {"job_id": JOB_ID}}, 3),
                rpc("tools/call", {"name": [], "arguments": {}}, 4),
                rpc("tools/call", {"name": "island_status", "arguments": {}, "cwd": "other"}, 5)]
    messages, client = run(requests)
    assert all(item["error"]["code"] == -32602 for item in messages[1:]) and not client.calls


def test_private_transport_keys_and_secret_patterns_never_appear_in_results():
    client = Client()
    client.response = {"ready": True, "token": "BRIDGE_SECRET", "nonce": "NONCE_SECRET", "prompt": "ORIGINAL_PROMPT",
                       "privateCommand": "PRIVATE_COMMAND", "nested": {"bearer_token": "OTHER_SECRET", "env": {"KEY": "VALUE"}},
                       "message": "Bearer hidden-value sk-abcdefghijklmno ghp_12345678901234567",
                       "usage": {"input_tokens": 12}, "model_options": [{"reasoning_efforts": ["low", "high"]}]}
    messages, _ = run([rpc("tools/call", {"name": "island_status", "arguments": {}})], client)
    raw = json.dumps(messages)
    for secret in ("BRIDGE_SECRET", "NONCE_SECRET", "ORIGINAL_PROMPT", "PRIVATE_COMMAND", "OTHER_SECRET", "hidden-value", "sk-abcdefghijklmno", "ghp_12345678901234567"):
        assert secret not in raw
    result = messages[-1]["result"]["structuredContent"]
    assert result["usage"] == {"input_tokens": 12}
    assert result["model_options"][0]["reasoning_efforts"] == ["low", "high"]


@pytest.mark.parametrize("error", [mcp.ToolError("辦公室尚未就緒，請重新開啟。"), RuntimeError("DO_NOT_ECHO_PROMPT_OR_PRIVATE_COMMAND")])
def test_unavailable_bridge_is_tool_error_and_does_not_prevent_later_discovery(error):
    client = Client()
    client.error = error
    messages, _ = run([rpc("tools/call", {"name": "island_status", "arguments": {}}), rpc("tools/list", identifier=3)], client)
    assert messages[1]["result"]["isError"] is True
    assert "structuredContent" not in messages[1]["result"]
    assert "DO_NOT_ECHO" not in json.dumps(messages)
    assert len(messages[-1]["result"]["tools"]) == 5


def test_explicit_not_ready_status_is_an_error_result_without_starting_anything():
    client = Client()
    client.response = {"ready": False, "reason": "not_running"}
    messages, _ = run([rpc("tools/call", {"name": "island_status", "arguments": {}})], client)
    assert messages[-1]["result"]["isError"] is True
    assert "重新開啟" in messages[-1]["result"]["content"][0]["text"]
    assert client.calls == [("status", None)]


def test_unicode_arguments_roundtrip_as_utf8_with_no_request_echo():
    arguments = valid_start()
    arguments["prompt"] = "中文需求" * 2000
    client = Client()
    messages, client = run([rpc("tools/call", {"name": "island_start_collaboration", "arguments": arguments})], client)
    assert client.created[0]["prompt"] == arguments["prompt"]
    assert messages[-1]["result"]["structuredContent"]["message"] == "辦公室已就緒"


def test_malformed_duplicate_utf8_and_oversized_lines_recover_without_any_bridge_call():
    payloads = [b"{broken\n", b"\xff\n", b'{"jsonrpc":"2.0","id":2,"method":"tools/call","method":"ping"}\n',
                b"x" * (mcp.MAX_INPUT_BYTES + 5) + b"\n"]
    for payload in payloads:
        source = io.BytesIO(payload + json.dumps(rpc("ping")).encode() + b"\n")
        output, client = io.BytesIO(), Client()
        assert mcp._serve(source, output, client) == 0
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        assert responses[0]["error"]["code"] == -32700
        assert responses[-1]["result"] == {} and not client.calls


def test_text_stream_limit_counts_utf8_bytes_not_only_characters():
    source = io.StringIO('"' + "中" * (mcp.MAX_INPUT_BYTES // 3 + 100) + '"\n' + json.dumps(rpc("ping")) + "\n")
    output, client = io.StringIO(), Client()
    assert mcp._serve(source, output, client) == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32700 and responses[-1]["result"] == {}
    assert not client.calls


def test_oversized_diff_is_shortened_without_turning_review_into_failure():
    client = Client()
    client.response = {"diff": "中" * mcp.MAX_RESPONSE_BYTES}
    messages, _ = run([rpc("tools/call", {"name": "island_review_changes", "arguments": {"job_id": JOB_ID}})], client)
    result = messages[-1]["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["truncated"] is True
    assert "office UI" in result["structuredContent"]["notice"]
    assert len(result["structuredContent"]["diff"].encode("utf-8")) <= 16003


def test_nonfinite_office_results_are_bounded_safe_tool_errors():
    for value in ({"invalid": float("nan")},):
        client = Client()
        client.response = value
        messages, _ = run([rpc("tools/call", {"name": "island_review_changes", "arguments": {"job_id": JOB_ID}})], client)
        assert messages[-1]["result"]["isError"] is True
        assert len(messages[-1]["result"]["content"][0]["text"]) < 1000


def test_large_collections_keep_parent_child_identity_with_global_budget():
    client = Client()
    client.response = {"job": {"id": JOB_ID, "status": "running", "latest_message": "主導結論" * 2000},
                       "children": [{"id": str(index) * 32, "status": "completed", "latest_message": "子任務結論" * 1000}
                                    for index in range(1, 4)],
                       "details": [{"large": "檢查" * 10000} for _ in range(200)]}
    messages, _ = run([rpc("tools/call", {"name": "island_get_task", "arguments": {"job_id": JOB_ID}})], client)
    result = messages[-1]["result"]
    assert result["isError"] is False
    value = result["structuredContent"]
    assert value["truncated"] is True and value["job"]["id"] == JOB_ID
    assert [item["id"] for item in value["children"]] == [str(index) * 32 for index in range(1, 4)]
    assert all(item["status"] == "completed" and item["latest_message"] for item in value["children"])
    assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) <= mcp.PUBLIC_RESULT_BYTES


def test_initialization_required_invalid_ids_and_duplicate_initialization_never_call_client():
    messages, client = run([rpc("tools/list"), initialize(), initialize(),
                            rpc("tools/call", {"name": "island_status"}, identifier=True)], handshake=False)
    assert messages[0]["error"]["code"] == -32000
    assert messages[2]["error"]["code"] == -32600
    assert messages[3]["error"]["code"] == -32600 and not client.calls


def test_invalid_unicode_request_id_cannot_execute_a_mutating_tool():
    request = rpc("tools/call", {"name": "island_start_collaboration", "arguments": valid_start()}, identifier="\ud800")
    source = io.BytesIO((json.dumps(initialize()) + "\n" + json.dumps(request) + "\n").encode("utf-8"))
    output, client = io.BytesIO(), Client()
    assert mcp._serve(source, output, client) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[-1]["error"]["code"] == -32600 and not client.calls


def test_unterminated_stdio_message_cannot_start_an_office_job():
    request = rpc("tools/call", {"name": "island_start_collaboration", "arguments": valid_start()})
    source = io.BytesIO((json.dumps(initialize()) + "\n" + json.dumps(request)).encode("utf-8"))
    output, client = io.BytesIO(), Client()
    assert mcp._serve(source, output, client) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[-1]["error"]["code"] == -32700 and not client.calls


def test_invalid_unicode_tool_argument_is_rejected_before_bridge_call():
    arguments = valid_start()
    arguments["prompt"] = "\ud800"
    request = rpc("tools/call", {"name": "island_start_collaboration", "arguments": arguments})
    source = io.BytesIO((json.dumps(initialize()) + "\n" + json.dumps(request) + "\n").encode("utf-8"))
    output, client = io.BytesIO(), Client()
    assert mcp._serve(source, output, client) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[-1]["error"]["code"] == -32602 and not client.calls


def test_real_subprocess_handshake_lists_tools_without_backend_or_created_profile(tmp_path):
    profile = tmp_path / "未建立設定"
    payload = b"".join(json.dumps(item).encode() + b"\n" for item in [initialize(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, rpc("tools/list"), rpc("ping", identifier=3)])
    process = subprocess.run([sys.executable, str(ROOT / "tools" / "island_mcp.py"), "--project", str(tmp_path),
                              "--data-dir", str(profile)], input=payload, capture_output=True, cwd=ROOT,
                             timeout=10, shell=False, check=True)
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(responses) == 3 and len(responses[1]["result"]["tools"]) == 5
    assert process.stderr == b"" and list(tmp_path.iterdir()) == []


def test_invalid_fixed_project_reports_only_brief_stderr_not_stdout_json_noise(tmp_path):
    process = subprocess.run([sys.executable, str(ROOT / "tools" / "island_mcp.py"), "--project", str(tmp_path / "missing")],
                             input=b"", capture_output=True, cwd=ROOT, timeout=10, shell=False)
    assert process.returncode == 2 and process.stdout == b""
    assert b"Traceback" not in process.stderr


def test_fixed_profile_is_not_resolved_before_bridge_redirect_validation(tmp_path, monkeypatch):
    calls = []
    profile = tmp_path / "profile"
    monkeypatch.setattr(mcp, "_OfficeClient", lambda project, data_dir: calls.append((project, data_dir)) or object())
    monkeypatch.setattr(mcp, "_serve", lambda *_: 0)
    monkeypatch.setattr(Path, "resolve", lambda *_args, **_kwargs: pytest.fail("Do not hide redirects from bridge validation"))
    assert mcp.main(["--project", str(tmp_path), "--data-dir", str(profile)]) == 0
    assert calls == [(tmp_path.absolute(), profile.absolute())]
