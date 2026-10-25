"""Project-bound discovery and transport, with one actual owner integration."""
import io
import json
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest

from desktop_runtime import DesktopServer
from monitor_service import MonitorService, TERMINAL
from office_bridge_client import BridgeClientError, OfficeBridgeClient

JOB = "a" * 32


@pytest.fixture
def client(tmp_path):
    project, data = tmp_path / "project", tmp_path / "data"
    project.mkdir()
    data.mkdir()
    record = {"version": 1, "workspace": str(project), "port": 12345, "nonce": "n" * 32, "token": "t" * 43}
    (data / "office-bridge.json").write_text(json.dumps(record), encoding="utf-8")
    return OfficeBridgeClient(project, data)


class Reply(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class Opener:
    def __init__(self, client, *, mismatch=False):
        self.calls = []
        self.client = client
        self.mismatch = mismatch
    def open(self, message, timeout):
        self.calls.append(message)
        assert message.full_url.startswith("http://127.0.0.1:12345/api/bridge/")
        assert message.get_header("Authorization") == "Bearer " + "t" * 43
        assert timeout == 10
        if message.full_url.endswith("/info"):
            return Reply({"version": 1, "workspace": str(self.client.project), "nonce": "wrong" if self.mismatch else "n" * 32})
        return Reply({"ok": True})


def test_owner_is_verified_before_fixed_authenticated_tool_requests(client):
    client._opener = opener = Opener(client)
    assert client.status() == {"ok": True}
    client.start({"provider": "antigravity", "prompt": "sample", "collaboration": {"members": [{"provider": "codex"}]}})
    client.get_task(JOB)
    client.cancel(JOB)
    client.review(JOB)
    assert len(opener.calls) == 10
    assert opener.calls[3].get_method() == "POST" and opener.calls[7].data == b"{}"


def test_mismatched_owner_prevents_any_mutation(client):
    client._opener = opener = Opener(client, mismatch=True)
    with pytest.raises(BridgeClientError) as error:
        client.start({"provider": "codex", "prompt": "sample", "collaboration": {"members": [{"provider": "antigravity"}]}})
    assert error.value.code == "owner_mismatch"
    assert len(opener.calls) == 1 and opener.calls[0].data is None


@pytest.mark.parametrize("change", [{"port": True}, {"port": 0}, {"version": True}, {"nonce": "short"}, {"token": "bad"}, {"workspace": "/other"}])
def test_invalid_discovery_never_opens_a_connection(client, change):
    record = json.loads(client.record_path.read_text())
    record.update(change)
    client.record_path.write_text(json.dumps(record))
    client._opener = opener = Opener(client)
    with pytest.raises(BridgeClientError):
        client.status()
    assert opener.calls == []


def test_legacy_focus_record_is_never_used_as_api_discovery(client):
    client.record_path.unlink()
    (client.data_dir / "instance.json").write_text('{"token":"PRIVATE_FOCUS_TOKEN"}')
    with pytest.raises(BridgeClientError) as error:
        client.status()
    assert error.value.code == "office_not_ready"
    assert "PRIVATE_FOCUS_TOKEN" not in str(error.value)


@pytest.mark.parametrize("job_id", ["../other", JOB + "/apply", "", None])
def test_invalid_ids_do_not_even_probe_the_backend(client, job_id):
    client._opener = opener = Opener(client)
    for method in (client.get_task, client.cancel, client.review):
        with pytest.raises(BridgeClientError):
            method(job_id)
    assert opener.calls == []


def test_http_errors_cannot_echo_credentials_or_follow_redirects(client):
    class Rejected:
        def open(self, message, timeout):
            raise HTTPError(message.full_url, 302, "redirect", {}, io.BytesIO(b"private response"))
    client._opener = Rejected()
    with pytest.raises(BridgeClientError) as error:
        client.status()
    assert "302" in str(error.value)
    assert "private response" not in str(error.value) and "t" * 43 not in str(error.value)


def test_actual_client_and_office_use_same_jobs_and_copy_without_remote_apply(tmp_path):
    project, data = tmp_path / "project", tmp_path / "data"
    project.mkdir()
    turns = [0]
    def discover(*_):
        return [{"id": value, "available": True, "command": value + ".exe"} for value in ("codex", "antigravity")]
    def runner(provider, prompt, workspace, emit, cancel, **options):
        if provider == "codex":
            output = {"summary": "Created proposal", "changes": [{"path": "feature.py", "operation": "create", "content": "answer = 42\n"}]}
        else:
            turns[0] += 1
            output = {"action": "finish", "summary": "Completed", "tasks": []} if turns[0] > 1 else {
                "action": "delegate", "summary": "Delegate", "tasks": [{"provider": "codex", "task": "Create feature.py", "task_mode": "write"}]}
        return {"status": "completed", "output": json.dumps(output)}
    service = MonitorService(project, runner=runner, discoverer=discover)
    server = DesktopServer(project, data, app_kwargs={"start_loop": False, "monitor_service": service})
    try:
        server.start()
        client = OfficeBridgeClient(project, data)
        assert client.status()["jobs"] == []
        result = client.start({"provider": "antigravity", "prompt": "Create feature.py", "collaboration": {"members": [{"provider": "codex"}]}})
        job_id = result["job"]["id"]
        deadline = time.monotonic() + 5
        while True:
            task = client.get_task(job_id)
            if task["job"]["status"] in TERMINAL:
                break
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert task["job"]["status"] == "completed" and len(task["children"]) == 3
        assert "prompt" not in task["job"] and not (project / "feature.py").exists()
        assert "answer = 42" in client.review(job_id)["diff"]
    finally:
        server.stop()
    assert not (data / "office-bridge.json").exists()


def test_long_chinese_task_roundtrips_through_real_client_and_mcp_with_summary(client):
    from office_bridge import compact_job
    from tools import island_mcp as mcp
    parent = {"id": JOB, "provider": "antigravity", "kind": "collaboration", "status": "running",
              "latest_message": "公開分析" * 2000,
              "events": [{"kind": "result", "text": "詳細結論" * 500} for _ in range(20)]}
    children = [{**parent, "id": char * 32, "provider": "codex", "kind": "delegation",
                 "parent_job_id": JOB, "status": "completed"} for char in ("b", "c")]
    task = {"job": compact_job(parent, events=True),
            "children": [compact_job(child, events=True) for child in children], "artifact": None}
    raw = json.dumps(task, ensure_ascii=False).encode("utf-8")
    assert 256 * 1024 < len(raw) < 4 * 1024 * 1024

    class LongReply(Opener):
        def open(self, message, timeout):
            if message.full_url.endswith("/info"):
                return super().open(message, timeout)
            self.calls.append(message)
            return io.BytesIO(raw)
    client._opener = LongReply(client)
    assert client.get_task(JOB)["children"][1]["id"] == "c" * 32
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                            "clientInfo": {"name": "regression", "version": "1"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "island_get_task", "arguments": {"job_id": JOB}}}]
    source = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in requests))
    output = io.BytesIO()
    assert mcp._serve(source, output, client) == 0
    lines = output.getvalue().splitlines()
    assert all(len(line) + 1 <= mcp.MAX_RESPONSE_BYTES for line in lines)
    result = json.loads(lines[-1])["result"]
    assert result["isError"] is False
    summary = result["structuredContent"]
    assert summary["truncated"] is True and "office UI" in summary["notice"]
    assert summary["job"]["id"] == JOB and summary["job"]["status"] == "running"
    assert [item["id"] for item in summary["children"]] == ["b" * 32, "c" * 32]
    assert all(item["status"] == "completed" and item["latest_message"] for item in summary["children"])
    assert len(json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode()) <= mcp.PUBLIC_RESULT_BYTES


def test_response_above_explicit_raw_http_cap_is_still_rejected(client):
    import office_bridge_client as bridge_client
    class TooLarge(Opener):
        def open(self, message, timeout):
            if message.full_url.endswith("/info"):
                return super().open(message, timeout)
            return io.BytesIO(b" " * (bridge_client._MAX_RESPONSE + 1))
    client._opener = TooLarge(client)
    with pytest.raises(BridgeClientError) as error:
        client.get_task(JOB)
    assert error.value.code == "invalid_response"
