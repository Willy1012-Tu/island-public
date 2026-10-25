"""Documented provider stream fixtures and native-process isolation checks."""

from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_providers import (
    StreamNormalizer,
    build_command,
    copy_source_workspace,
    discover_providers,
    redact,
    run_cli,
)


PRIVATE = "PRIVATE_REASONING_MUST_NEVER_APPEAR"
FAKE_KEY = "sk-proj-syntheticABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def messages(events):
    return [event for event in events if event["kind"] == "message"]


def test_codex_public_messages_tools_and_terminal_usage():
    normalizer = StreamNormalizer("codex")
    events = []
    fixtures = [
        {"type": "thread.started", "thread_id": "codex-session"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": PRIVATE}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "我會先閱讀專案。"}},
        {"type": "item.started", "item": {
            "id": "command-1", "type": "command_execution", "command": "python --version",
            "status": "in_progress", "aggregated_output": "",
        }},
        {"type": "item.completed", "item": {
            "id": "command-1", "type": "command_execution", "command": "python --version",
            "status": "completed", "aggregated_output": "Python 3.12.0", "exit_code": 0,
        }},
        {"type": "turn.completed", "usage": {"input_tokens": 40, "output_tokens": 7}},
    ]
    for fixture in fixtures:
        events.extend(normalizer.parse(fixture))
    assert any(event.get("session_id") == "codex-session" for event in events)
    assert any(event["text"] == "我會先閱讀專案。" for event in messages(events))
    tools = [event for event in events if event["kind"] == "tool"]
    assert tools[0]["status"] == "running" and tools[-1]["status"] == "completed"
    assert "Python 3.12.0" in tools[-1]["text"]
    assert events[-1]["status"] == "completed"
    assert events[-1]["usage"] == {"input_tokens": 40, "output_tokens": 7}
    assert PRIVATE not in json.dumps(events)


def test_codex_failure_and_file_change_are_public_events():
    normalizer = StreamNormalizer("codex")
    changes = normalizer.parse({"type": "item.completed", "item": {
        "type": "file_change", "changes": [{"path": "src/小島.py", "kind": "update"}],
        "status": "completed",
    }})
    assert changes[0]["kind"] == "tool" and "src/小島.py" in changes[0]["text"]
    failed = normalizer.parse({"type": "turn.failed", "error": {"message": "Authentication required"}})
    assert any(event["kind"] == "error" for event in failed)
    assert failed[-1]["kind"] == "result" and failed[-1]["status"] == "failed"


def test_claude_text_deltas_tool_results_and_public_text_only():
    normalizer = StreamNormalizer("claude")
    events = []
    fixtures = [
        {"type": "system", "subtype": "init", "session_id": "claude-session", "model": "saved-model"},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": PRIVATE},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "signature_delta", "signature": PRIVATE},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "公開進度："},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 2,
            "content_block": {"type": "tool_use", "id": "read-1", "name": "Read", "input": {}},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"file_path":'},
        }},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "thinking", "thinking": PRIVATE, "signature": PRIVATE},
            {"type": "text", "text": "正在閱讀 README。"},
            {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "README.md"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-1", "content": [{"type": "text", "text": "# 小島"}], "is_error": False},
        ]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "閱讀完成。",
         "session_id": "claude-session", "usage": {"input_tokens": 20, "output_tokens": 4}},
    ]
    for fixture in fixtures:
        events.extend(normalizer.parse(fixture))
    assert events[0]["session_id"] == "claude-session" and events[0]["model"] == "saved-model"
    delta = next(event for event in messages(events) if event["text"] == "公開進度：")
    assert delta["delta"] is True
    assert any(event["text"] == "正在閱讀 README。" for event in messages(events))
    assert any(event["kind"] == "tool" and event.get("tool") == "Read" and event.get("status") == "running" for event in events)
    assert any(event["kind"] == "tool" and event.get("tool") == "Read" and event.get("status") == "completed" and "小島" in event["text"] for event in events)
    assert events[-1]["status"] == "completed"
    assert PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("subtype", ["error_during_execution", "error_max_turns", "error_max_structured_output_retries"])
def test_claude_errors_do_not_require_a_result_string(subtype):
    events = StreamNormalizer("claude").parse({
        "type": "result", "subtype": subtype, "is_error": True,
        "errors": ["The task could not complete"], "session_id": "failure-session",
    })
    assert events[-1]["kind"] == "result" and events[-1]["status"] == "failed"
    assert "could not complete" in events[-1]["text"]


def test_claude_tool_error_and_intermediate_status_are_not_terminal_success():
    normalizer = StreamNormalizer("claude")
    normalizer.parse({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "read-failure", "name": "Read", "input": {"file_path": "missing.md"}},
    ]}})
    tool_error = normalizer.parse({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "read-failure", "content": "File not found", "is_error": True},
    ]}})
    assert tool_error[0]["kind"] == "tool" and tool_error[0]["status"] == "failed"
    assert tool_error[0]["tool"] == "Read"
    for fixture in (
        {"type": "system", "subtype": "status", "status": "compacting"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
        {"type": "tool_progress", "tool_name": "Read", "tool_use_id": "read-failure", "elapsed_time_seconds": 30},
    ):
        assert all(event["kind"] != "result" for event in normalizer.parse(fixture))


def test_antigravity_outer_event_envelopes_and_done_text_fragment():
    normalizer = StreamNormalizer("antigravity")
    fixtures = [
        {"event": "init", "conversation_id": "agy-session", "init": {"model": "saved-model", "permission_mode": "request-review"}},
        {"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "state": "ACTIVE", "text_delta": "開始閱讀"}},
        {"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "state": "DONE", "text_delta": "。"}},
        {"event": "step_update", "step_update": {"step_index": 2, "step_type": "tool", "state": "ACTIVE", "tool_name": "view_file", "tool_info": {"name": "view_file", "parameters": {"path": "README.md"}}}},
        {"event": "step_update", "step_update": {"step_index": 2, "step_type": "tool", "state": "DONE", "tool_name": "view_file", "tool_info": {"name": "view_file", "output": "# 雲上小島"}}},
        {"event": "step_update", "step_update": {"step_index": 3, "step_type": "thinking", "state": "DONE", "text_delta": PRIVATE}},
        {"event": "result", "result": {"conversation_id": "agy-session", "status": "SUCCESS", "response": "閱讀完成。", "usage": {"input_tokens": 10, "output_tokens": 2}}},
    ]
    events = [event for fixture in fixtures for event in normalizer.parse(fixture)]
    assert events[0]["session_id"] == "agy-session"
    assert "".join(event["text"] for event in messages(events)) == "開始閱讀。"
    assert all(event.get("delta") is True for event in messages(events))
    assert any(event.get("tool") == "view_file" and event.get("status") == "running" for event in events)
    assert any(event.get("tool") == "view_file" and event.get("status") == "completed" and "雲上小島" in event["text"] for event in events)
    assert events[-1]["status"] == "completed" and events[-1]["usage"]["output_tokens"] == 2
    assert PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("provider_status,expected", [
    ("ERROR", "failed"), ("INVALID", "failed"), ("CANCELED", "cancelled"), ("INTERRUPTED", "cancelled"),
])
def test_antigravity_terminal_statuses(provider_status, expected):
    events = StreamNormalizer("antigravity").parse({
        "event": "result", "result": {"status": provider_status, "response": "", "error": "service interrupted"},
    })
    assert events[-1]["kind"] == "result" and events[-1]["status"] == expected


@pytest.mark.parametrize("provider_status", ["RUNNING", "WAITING"])
def test_antigravity_waiting_is_not_completion(provider_status):
    events = StreamNormalizer("antigravity").parse({"event": "result", "result": {"status": provider_status}})
    assert all(event["kind"] != "result" for event in events)


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_unknown_formats_and_private_only_payloads_are_ignored(provider):
    normalizer = StreamNormalizer(provider)
    for payload in (None, [], "not JSON", {}, {"type": "future.event", "event": "future.event", "text": PRIVATE}):
        assert normalizer.parse(payload) == []


@pytest.mark.parametrize("provider,payload", [
    ("claude", {"type": "stream_event", "event": None}),
    ("claude", {"type": "assistant", "message": []}),
    ("antigravity", {"event": "step_update", "step_update": None}),
    ("antigravity", {"event": "init", "init": []}),
])
def test_malformed_nested_envelopes_do_not_crash_normalization(provider, payload):
    assert StreamNormalizer(provider).parse(payload) == []


@pytest.mark.parametrize("text,secret", [
    (FAKE_KEY, FAKE_KEY),
    ("sk-ant-api03-syntheticABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "sk-ant-api03-syntheticABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
    ("Authorization: Bearer synthetic-access-token", "synthetic-access-token"),
    ("OPENAI_API_KEY=synthetic-key-value", "synthetic-key-value"),
    ('{"password":"synthetic-password-value"}', "synthetic-password-value"),
    ('{"api_key": "synthetic-api-value"}', "synthetic-api-value"),
    ("access_token='synthetic-token-value'", "synthetic-token-value"),
])
def test_credentials_are_redacted_in_plain_and_serialized_tool_inputs(text, secret):
    cleaned = redact(text)
    assert secret not in cleaned
    assert "REDACTED" in cleaned


def test_redaction_strips_terminal_control_bytes_and_bounds_messages():
    cleaned = redact("\x1b[31m公開\x1b[0m\x00\x07\r\n下一行")
    assert "公開" in cleaned and "下一行" in cleaned
    assert "\x1b" not in cleaned and "\x00" not in cleaned and "\x07" not in cleaned
    assert "concealed-title" not in redact("\x1b]0;concealed-title\x07公開")
    assert len(redact("x" * 100_000)) <= 8000


def test_normalized_tool_inputs_cannot_publish_quoted_credentials():
    events = StreamNormalizer("claude").parse({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "secret-input", "name": "Read", "input": {"password": "synthetic-password-value", "api_key": FAKE_KEY}},
    ]}})
    serialized = json.dumps(events)
    assert "synthetic-password-value" not in serialized and FAKE_KEY not in serialized


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_native_command_retains_unicode_paths_and_provider_restrictions(provider, tmp_path):
    executable = str(tmp_path / "工具 bin" / (provider + ".exe"))
    workspace = tmp_path / "小島 workspace $(literal)"
    argv = build_command(provider, executable, workspace, model="saved model", timeout=17)
    assert argv[0] == executable and all(isinstance(value, str) for value in argv)
    assert argv[argv.index("--model") + 1] == "saved model"
    assert not any("dangerously" in value for value in argv)
    if provider == "codex":
        assert "--json" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
        assert argv[argv.index("--cd") + 1] == str(workspace)
        assert argv[-1] == "-"
    elif provider == "claude":
        assert "--bare" not in argv and "--safe-mode" in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in argv and "--include-partial-messages" in argv
        assert set(argv[argv.index("--tools") + 1].split(",")) == {"Read", "Grep", "Glob"}
        assert argv[argv.index("--permission-mode") + 1] == "plan"
        assert "--strict-mcp-config" in argv
        assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    else:
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--mode=plan" in argv and "--sandbox" in argv
        assert "-p" not in argv and "--print" not in argv


def test_discovery_reports_installation_without_claiming_authentication():
    native = {"custom-codex": "C:/tools/codex.exe", "custom-claude": "C:/tools/claude.exe"}
    with patch("cli_providers.shutil.which", side_effect=lambda name: native.get(name)), \
            patch("cli_providers._candidate_paths", return_value=[]), \
            patch("cli_providers.subprocess.Popen") as popen:
        providers = discover_providers({"codex": "custom-codex", "claude": "custom-claude", "antigravity": "absent-agy"})
    by_id = {provider["id"]: provider for provider in providers}
    assert set(by_id) == {"codex", "claude", "antigravity"}
    assert by_id["codex"]["available"] and by_id["claude"]["available"]
    assert not by_id["antigravity"]["available"]
    assert all(provider["auth"] == "unknown" and provider["reason"] for provider in providers)
    assert by_id["codex"]["execution_mode"] == "read_only"
    assert by_id["claude"]["execution_mode"] == by_id["antigravity"]["execution_mode"] == "isolated"
    assert by_id["claude"]["transport"] == "antigravity"
    assert by_id["claude"]["configured_model"] == "claude-sonnet-4-6"
    popen.assert_not_called()


def test_discovery_uses_advertised_default_when_no_saved_codex_model():
    options = [{"id": "first", "name": "First", "reasoning_efforts": ["low"], "default_reasoning_effort": "low"},
               {"id": "default", "name": "Default", "is_default": True,
                "reasoning_efforts": ["medium"], "default_reasoning_effort": "medium"}]
    with patch("cli_providers.shutil.which", return_value="C:/tools/codex.exe"), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.saved_reasoning_effort", return_value=None), \
            patch("cli_providers.discover_models", return_value=options):
        codex = next(item for item in discover_providers() if item["id"] == "codex")
    assert codex["configured_model"] == "default"
    assert codex["configured_reasoning_effort"] == "medium"


def test_claude_office_is_available_from_agy_without_any_claude_code_binary():
    with patch("cli_providers.shutil.which", side_effect=lambda name: "C:/tools/agy.exe" if name == "agy" else None), \
            patch("cli_providers._candidate_paths", return_value=[]), \
            patch("cli_providers._saved_model", return_value=None):
        providers = {provider["id"]: provider for provider in discover_providers()}
    claude = providers["claude"]
    assert claude["available"] and claude["command"] == "C:/tools/agy.exe"
    assert claude["name"] == "Claude 4.6" and claude["transport"] == "antigravity"
    assert claude["configured_model"] == "claude-sonnet-4-6"
    assert {option["id"] for option in claude["model_options"]} == {
        "claude-sonnet-4-6", "claude-opus-4-6-thinking",
    }
    assert providers["antigravity"]["configured_model"] == "gemini-3.8-flash-high"
    assert claude["execution_mode"] == "isolated" and claude["auth"] == "unknown"


def test_both_agy_offices_share_an_explicit_antigravity_executable_override():
    with patch("cli_providers.shutil.which", side_effect=lambda name: "C:/custom/agy.exe" if name == "custom-agy" else None), \
            patch("cli_providers._candidate_paths", return_value=[]), \
            patch("cli_providers._saved_model", return_value=None):
        providers = {provider["id"]: provider for provider in discover_providers({"antigravity": "custom-agy"})}
    assert providers["claude"]["command"] == providers["antigravity"]["command"] == "C:/custom/agy.exe"


@pytest.mark.skipif(os.name != "nt", reason="Windows native argv safety")
def test_windows_shell_wrappers_are_not_used_as_provider_executables():
    with patch("cli_providers.shutil.which", return_value="C:/tools/claude.cmd"), \
            patch("cli_providers._candidate_paths", return_value=[]):
        assert not any(provider["available"] for provider in discover_providers())


def test_isolated_copy_keeps_source_unchanged_and_excludes_sensitive_generated_files(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "isolated"
    safe = {"main.py": "print('小島')\n", "src/app.js": "export const island = 20;\n", "README.md": "# 小島\n", "package.json": "{}"}
    excluded = [".env", ".env.local", ".git/config", ".hidden/data.py", ".venv/lib/a.py",
                "node_modules/module.js", "artifacts/result.json", "credentials.json", "auth.json",
                "secrets.yaml", "token.txt", "passwords.txt", "private_key.txt", "assets/photo.png", "config/office-bridge.json"]
    for name, text in {**safe, **{name: "synthetic secret" for name in excluded}}.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    copied = copy_source_workspace(source, destination)
    assert copied == len(safe)
    for name, text in safe.items():
        assert (destination / name).read_text(encoding="utf-8") == text
    assert all(not (destination / name).exists() for name in excluded)
    (destination / "main.py").write_text("changed only in the isolated copy", encoding="utf-8")
    after = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    assert after == before


def test_source_copy_bounds_individual_and_total_bytes(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "isolated"
    source.mkdir()
    (source / "oversized.py").write_bytes(b"x" * (1024 * 1024 + 1))
    for index in range(22):
        (source / f"module_{index:02}.py").write_bytes(b"x" * (1024 * 1024))
    copied = copy_source_workspace(source, destination)
    files = [path for path in destination.rglob("*") if path.is_file()]
    assert not (destination / "oversized.py").exists()
    assert copied == len(files) and copied > 0
    assert all(path.stat().st_size <= 1024 * 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 20 * 1024 * 1024


def test_source_copy_never_follows_file_or_directory_symlinks(tmp_path):
    source, destination, outside = tmp_path / "source", tmp_path / "isolated", tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "external.py").write_text("must never copy outside source", encoding="utf-8")
    (source / "main.py").write_text("safe", encoding="utf-8")
    try:
        (source / "linked.py").symlink_to(outside / "external.py")
        (source / "linked_dir").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating test symlinks is not available on this Windows account")
    assert copy_source_workspace(source, destination) == 1
    assert (destination / "main.py").is_file()
    assert not (destination / "linked.py").exists() and not (destination / "linked_dir").exists()


def test_source_copy_prunes_junction_directories_and_rejects_junction_files(monkeypatch, tmp_path):
    source, destination = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    (source / "main.py").write_text("safe source", encoding="utf-8")
    redirected = source / "junction-dir"
    redirected.mkdir()
    (redirected / "external.py").write_text("junction content must not be walked", encoding="utf-8")
    (source / "junction-file.py").write_text("junction file must not be copied", encoding="utf-8")
    original_junction = getattr(Path, "is_junction", None)

    def is_junction(path):
        if path in (redirected, source / "junction-file.py"):
            return True
        return original_junction(path) if original_junction is not None else False

    monkeypatch.setattr(Path, "is_junction", is_junction, raising=False)
    assert copy_source_workspace(source, destination) == 1
    assert (destination / "main.py").read_text(encoding="utf-8") == "safe source"
    assert not (destination / "junction-dir").exists()
    assert not (destination / "junction-file.py").exists()


def test_source_copy_checks_resolved_containment_even_without_link_metadata(monkeypatch, tmp_path):
    source, destination, outside = tmp_path / "source", tmp_path / "copy", tmp_path / "outside"
    source.mkdir(); outside.mkdir()
    (source / "main.py").write_text("safe source", encoding="utf-8")
    redirected = source / "redirected"
    redirected.mkdir()
    (redirected / "escaped.py").write_text("must not traverse", encoding="utf-8")
    redirected_file = source / "redirected.py"
    redirected_file.write_text("must not copy", encoding="utf-8")
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == redirected:
            return outside
        if path == redirected_file:
            return outside / "external.py"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert copy_source_workspace(source, destination) == 1
    assert (destination / "main.py").is_file()
    assert not (destination / "redirected").exists()
    assert not (destination / "redirected.py").exists()


def test_source_copy_rechecks_current_walk_directory_before_reading_files(monkeypatch, tmp_path):
    source, destination, outside = tmp_path / "source", tmp_path / "copy", tmp_path / "outside"
    source.mkdir(); outside.mkdir()
    (source / "main.py").write_text("safe", encoding="utf-8")
    (outside / "external.py").write_text("outside content", encoding="utf-8")
    monkeypatch.setattr("cli_providers.os.walk", lambda *args, **kwargs: iter([
        (str(source), [], ["main.py"]), (str(outside), [], ["external.py"]),
    ]))
    assert copy_source_workspace(source, destination) == 1
    assert (destination / "main.py").is_file()
    assert not (destination / "external.py").exists()


class RecordingInput:
    def __init__(self):
        self.text = ""
        self.closed = threading.Event()

    def write(self, text):
        self.text += text

    def close(self):
        self.closed.set()


class BlockedOutput:
    def __init__(self, released):
        self.released = released

    def readline(self, size=-1):
        self.released.wait(3)
        return ""

    def close(self):
        self.released.set()


class FakeCLIProcess:
    def __init__(self, payloads=(), *, exit_code=0, diagnostic="", blocked=False):
        self.pid = 987654
        self.stdin = RecordingInput()
        self.released = threading.Event()
        self.stdout = BlockedOutput(self.released) if blocked else io.StringIO("".join(json.dumps(payload, ensure_ascii=False) + "\n" for payload in payloads))
        self.stderr = BlockedOutput(self.released) if blocked else io.StringIO(diagnostic)
        self.returncode = None if blocked else exit_code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.released.set()


def available_providers(*args, **kwargs):
    return [{"id": provider, "available": True, "command": f"C:/測試 tools/{provider}.exe", "reason": "test installed"}
            for provider in ("codex", "claude", "antigravity")]


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_run_cli_uses_native_utf8_stdin_and_requires_real_terminal_result(provider, tmp_path):
    fixtures = {
        "codex": [{"type": "thread.started", "thread_id": "run-session"}, {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}}],
        "claude": [{"event": "init", "conversation_id": "run-session", "init": {"model": "reported-model"}},
                   {"event": "result", "result": {"status": "SUCCESS", "response": "完成", "usage": {"input_tokens": 2, "output_tokens": 1}}}],
        "antigravity": [{"event": "init", "conversation_id": "run-session", "init": {"model": "reported-model"}},
                        {"event": "result", "result": {"status": "SUCCESS", "response": "完成", "usage": {"input_tokens": 2, "output_tokens": 1}}}],
    }
    proc = FakeCLIProcess(fixtures[provider])
    events = []
    prompt = "請閱讀小島；$(literal) `literal` & literal\n第二行"
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", return_value=proc) as popen:
        result = run_cli(provider, prompt, tmp_path, events.append, threading.Event(), timeout=2)
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert result["session_id"] == "run-session"
    assert result["usage"] == {"input_tokens": 2, "output_tokens": 1}
    if provider != "codex":
        assert result["model"] == "reported-model"
    argv, options = popen.call_args.args[0], popen.call_args.kwargs
    assert options["shell"] is False and options["encoding"] == "utf-8"
    assert options["stdin"] is not None and options["cwd"]
    assert prompt not in argv
    assert proc.stdin.closed.wait(1)
    if provider in ("claude", "antigravity"):
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        assert "--safe-mode" not in argv and "--tools" not in argv
        assert result["transport"] == "antigravity"
        assert json.loads(proc.stdin.text) == {"event": "user", "message": {"content": prompt}}
        assert Path(options["cwd"]) != tmp_path.resolve()
        assert not Path(options["cwd"]).exists(), "The isolated run folder must be removed"
    else:
        assert proc.stdin.text.rstrip("\n") == prompt
        assert Path(options["cwd"]) == tmp_path.resolve()


def test_claude_office_runs_the_selected_opus_model_with_agy_protocol(tmp_path):
    proc = FakeCLIProcess([{"event": "result", "result": {"status": "SUCCESS", "response": "Claude office ready"}}])
    discovered = [{"id": "claude", "available": True, "command": "C:/tools/agy.exe", "transport": "antigravity",
                   "configured_model": "claude-sonnet-4-6", "model_options": [
                       {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6"},
                       {"id": "claude-opus-4-6-thinking", "name": "Opus 4.6"}]}]
    with patch("cli_providers.discover_providers", return_value=discovered), \
            patch("cli_providers.subprocess.Popen", return_value=proc) as popen:
        result = run_cli("claude", "只回覆連線正常", tmp_path, lambda event: None, threading.Event(),
                         model="claude-opus-4-6-thinking", timeout=2)
    argv = popen.call_args.args[0]
    assert result["status"] == "completed" and result["model"] == "claude-opus-4-6-thinking"
    assert argv[0] == "C:/tools/agy.exe"
    assert argv[argv.index("--model") + 1] == "claude-opus-4-6-thinking"
    assert "--input-format" in argv and "--safe-mode" not in argv


@pytest.mark.parametrize("model,effort,expected", [
    ("model-a", "high", "high"), ("model-a", None, "ultra"),
    ("model-b", None, "medium"), ("model-b", "low", "low"),
])
def test_codex_runs_selected_model_and_supported_effort(tmp_path, model, effort, expected):
    proc = FakeCLIProcess([{"type": "turn.completed"}])
    metadata = [{"id": "codex", "available": True, "command": "C:/tools/codex.exe",
                 "configured_model": "model-a", "configured_reasoning_effort": "ultra",
                 "model_options": [
                     {"id": "model-a", "reasoning_efforts": ["low", "high", "ultra"], "default_reasoning_effort": "low"},
                     {"id": "model-b", "reasoning_efforts": ["low", "medium"], "default_reasoning_effort": "medium"}]}]
    with patch("cli_providers.discover_providers", return_value=metadata), \
            patch("cli_providers.subprocess.Popen", return_value=proc) as popen:
        result = run_cli("codex", "只回覆連線正常", tmp_path, lambda event: None, threading.Event(),
                         model=model, reasoning_effort=effort, timeout=2)
    argv = popen.call_args.args[0]
    assert result["status"] == "completed"
    assert result["model"] == model and result["reasoning_effort"] == expected
    assert argv[argv.index("--model") + 1] == model
    assert [value for value in argv if value.startswith("model_reasoning_effort=")] == [f'model_reasoning_effort="{expected}"']
    assert argv[argv.index("--sandbox") + 1] == "read-only"


@pytest.mark.parametrize("model,effort", [("unlisted", "low"), ("model-a", "ultra"), ("model-a", []),
                                        ("model-a", 'low" sandbox_mode="danger-full-access')])
def test_codex_rejects_unlisted_model_effort_before_spawn(tmp_path, model, effort):
    metadata = [{"id": "codex", "available": True, "command": "codex.exe", "configured_model": "model-a",
                 "model_options": [{"id": "model-a", "reasoning_efforts": ["low"], "default_reasoning_effort": "low"}]}]
    with patch("cli_providers.discover_providers", return_value=metadata), \
            patch("cli_providers.subprocess.Popen") as popen, pytest.raises(ValueError):
        run_cli("codex", "連線正常", tmp_path, lambda event: None, threading.Event(), model=model, reasoning_effort=effort)
    popen.assert_not_called()


def test_codex_does_not_force_low_when_no_effort_is_available(tmp_path):
    argv = build_command("codex", "codex.exe", tmp_path, model="model-a")
    assert not any(value.startswith("model_reasoning_effort=") for value in argv)
    with pytest.raises(ValueError):
        build_command("antigravity", "agy.exe", tmp_path, reasoning_effort="high")


def test_antigravity_can_modify_only_its_copied_workspace(tmp_path):
    project = tmp_path / "sibling-project"
    project.mkdir()
    (project / "main.py").write_text("original project", encoding="utf-8")
    (project / ".env").write_text("synthetic secret", encoding="utf-8")
    proc = FakeCLIProcess([{"event": "result", "result": {"status": "SUCCESS", "response": "done"}}])

    def spawn(argv, **options):
        isolated = Path(options["cwd"])
        assert isolated != project.resolve()
        assert (isolated / "main.py").read_text(encoding="utf-8") == "original project"
        assert not (isolated / ".env").exists()
        (isolated / "main.py").write_text("CLI touched only this copy", encoding="utf-8")
        return proc

    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers.subprocess.Popen", side_effect=spawn):
        result = run_cli("antigravity", "Read the project", project, lambda event: None, threading.Event(), timeout=2)
    assert result["status"] == "completed"
    assert (project / "main.py").read_text(encoding="utf-8") == "original project"
    assert (project / ".env").read_text(encoding="utf-8") == "synthetic secret"


@pytest.mark.parametrize("exit_code", [0, 3])
def test_run_cli_does_not_treat_missing_final_event_as_success(exit_code, tmp_path):
    proc = FakeCLIProcess(exit_code=exit_code, diagnostic=f"Authentication failed: {FAKE_KEY}\n")
    events = []
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", return_value=proc):
        result = run_cli("codex", "Read", tmp_path, events.append, threading.Event(), timeout=2)
    assert result["status"] == "failed" and result["error"]
    assert FAKE_KEY not in json.dumps({"result": result, "events": events})


@pytest.mark.parametrize("payload,exit_code", [
    ({"type": "turn.failed", "error": {"message": "The provider refused the task"}}, 0),
    ({"type": "turn.completed", "usage": {}}, 3),
])
def test_terminal_provider_failure_or_nonzero_exit_prevents_completed_status(payload, exit_code, tmp_path):
    proc = FakeCLIProcess([payload], exit_code=exit_code)
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", return_value=proc):
        result = run_cli("codex", "Read", tmp_path, lambda event: None, threading.Event(), timeout=2)
    assert result["status"] == "failed" and result["error"]


def test_run_cli_ignores_non_protocol_stdout_and_filters_usage_metadata(tmp_path):
    proc = FakeCLIProcess()
    final = {"type": "turn.completed", "usage": {
        "input_tokens": 2, "output_tokens": 1, "unknown_text": PRIVATE, "unknown_bool": True,
    }}
    proc.stdout = io.StringIO("diagnostic banner\n{invalid JSON\n" + json.dumps(final) + "\n")
    events = []
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", return_value=proc):
        result = run_cli("codex", "Read", tmp_path, events.append, threading.Event(), timeout=2)
    assert result["status"] == "completed"
    assert result["usage"] == {"input_tokens": 2, "output_tokens": 1}
    assert PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("mode,expected", [("timeout", "timed_out"), ("cancel", "cancelled")])
def test_run_cli_timeout_and_cancel_stop_only_its_owned_process(mode, expected, tmp_path):
    proc = FakeCLIProcess(blocked=True)
    cancel = threading.Event()
    events = []

    def emit(event):
        events.append(event)
        if mode == "cancel" and "CLI 程序已啟動" in event["text"]:
            cancel.set()

    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", return_value=proc), \
            patch("cli_providers.AgentBrain._stop_process", side_effect=lambda owned: owned.kill()) as stop:
        result = run_cli("codex", "Read", tmp_path, emit, cancel, timeout=0.03 if mode == "timeout" else 2)
    assert result["status"] == expected
    stop.assert_called_with(proc)
    assert proc.released.is_set()


def test_pre_cancelled_and_unavailable_tasks_never_spawn_a_process(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers.subprocess.Popen") as popen:
        assert run_cli("codex", "Read", tmp_path, lambda event: None, cancel)["status"] == "cancelled"
        popen.assert_not_called()
    with patch("cli_providers.discover_providers", return_value=[{"id": "claude", "available": False, "reason": "CLI not installed"}]), \
            patch("cli_providers.subprocess.Popen") as popen:
        result = run_cli("claude", "Read", tmp_path, lambda event: None, threading.Event())
        assert result["status"] == "failed" and "not installed" in result["error"]
        popen.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_cli_timeout_never_spawns_a_process(timeout, tmp_path):
    with patch("cli_providers.subprocess.Popen") as popen, pytest.raises(ValueError):
        run_cli("codex", "Read", tmp_path, lambda event: None, threading.Event(), timeout=timeout)
    popen.assert_not_called()


LEAD_SCHEMA = {
    "type": "object", "properties": {"action": {"type": "string"}, "task": {"type": "string"}},
    "required": ["action", "task"], "additionalProperties": False,
}


@pytest.mark.parametrize("transport", ["codex", "antigravity"])
def test_collaboration_write_mode_is_explicit_and_keeps_provider_sandbox(transport, tmp_path):
    argv = build_command(transport, "native.exe", tmp_path, execution_mode="workspace_write", shared_workspace=True)
    assert not any("dangerously" in value for value in argv)
    if transport == "codex":
        assert argv[argv.index("--sandbox") + 1] == "workspace-write"
        assert argv[argv.index("--ask-for-approval") + 1] == "never"
        disabled = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--disable"]
        assert {"plugins", "apps", "hooks", "multi_agent"} <= set(disabled)
    else:
        assert "--mode=accept-edits" in argv and "--mode=plan" not in argv
        assert "--sandbox" in argv


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
@pytest.mark.parametrize("shared", [False, None, "true", 1])
def test_writing_without_explicit_host_shared_workspace_is_rejected_before_discovery(provider, shared, tmp_path):
    with patch("cli_providers.discover_providers") as discover, patch("cli_providers.subprocess.Popen") as popen:
        with pytest.raises(ValueError):
            run_cli(provider, "Change code", tmp_path, lambda event: None, threading.Event(),
                    execution_mode="workspace_write", shared_workspace=shared)
        with pytest.raises(ValueError):
            build_command(provider, "native.exe", tmp_path, execution_mode="workspace_write", shared_workspace=shared)
    discover.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("mode", ["danger-full-access", "isolated", "accept-edits", [], None])
def test_unknown_execution_mode_is_not_forwarded_to_a_provider(mode, tmp_path):
    with patch("cli_providers.subprocess.Popen") as popen, pytest.raises(ValueError):
        run_cli("codex", "Change code", tmp_path, lambda event: None, threading.Event(),
                execution_mode=mode, shared_workspace=True)
    popen.assert_not_called()


def test_schema_argument_builder_does_not_leak_unmanaged_codex_files(tmp_path):
    with pytest.raises(ValueError, match="managed schema file"):
        build_command("codex", "codex.exe", tmp_path, output_schema=LEAD_SCHEMA)
    with pytest.raises(ValueError, match="requires output_schema"):
        build_command("codex", "codex.exe", tmp_path, output_schema_path=tmp_path / "unused.json")
    schema = {**LEAD_SCHEMA, "description": "Unicode 小島 ' $(literal) `literal` & literal"}
    argv = build_command("antigravity", "agy.exe", tmp_path, output_schema=schema)
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema
    assert "--mode=plan" in argv


@pytest.mark.parametrize("schema", [[], "not a schema", {"not-finite": float("nan")}, {"invalid": object()}, {"description": "x" * 65536}])
def test_invalid_or_oversized_schema_fails_before_native_process(schema, tmp_path):
    with patch("cli_providers.discover_providers") as discover, patch("cli_providers.subprocess.Popen") as popen:
        with pytest.raises(ValueError):
            run_cli("codex", "Plan a handoff", tmp_path, lambda event: None, threading.Event(), output_schema=schema)
    discover.assert_not_called()
    popen.assert_not_called()


def test_codex_writer_schema_exists_for_owned_process_and_is_cleaned_afterwards(tmp_path):
    original = tmp_path / "original"
    shared = tmp_path / "host-isolated"
    original.mkdir(); shared.mkdir()
    (original / "main.py").write_text("original", encoding="utf-8")
    (shared / "main.py").write_text("original", encoding="utf-8")
    public = {"action": "done", "task": "已更新隔離副本"}
    proc = FakeCLIProcess([
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(public, ensure_ascii=False)}},
        {"type": "turn.completed"},
    ])
    schema_paths = []

    def spawn(argv, **options):
        assert options["cwd"] == shared.resolve()
        assert argv[argv.index("--sandbox") + 1] == "workspace-write"
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema_paths.append(schema_path)
        assert schema_path.is_file() and json.loads(schema_path.read_text(encoding="utf-8")) == LEAD_SCHEMA
        assert not schema_path.is_relative_to(shared), "host schema metadata must not enter the reviewable project copy"
        (shared / "main.py").write_text("worker change", encoding="utf-8")
        return proc

    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", side_effect=spawn):
        result = run_cli("codex", "Update the isolated project", shared, lambda event: None, threading.Event(),
                         shared_workspace=True, execution_mode="workspace_write", output_schema=LEAD_SCHEMA, timeout=2)
    assert result["status"] == "completed" and json.loads(result["output"]) == public
    assert result["output_truncated"] is False
    assert not schema_paths[0].exists()
    assert (original / "main.py").read_text(encoding="utf-8") == "original"
    assert (shared / "main.py").read_text(encoding="utf-8") == "worker change", "team copy must survive for later review"


@pytest.mark.parametrize("provider", ["claude", "antigravity"])
def test_shared_agy_writer_preserves_host_copy_without_per_turn_recopy(provider, tmp_path):
    (tmp_path / "worker.py").write_text("previous team turn", encoding="utf-8")
    structured = {"action": "delegate", "task": "請 Codex 修改這個隔離副本。"}
    proc = FakeCLIProcess([{"event": "result", "result": {
        "status": "SUCCESS", "response": "ignored plain response", "structured_output": structured,
    }}])

    def spawn(argv, **options):
        assert options["cwd"] == tmp_path.resolve()
        assert "--mode=accept-edits" in argv and "--sandbox" in argv
        assert json.loads(argv[argv.index("--json-schema") + 1]) == LEAD_SCHEMA
        assert (tmp_path / "worker.py").read_text(encoding="utf-8") == "previous team turn"
        (tmp_path / "worker.py").write_text("next team turn", encoding="utf-8")
        return proc

    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers.copy_source_workspace") as copy, \
            patch("cli_providers.subprocess.Popen", side_effect=spawn):
        result = run_cli(provider, "Update shared copy", tmp_path, lambda event: None, threading.Event(),
                         execution_mode="workspace_write", shared_workspace=True,
                         output_schema=LEAD_SCHEMA, timeout=2)
    copy.assert_not_called()
    assert result["transport"] == "antigravity" and result["status"] == "completed"
    assert json.loads(result["output"]) == structured
    assert (tmp_path / "worker.py").read_text(encoding="utf-8") == "next team turn"


def test_shared_agy_reader_uses_same_copy_but_retains_plan_mode(tmp_path):
    proc = FakeCLIProcess([{"event": "result", "result": {"status": "SUCCESS", "response": "Reviewed"}}])
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers.copy_source_workspace") as copy, \
            patch("cli_providers.subprocess.Popen", return_value=proc) as popen:
        result = run_cli("antigravity", "Read previous turn", tmp_path, lambda event: None, threading.Event(),
                         shared_workspace=True, timeout=2)
    copy.assert_not_called()
    assert popen.call_args.kwargs["cwd"] == tmp_path.resolve()
    assert "--mode=plan" in popen.call_args.args[0]
    assert result["output"] == "Reviewed"


def test_large_agy_schema_uses_managed_file_and_does_not_hit_windows_argv_limit(tmp_path):
    schema = {**LEAD_SCHEMA, "description": "x" * 10000}
    proc = FakeCLIProcess([{"event": "result", "result": {"status": "SUCCESS", "structured_output": {"action": "done", "task": "ok"}}}])
    paths = []

    def spawn(argv, **options):
        path = Path(argv[argv.index("--json-schema") + 1])
        paths.append(path)
        assert path.is_file() and json.loads(path.read_text(encoding="utf-8")) == schema
        return proc

    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers.subprocess.Popen", side_effect=spawn):
        result = run_cli("antigravity", "Plan", tmp_path, lambda event: None, threading.Event(),
                         shared_workspace=True, output_schema=schema, timeout=2)
    assert result["status"] == "completed" and not paths[0].exists()


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_internal_final_output_is_complete_public_text_while_ui_events_stay_redacted(provider, tmp_path):
    final_text = "Public final result: " + "x" * 12000 + " 完整尾端 " + FAKE_KEY
    if provider == "codex":
        payloads = [
            {"type": "item.completed", "item": {"type": "reasoning", "text": PRIVATE}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "earlier public commentary"}},
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "TOOL_OUTPUT_EXCLUDED", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final_text}},
            {"type": "turn.completed"},
        ]
    else:
        payloads = [
            {"event": "step_update", "step_update": {"step_type": "thinking", "text_delta": PRIVATE}},
            {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_info": {"output": "TOOL_OUTPUT_EXCLUDED"}}},
            {"event": "step_update", "step_update": {"step_type": "agent_response", "step_index": 1, "text_delta": "earlier public fragment"}},
            {"event": "result", "result": {"status": "SUCCESS", "response": final_text}},
        ]
    proc = FakeCLIProcess(payloads)
    events = []
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", return_value=proc):
        result = run_cli(provider, "Read", tmp_path, events.append, threading.Event(), timeout=2)
    assert result["status"] == "completed" and result["output"] == final_text
    assert result["output_truncated"] is False
    assert PRIVATE not in result["output"] and "TOOL_OUTPUT_EXCLUDED" not in result["output"]
    assert FAKE_KEY not in json.dumps(events) and PRIVATE not in json.dumps(events)
    assert all(len(event["text"]) <= 8000 for event in events)


@pytest.mark.parametrize("transport,envelope", [
    ("antigravity", {"event": "result", "result": {"status": "SUCCESS", "response": {"action": "delegate", "text": "retain this field"}}}),
    ("claude", {"type": "result", "subtype": "success", "result": {"action": "delegate", "text": "retain this field"}}),
])
def test_public_response_objects_are_json_even_when_the_schema_has_a_text_field(transport, envelope):
    normalizer = StreamNormalizer(transport)
    events = normalizer.parse(envelope)
    assert json.loads(normalizer.output) == {"action": "delegate", "text": "retain this field"}
    assert events[-1]["public_output"] is True


def test_legacy_claude_capture_excludes_private_blocks_and_prefers_structured_final_object():
    normalizer = StreamNormalizer("claude")
    normalizer.parse({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": PRIVATE}, {"type": "text", "text": "public analysis"},
        {"type": "tool_use", "name": "Read", "input": {"marker": "TOOL_OUTPUT_EXCLUDED"}},
    ]}})
    assert normalizer.output == "public analysis"
    final = {"action": "delegate", "task": "ask Codex"}
    normalizer.parse({"type": "result", "subtype": "success", "structured_output": final})
    assert json.loads(normalizer.output) == final
    assert PRIVATE not in normalizer.output


def test_oversized_public_output_is_utf8_bounded_and_flagged_for_host_rejection(tmp_path):
    final_text = "😀" * 10000
    proc = FakeCLIProcess([
        {"type": "item.completed", "item": {"type": "agent_message", "text": final_text}},
        {"type": "turn.completed"},
    ])
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", return_value=proc):
        result = run_cli("codex", "Read", tmp_path, lambda event: None, threading.Event(), timeout=2)
    assert result["output_truncated"] is True
    assert len(result["output"].encode("utf-8")) <= 32768
    assert final_text.startswith(result["output"])
    assert "\ufffd" not in result["output"]


def test_truncated_fragment_capture_stays_a_prefix_then_a_small_final_can_replace_it():
    normalizer = StreamNormalizer("antigravity")
    normalizer.parse({"event": "step_update", "step_update": {"step_type": "agent_response", "step_index": 1, "text_delta": "x" * 40000}})
    prefix = normalizer.output
    normalizer.parse({"event": "step_update", "step_update": {"step_type": "agent_response", "step_index": 1, "text_delta": "must not alter an already truncated prefix"}})
    assert normalizer.output == prefix and normalizer.output_truncated is True
    normalizer.parse({"event": "result", "result": {"status": "SUCCESS", "response": "small final"}})
    assert normalizer.output == "small final" and normalizer.output_truncated is False


def test_cancelled_schema_process_keeps_schema_until_stop_then_cleans_it(tmp_path):
    proc = FakeCLIProcess(blocked=True)
    cancel = threading.Event()
    paths = []

    def spawn(argv, **options):
        path = Path(argv[argv.index("--output-schema") + 1])
        paths.append(path)
        assert path.is_file()
        return proc

    def emit(event):
        if "CLI 程序已啟動" in event["text"]:
            cancel.set()

    def stop(owned):
        assert owned is proc and paths[0].is_file()
        proc.kill()

    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", side_effect=spawn), \
            patch("cli_providers.AgentBrain._stop_process", side_effect=stop):
        result = run_cli("codex", "Plan", tmp_path, emit, cancel, shared_workspace=True,
                         output_schema=LEAD_SCHEMA, timeout=2)
    assert result["status"] == "cancelled" and not paths[0].exists()


def test_schema_tempfile_is_cleaned_if_native_process_cannot_start(tmp_path):
    paths = []

    def spawn(argv, **options):
        path = Path(argv[argv.index("--output-schema") + 1])
        paths.append(path)
        assert path.is_file()
        raise OSError("fixture launch failure")

    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", side_effect=spawn):
        result = run_cli("codex", "Plan", tmp_path, lambda event: None, threading.Event(),
                         shared_workspace=True, output_schema=LEAD_SCHEMA, timeout=2)
    assert result["status"] == "failed" and not paths[0].exists()


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_explicit_prompt_schema_compatibility_omits_native_schema_without_changing_permissions(provider, tmp_path):
    final = {"action": "delegate", "task": "review the isolated copy"}
    payloads = ([{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(final)}},
                 {"type": "turn.completed"}] if provider == "codex" else [
                     {"event": "result", "result": {"status": "SUCCESS", "response": json.dumps(final)}},
                 ])
    proc = FakeCLIProcess(payloads)
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers._saved_model", return_value=None), \
            patch("cli_providers.subprocess.Popen", return_value=proc) as popen:
        result = run_cli(provider, "Plan an allowed handoff", tmp_path, lambda event: None, threading.Event(),
                         shared_workspace=True, output_schema=LEAD_SCHEMA, native_schema=False, timeout=2)
    argv = popen.call_args.args[0]
    assert "--json-schema" not in argv and "--output-schema" not in argv
    assert not any("dangerously" in argument for argument in argv)
    assert popen.call_args.kwargs["shell"] is False
    if provider == "codex":
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert argv[argv.index("--ask-for-approval") + 1] == "never"
        prompt = proc.stdin.text.rstrip("\n")
    else:
        assert "--mode=plan" in argv and "--mode=accept-edits" not in argv and "--sandbox" in argv
        prompt = json.loads(proc.stdin.text)["message"]["content"]
    assert prompt.startswith("Plan an allowed handoff")
    assert "exactly one JSON object" in prompt and "do not bypass restrictions or retry denied operations" in prompt
    assert json.loads(prompt.split("JSON Schema:\n", 1)[1]) == LEAD_SCHEMA
    assert result["status"] == "completed" and json.loads(result["output"]) == final


@pytest.mark.parametrize("native_schema", [None, 0, 1, "false", []])
def test_native_schema_compatibility_requires_an_explicit_boolean(native_schema, tmp_path):
    with patch("cli_providers.discover_providers") as discover, patch("cli_providers.subprocess.Popen") as popen:
        with pytest.raises(ValueError, match="native_schema"):
            run_cli("antigravity", "Plan", tmp_path, lambda event: None, threading.Event(),
                    output_schema=LEAD_SCHEMA, native_schema=native_schema)
    discover.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("native_schema", [True, False])
@pytest.mark.parametrize("diagnostic", ["", f"no output produced: {FAKE_KEY}"])
def test_agy_schema_success_without_public_payload_is_failed_with_safe_diagnostic(native_schema, diagnostic, tmp_path):
    proc = FakeCLIProcess([{"event": "result", "result": {"status": "SUCCESS", "response": ""}}], diagnostic=diagnostic)
    events = []
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers.subprocess.Popen", return_value=proc):
        result = run_cli("antigravity", "Plan", tmp_path, events.append, threading.Event(),
                         shared_workspace=True, output_schema=LEAD_SCHEMA,
                         native_schema=native_schema, timeout=2)
    assert result["status"] == "failed" and result["output"] == ""
    assert result["error"]
    assert FAKE_KEY not in result["error"]
    if diagnostic:
        assert "no output produced" in result["error"]
    else:
        assert "未提供結構化結果" in result["error"]
    assert not any(event.get("status") == "completed" and event.get("kind") == "result" for event in events)
    assert any(event["kind"] == "error" for event in events)


def test_disabling_native_schema_without_schema_preserves_the_original_stdin(tmp_path):
    proc = FakeCLIProcess([{"event": "result", "result": {"status": "SUCCESS", "response": "public result"}}])
    with patch("cli_providers.discover_providers", side_effect=available_providers), \
            patch("cli_providers.subprocess.Popen", return_value=proc):
        result = run_cli("antigravity", "Unchanged ordinary task", tmp_path, lambda event: None,
                         threading.Event(), native_schema=False, timeout=2)
    assert json.loads(proc.stdin.text)["message"]["content"] == "Unchanged ordinary task"
    assert result["status"] == "completed"
