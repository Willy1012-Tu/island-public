"""Authenticated bridge checks with fake jobs and local-only test listeners."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from monitor_service import MonitorError
from office_bridge import BridgeError, OfficeBridge, compact_job, register_office_bridge

JOB_ID = "a" * 32
CHILD_ID = "b" * 32
TOKEN_TEXT = "sk-proj-synthetic_secret_value_123456"


class FakeService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.calls = []
        self.cancelled = []
        self.jobs = [{"id": JOB_ID, "provider": "codex", "kind": "collaboration", "status": "running",
                      "prompt": "PRIVATE TASK PROMPT", "raw_prompt": "PRIVATE RAW PROMPT",
                      "command_key": "PRIVATE_COMMAND", "runner_options": {"command": "PRIVATE_COMMAND"},
                      "latest_message": "Public message api_key=" + TOKEN_TEXT,
                      "error": None, "active_tool": "read_file", "elapsed_seconds": 1,
                      "collaboration": {"phase": "planning", "round": 1, "max_rounds": 4,
                                        "summary": "Public summary", "members": [{"provider": "codex", "model": "test-model"}],
                                        "private": "PRIVATE COLLABORATION SETTINGS"},
                      "events": [{"id": index, "kind": "tool", "at": "2026-10-25T00:00:00Z",
                                  "tool": "read_file", "text": "Public activity", "arguments": "PRIVATE TOOL ARGS"}
                                 for index in range(35)],
                      "artifact": {"id": JOB_ID, "changed_files": [{"path": "main.py", "status": "modified",
                                                                  "additions": 1, "deletions": 0}],
                                   "applied": False, "payload": "PRIVATE CODE PAYLOAD"}}]

    def snapshot(self):
        return {"providers": [{"id": "codex", "name": "Codex", "phase": "running", "available": True,
                               "command": "PRIVATE_COMMAND", "reason": "",
                               "model_options": [{"id": "test-model", "name": "Test", "reasoning_efforts": ["high"],
                                                  "default_reasoning_effort": "high", "private": "PRIVATE_MODEL_DATA"}]}],
                "jobs": self.jobs, "events": [{"text": "PRIVATE GLOBAL EVENT"}]}

    def start_job(self, *args):
        self.calls.append(args)
        return self.jobs[0]

    def get_job(self, job_id):
        for job in self.jobs:
            if job["id"] == job_id:
                return job
        raise MonitorError("Task not found.", 404)

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        self.jobs[0]["cancel_requested"] = True
        return self.jobs[0]


def request_data():
    return {"provider": "codex", "prompt": "Review source.", "model": "test-model",
            "reasoning_effort": "high", "collaboration": {"members": [{"provider": "antigravity"}]}}


async def session(tmp_path):
    service = FakeService(tmp_path / "source")
    app = web.Application(client_max_size=64 * 1024)
    bridge = register_office_bridge(app, service, tmp_path / "private-data")
    assert not bridge.path.exists()
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    bridge.publish(client.server.port)
    record = json.loads(bridge.path.read_text(encoding="utf-8"))
    return service, bridge, client, {"Authorization": "Bearer " + record["token"]}, record


def test_info_authentication_and_record_privacy(tmp_path):
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        try:
            response = await client.get("/api/bridge/info", headers=headers)
            data = await response.json()
            assert response.status == 200
            assert data == {"version": 1, "workspace": str(service.workspace.resolve()), "nonce": record["nonce"]}
            assert record["token"] not in json.dumps(data)
            assert response.headers["Cache-Control"] == "no-store"
            for invalid in ({}, {"Authorization": "Bearer " + "x" * 43}, {"Authorization": "Basic no"}):
                response = await client.get("/api/bridge/info", headers=invalid)
                assert response.status == 401
            assert service.calls == []
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/bridge/info", None), ("get", "/api/bridge/status", None),
    ("get", f"/api/bridge/jobs/{JOB_ID}", None),
    ("get", f"/api/bridge/jobs/{JOB_ID}/changes", None),
    ("post", "/api/bridge/start", request_data()),
    ("post", f"/api/bridge/jobs/{JOB_ID}/cancel", {}),
])
def test_every_route_checks_bearer_and_preserves_origin_guard(tmp_path, method, path, body):
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        try:
            options = {"json": body} if body is not None else {}
            response = await getattr(client, method)(path, **options)
            assert response.status == 401
            response = await getattr(client, method)(path, headers={**headers, "Origin": "https://foreign.example"}, **options)
            assert response.status == 403
            assert service.calls == service.cancelled == []
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


def test_nonloopback_request_is_rejected_even_with_token(tmp_path):
    bridge = OfficeBridge(FakeService(tmp_path), tmp_path / "data")
    transport = Mock()
    transport.get_extra_info.side_effect = lambda key: ("192.0.2.20", 1234) if key == "peername" else None
    request = make_mocked_request("GET", "/api/bridge/info", transport=transport,
                                  headers={"Authorization": "Bearer " + bridge._token})
    try:
        with pytest.raises(MonitorError, match="this computer") as error:
            bridge._authorize(request)
        assert error.value.status == 403
    finally:
        bridge.close()


def test_status_and_job_whitelist_public_bounded_fields(tmp_path):
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        service.jobs.append({**service.jobs[0], "id": CHILD_ID, "kind": "delegation", "parent_job_id": JOB_ID})
        try:
            response = await client.get("/api/bridge/status", headers=headers)
            state = await response.json()
            assert len(state["providers"]) == 1 and len(state["jobs"]) == 2
            assert "events" not in state["jobs"][0]
            response = await client.get(f"/api/bridge/jobs/{JOB_ID}", headers=headers)
            data = await response.json()
            assert len(data["job"]["events"]) == 20 and len(data["children"]) == 1
            encoded = json.dumps(data) + json.dumps(state)
            assert "PRIVATE" not in encoded and TOKEN_TEXT not in encoded
            assert "[REDACTED]" in encoded and "Public message" in encoded
            assert record["token"] not in encoded
            assert data["artifact"]["changed_files"][0]["path"] == "main.py"
            assert service.calls == []
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


def test_start_forwards_to_same_service_and_cancel_uses_parent(tmp_path):
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        try:
            response = await client.post("/api/bridge/start", json=request_data(), headers=headers)
            assert response.status == 202 and (await response.json())["job"]["id"] == JOB_ID
            assert service.calls == [("codex", "Review source.", "test-model", "high", request_data()["collaboration"])]
            response = await client.post(f"/api/bridge/jobs/{JOB_ID}/cancel", json={}, headers=headers)
            assert response.status == 202 and (await response.json())["job"]["cancel_requested"] is True
            assert service.cancelled == [JOB_ID]
            response = await client.post(f"/api/bridge/jobs/{JOB_ID}/apply", json={}, headers=headers)
            assert response.status == 404
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"provider": "codex", "prompt": "Solo."},
    {"provider": "codex", "prompt": "Solo.", "collaboration": None},
    {**request_data(), "workspace": "outside"}, {**request_data(), "command": "anything"},
    {**request_data(), "execution_mode": "workspace_write"},
])
def test_start_rejects_solo_or_new_execution_parameters(tmp_path, body):
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        try:
            response = await client.post("/api/bridge/start", json=body, headers=headers)
            assert response.status == 400 and service.calls == []
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("body", ['{"provider":"codex","provider":"antigravity"}', '{"x":NaN}', '{'])
def test_controls_reject_duplicate_nonfinite_and_malformed_json(tmp_path, body):
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        try:
            response = await client.post("/api/bridge/start", data=body,
                                         headers={**headers, "Content-Type": "application/json"})
            assert response.status == 400 and service.calls == []
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


def test_controls_require_json_and_cancel_empty_object(tmp_path):
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        try:
            response = await client.post("/api/bridge/start", data="x", headers=headers)
            assert response.status == 415
            response = await client.post(f"/api/bridge/jobs/{JOB_ID}/cancel", json={"prompt": "new work"}, headers=headers)
            assert response.status == 400 and service.cancelled == []
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("job_id", ["not-a-job", "C" * 32, "c" * 32, CHILD_ID])
def test_only_known_hex_parent_collaborations_can_be_addressed(tmp_path, job_id):
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        service.jobs.append({"id": CHILD_ID, "kind": "delegation", "parent_job_id": JOB_ID})
        try:
            response = await client.get(f"/api/bridge/jobs/{job_id}", headers=headers)
            assert response.status == 404
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


def test_changes_is_read_only_explicit_redacted_preview(tmp_path, monkeypatch):
    import collaboration_workspace
    class Store:
        def __init__(self, workspace):
            assert workspace == tmp_path / "source"
        def review(self, job_id):
            assert job_id == JOB_ID
            return {"artifact": {"id": job_id, "changed_files": []}, "diff": f"api_key={TOKEN_TEXT}\n" + "x" * 65000}
        def apply(self, *args):
            raise AssertionError("Bridge must not apply changes")
    monkeypatch.setattr(collaboration_workspace, "ArtifactStore", Store)
    async def scenario():
        service, bridge, client, headers, record = await session(tmp_path)
        try:
            response = await client.get(f"/api/bridge/jobs/{JOB_ID}/changes", headers=headers)
            result = await response.json()
            assert response.status == 200 and result["truncated"] is True
            assert TOKEN_TEXT not in result["diff"] and "[REDACTED]" in result["diff"]
            assert service.calls == service.cancelled == []
        finally:
            bridge.close()
            await client.close()
    asyncio.run(scenario())


def test_record_published_after_ready_and_removed_only_by_its_owner(tmp_path):
    service = FakeService(tmp_path)
    bridge = OfficeBridge(service, tmp_path / "data")
    try:
        assert not bridge.path.exists()
        bridge.publish(12345)
        record = json.loads(bridge.path.read_text())
        assert set(record) == {"version", "workspace", "port", "nonce", "token"}
        assert record["port"] == 12345
        if os.name != "nt":
            assert bridge.path.stat().st_mode & 0o077 == 0
        replacement = {**record, "nonce": secrets.token_urlsafe(24), "token": secrets.token_urlsafe(32)}
        bridge.path.write_text(json.dumps(replacement))
    finally:
        bridge.close()
    assert json.loads(bridge.path.read_text()) == replacement
    assert not list(bridge.data_dir.glob(".office-bridge-*.tmp"))


def test_close_removes_owned_record_and_is_idempotent(tmp_path):
    bridge = OfficeBridge(FakeService(tmp_path), tmp_path / "data")
    bridge.publish(12345)
    bridge.close()
    bridge.close()
    assert not bridge.path.exists()
    assert (bridge.data_dir / "office-bridge.lock").exists()


def test_second_live_owner_cannot_replace_record(tmp_path):
    service = FakeService(tmp_path)
    bridge = OfficeBridge(service, tmp_path / "data")
    bridge.publish(12345)
    before = bridge.path.read_bytes()
    try:
        with pytest.raises(BridgeError, match="owns"):
            OfficeBridge(service, bridge.data_dir)
        assert bridge.path.read_bytes() == before
    finally:
        bridge.close()


@pytest.mark.parametrize("content", ["{}", "not JSON", "x" * 20000,
    '{"version":1,"version":1,"workspace":"x","port":1,"nonce":"' + "a" * 32 + '","token":"' + "b" * 32 + '"}'])
def test_foreign_or_malformed_record_is_preserved(tmp_path, content):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "office-bridge.json"
    path.write_text(content)
    with pytest.raises(BridgeError, match="preserved"):
        OfficeBridge(FakeService(tmp_path), data)
    assert path.read_text() == content


def test_valid_record_for_another_workspace_is_not_overwritten(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    record = {"version": 1, "workspace": str(tmp_path / "different"), "port": 1234,
              "nonce": "a" * 32, "token": "b" * 32}
    path = data / "office-bridge.json"
    path.write_text(json.dumps(record))
    with pytest.raises(BridgeError, match="another workspace"):
        OfficeBridge(FakeService(tmp_path), data)
    assert json.loads(path.read_text()) == record


def test_stale_same_workspace_record_is_replaced_only_after_lock_ownership(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    record = {"version": 1, "workspace": str(tmp_path.resolve()), "port": 1234,
              "nonce": "a" * 32, "token": "b" * 32}
    (data / "office-bridge.json").write_text(json.dumps(record))
    bridge = OfficeBridge(FakeService(tmp_path), data)
    try:
        assert json.loads(bridge.path.read_text()) == record
        bridge.publish(4321)
        current = json.loads(bridge.path.read_text())
        assert current["port"] == 4321 and current["token"] != record["token"]
    finally:
        bridge.close()


def test_failed_publish_does_not_remove_foreign_record_or_leak_temporary(tmp_path, monkeypatch):
    bridge = OfficeBridge(FakeService(tmp_path), tmp_path / "data")
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("failure")))
    try:
        with pytest.raises(BridgeError, match="publish"):
            bridge.publish(12345)
        assert not bridge.path.exists() and not list(bridge.data_dir.glob(".office-bridge-*.tmp"))
    finally:
        bridge.close()


def test_record_link_is_rejected_without_modifying_target(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("outside")
    try:
        (data / "office-bridge.json").symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links require Windows developer-mode privilege")
    with pytest.raises(BridgeError, match="link"):
        OfficeBridge(FakeService(tmp_path), data)
    assert outside.read_text() == "outside"


def test_compact_job_bounds_messages_and_never_returns_prompt_or_payload(tmp_path):
    job = FakeService(tmp_path).jobs[0]
    job["latest_message"] = "x" * 100000
    result = compact_job(job, events=True)
    assert len(result["latest_message"]) == 8000 and len(result["events"]) == 20
    assert "prompt" not in result and "runner_options" not in result
    assert "payload" not in result["artifact"]
