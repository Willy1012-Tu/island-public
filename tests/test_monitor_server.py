"""Owned, streamed coding jobs over real local HTTP/WebSockets; no model calls."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import threading
import types
from pathlib import Path

import pytest
from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

import monitor_server
import cli_providers
from monitor_server import MONITOR_KEY, register_monitor
from monitor_service import MonitorError, MonitorService
from web_server import RUNTIME_KEY, create_app

SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-6-thinking"
GEMINI = "gemini-3.8-flash-high"
GEMINI_MEDIUM = "gemini-3.8-flash-medium"
PROFILES = {
    "codex": {"transport": "codex", "model": None, "model_options": []},
    "claude": {"transport": "antigravity", "model": SONNET,
               "model_options": [{"id": SONNET, "name": "Claude Sonnet 4.6"},
                                 {"id": OPUS, "name": "Claude Opus 4.6 Thinking"}]},
    "antigravity": {"transport": "antigravity", "model": GEMINI,
                    "model_options": [{"id": GEMINI, "name": "Gemini high"},
                                      {"id": GEMINI_MEDIUM, "name": "Gemini medium"}]},
}


class ControlledRunner:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.active = 0
        self.lock = threading.Lock()

    def __call__(self, provider, prompt, workspace, emit, cancel, **options):
        with self.lock:
            self.active += 1
            self.calls.append((provider, prompt, workspace, options))
        try:
            emit({"kind": "status", "text": "Reading workspace", "model": "test-model", "session_id": "test-session"})
            emit({"kind": "tool", "text": "Inspect source", "tool": "read_file", "status": "running"})
            emit({"kind": "message", "text": "I found ", "delta": True})
            emit({"kind": "message", "text": "the entry point.", "delta": True})
            while not self.release.wait(0.01):
                if cancel.is_set():
                    return {"status": "cancelled", "exit_code": None}
            emit({"kind": "tool", "text": "Source inspected", "tool": "read_file", "status": "completed"})
            emit({"kind": "message", "text": "Review complete."})
            emit({"kind": "result", "text": "Task completed."})
            return {"status": "completed", "exit_code": 0, "model": "test-model",
                    "session_id": "test-session", "usage": {"input_tokens": 12, "output_tokens": 8}, "error": None}
        finally:
            with self.lock:
                self.active -= 1


def service(tmp_path, runner=None, *, available=("codex", "claude", "antigravity"), profiles=None):
    runner = runner or ControlledRunner()
    profiles = profiles or PROFILES

    def discover(commands=None):
        return [{"id": name, "name": name.title(), "available": name in available,
                 "reason": "" if name in available else "CLI not installed.", "auth": "unknown",
                 "execution_mode": "read_only" if name == "codex" else "isolated",
                 "execution_note": "Test execution mode.", **profiles[name]}
                for name in ("codex", "claude", "antigravity")]

    return MonitorService(tmp_path, commands={"codex": "configured-codex"},
                          timeout=12, runner=runner, discoverer=discover), runner


def frontend(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "monitor.html").write_text("<title>Task monitor</title>", encoding="utf-8")
    (root / "index.html").write_text("<title>Original island</title>", encoding="utf-8")
    return root


def app_for(tmp_path, selected):
    app = web.Application(client_max_size=64 * 1024)
    register_monitor(app, workspace=tmp_path, web_root=frontend(tmp_path), service=selected)
    return app


async def until(predicate, timeout=2):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


async def receive(socket, predicate=lambda state: True):
    async def wait():
        while True:
            state = await socket.receive_json()
            if state.get("type") == "monitor_state" and predicate(state):
                return state
    return await asyncio.wait_for(wait(), 2)


def test_no_jobs_autostart_and_missing_providers_are_truthful(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path, available=("codex",))
        app = app_for(tmp_path, selected)
        async with TestClient(TestServer(app)) as client:
            assert "Task monitor" in await (await client.get("/monitor")).text()
            assert "Original island" in await (await client.get("/island")).text()
            socket = await client.ws_connect("/ws/monitor")
            state = await receive(socket)
            assert state["idle_behavior"] == "scripted" and state["jobs"] == []
            assert state["workspace"] == str(tmp_path.resolve())
            assert all(item["auth"] == "unknown" for item in state["providers"])
            cards = {item["id"]: item for item in state["providers"]}
            assert cards["claude"]["phase"] == "missing" and not cards["claude"]["available"]
            assert cards["codex"]["persona"]["name"] == "沉穩築路者"
            assert cards["antigravity"]["execution_mode"] == "isolated"
            response = await client.post("/api/monitor/jobs", json={"provider": "claude", "prompt": "Review this source."})
            assert response.status == 409
            assert (await response.json())["type"] == "monitor_error"
            assert runner.calls == []
            await socket.close()
    asyncio.run(scenario())


def test_three_providers_stream_real_job_state_and_only_one_job_per_provider(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path)
        notifications = []
        selected.subscribe(lambda: notifications.append(threading.get_ident()))
        loop_thread = threading.get_ident()
        try:
            jobs = [selected.start_job(provider, "Review entry point.") for provider in ("codex", "claude", "antigravity")]
            with pytest.raises(MonitorError, match="active"):
                selected.start_job("codex", "Another task.")
            await until(lambda: runner.active == 3 and all(job["latest_message"] == "I found the entry point."
                                                         for job in selected.snapshot()["jobs"]))
            state = selected.snapshot()
            assert len(state["jobs"]) == 3
            assert all(item["phase"] == "running" for item in state["providers"])
            for job in state["jobs"]:
                assert job["latest_message"] == "I found the entry point."
                assert job["active_tool"] == "read_file"
                assert job["model"] == "test-model" and job["session_id"] == "test-session"
                assert job["elapsed_seconds"] >= 0 and job["started_at"].endswith("Z")
            assert all(identity == loop_thread for identity in notifications)
            assert all(call[2] == tmp_path.resolve() and call[3]["timeout"] == 12 for call in runner.calls)
            assert next(call for call in runner.calls if call[0] == "codex")[3]["command"] == "configured-codex"
            runner.release.set()
            await until(lambda: all(job["status"] == "completed" for job in selected.snapshot()["jobs"]))
            assert all(job["active_tool"] is None and job["usage"]["output_tokens"] == 8 for job in selected.snapshot()["jobs"])
            assert all(item["active_job_id"] is None for item in selected.snapshot()["providers"])
            assert all(item["auth"] == "verified" for item in selected.snapshot()["providers"])
            assert all(job["latest_message"] == "Review complete." for job in selected.snapshot()["jobs"])
            assert len({job["id"] for job in jobs}) == 3
        finally:
            await selected.close()
        assert runner.active == 0
    asyncio.run(scenario())


def test_websocket_start_shared_updates_cancel_and_reuse_provider(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path)
        app = app_for(tmp_path, selected)
        async with TestClient(TestServer(app)) as client:
            first, second = await client.ws_connect("/ws/monitor"), await client.ws_connect("/ws/monitor")
            await receive(first)
            await receive(second)
            await first.send_json({"type": "monitor_control", "action": "start", "provider": "codex", "prompt": "Review source."})
            active = await receive(second, lambda state: bool(state["jobs"]) and state["jobs"][0]["active_tool"] == "read_file")
            job_id = active["jobs"][0]["id"]
            await first.send_json({"type": "monitor_control", "action": "cancel", "job_id": job_id})
            cancelled = await receive(second, lambda state: state["jobs"][0]["status"] == "cancelled")
            assert cancelled["jobs"][0]["cancel_requested"]
            assert cancelled["providers"][0]["active_job_id"] is None
            # Cancellation does not permanently close the provider.
            runner.release.set()
            response = await client.post("/api/monitor/jobs", json={"provider": "codex", "prompt": "Review again."})
            assert response.status == 202
            await receive(second, lambda state: len(state["jobs"]) == 2 and state["jobs"][0]["status"] == "completed")
            await first.close()
            await second.close()
        assert runner.active == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    {"provider": "unknown", "prompt": "Review."}, {"provider": "codex", "prompt": " "},
    {"provider": "codex", "prompt": "x" * 8001},
    {"provider": "codex", "prompt": "Review.", "workspace": "C:/"},
    {"provider": "codex", "prompt": "Review.", "command": "arbitrary"}, [],
])
def test_http_task_fields_are_bounded_and_cannot_override_execution(tmp_path, data):
    async def scenario():
        selected, runner = service(tmp_path)
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            response = await client.post("/api/monitor/jobs", json=data)
            assert response.status == 400
            assert (await response.json())["type"] == "monitor_error"
            assert runner.calls == []
    asyncio.run(scenario())


def test_foreign_origin_and_non_json_execution_requests_are_rejected(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path)
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            start = await client.post("/api/monitor/jobs", json={"provider": "codex", "prompt": "Review."},
                                      headers={"Origin": "https://foreign.example"})
            assert start.status == 403
            cancel = await client.post("/api/monitor/jobs/unknown/cancel", headers={"Origin": "null"})
            assert cancel.status == 403
            with pytest.raises(WSServerHandshakeError) as error:
                await client.ws_connect("/ws/monitor", headers={"Origin": "https://foreign.example"})
            assert error.value.status == 403
            form = await client.post("/api/monitor/jobs", data={"provider": "codex", "prompt": "Review."})
            assert form.status == 415
            unknown = await client.post("/api/monitor/jobs/unknown/cancel")
            assert unknown.status == 404
            assert runner.calls == []
    asyncio.run(scenario())


def test_malformed_ws_controls_return_errors_without_running_tasks(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path)
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            socket = await client.ws_connect("/ws/monitor")
            await receive(socket)
            for data in ([], {"type": "control", "action": "refresh"},
                         {"type": "monitor_control", "action": "start", "provider": "codex", "prompt": "Review.", "cwd": "C:/"}):
                await socket.send_json(data)
                assert (await socket.receive_json())["type"] == "monitor_error"
            await socket.send_str("not json")
            assert (await socket.receive_json())["type"] == "monitor_error"
            await socket.send_bytes(b"start")
            assert (await socket.receive_json())["type"] == "monitor_error"
            assert runner.calls == []
            await socket.close()
    asyncio.run(scenario())


def test_monitor_backpressure_does_not_stall_other_clients_or_island(tmp_path, monkeypatch):
    async def scenario():
        selected, runner = service(tmp_path)
        original = monitor_server._send_states
        blocked, release = asyncio.Event(), asyncio.Event()
        first_peer = None

        async def slow_first(peer):
            nonlocal first_peer
            if first_peer is None:
                first_peer = peer
                blocked.set()
                await release.wait()
            await original(peer)

        monkeypatch.setattr(monitor_server, "_send_states", slow_first)
        app = create_app(web_root=frontend(tmp_path), tick_seconds=0.02, monitor_service=selected)
        async with TestClient(TestServer(app)) as client:
            slow = await client.ws_connect("/ws/monitor")
            await asyncio.wait_for(blocked.wait(), 1)
            fast = await client.ws_connect("/ws/monitor")
            await receive(fast)
            await fast.send_json({"type": "monitor_control", "action": "start", "provider": "codex", "prompt": "Review."})
            active = await receive(fast, lambda state: bool(state["jobs"]) and state["jobs"][0]["latest_message"] != "")
            await until(lambda: app[RUNTIME_KEY].simulation.tick_count >= 4)
            assert first_peer.queue.maxsize == first_peer.queue.qsize() == 1
            island = await (await client.get("/api/state")).json()
            assert island["type"] == "state" and island["version"] == 1 and island["tick"] >= 4
            assert "Original island" in await (await client.get("/")).text()
            release.set()
            latest = await receive(slow)
            assert latest["revision"] >= active["revision"]
            assert latest["jobs"][0]["id"] == active["jobs"][0]["id"]
            await slow.close()
            await fast.close()
        assert runner.active == 0 and selected.snapshot()["jobs"][0]["status"] == "cancelled"
    asyncio.run(scenario())


def test_job_and_event_histories_are_bounded_and_ids_stay_monotonic(tmp_path):
    def burst(provider, prompt, workspace, emit, cancel, **options):
        for _ in range(120):
            emit({"kind": "message", "text": "short public update"})
        emit({"kind": "message", "text": "x" * 9000})
        return {"status": "completed", "exit_code": 0}

    async def scenario():
        selected, _ = service(tmp_path, runner=burst)
        try:
            for _ in range(22):
                selected.start_job("codex", "Review.")
                await until(lambda: selected.snapshot()["providers"][0]["active_job_id"] is None)
            state = selected.snapshot()
            assert len(state["jobs"]) == 20 and len(state["events"]) == 300
            assert all(len(job["events"]) <= 100 for job in state["jobs"])
            assert all(len(event["text"]) <= 8000 for event in state["events"])
            assert len(state["jobs"][0]["latest_message"]) == 8000
            ids = [event["id"] for event in state["events"]]
            assert ids == sorted(set(ids)) and ids[0] > 1
        finally:
            await selected.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["failed", "timed_out"])
def test_runner_terminal_errors_are_visible_without_fake_connected_status(tmp_path, terminal):
    def failed(provider, prompt, workspace, emit, cancel, **options):
        return {"status": terminal, "exit_code": 1, "error": "CLI could not complete this task."}

    async def scenario():
        selected, _ = service(tmp_path, runner=failed)
        try:
            selected.start_job("codex", "Review.")
            await until(lambda: selected.snapshot()["jobs"][0]["status"] in ("failed", "timed_out"))
            state = selected.snapshot()
            assert state["jobs"][0]["error"] == "CLI could not complete this task."
            assert state["providers"][0]["phase"] == "error"
            assert state["providers"][0]["auth"] == "unknown"
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_successful_auth_verification_tracks_the_discovered_command(tmp_path):
    capability = {"command": "one.exe", "available": True}

    def discover(commands=None):
        return [{"id": "codex", "name": "Codex", "auth": "unknown", "reason": "", **capability}]

    def complete(provider, prompt, workspace, emit, cancel, **options):
        return {"status": "completed", "exit_code": 0}

    async def scenario():
        selected = MonitorService(tmp_path, runner=complete, discoverer=discover)
        try:
            selected.start_job("codex", "Review.")
            await until(lambda: selected.snapshot()["jobs"][0]["status"] == "completed")
            assert selected.snapshot()["providers"][0]["auth"] == "verified"
            selected.refresh()
            assert selected.snapshot()["providers"][0]["auth"] == "verified"
            capability["command"] = "two.exe"
            selected.refresh()
            assert selected.snapshot()["providers"][0]["auth"] == "unknown"
            capability["available"] = False
            selected.refresh()
            assert selected.snapshot()["providers"][0]["phase"] == "missing"
            assert selected.snapshot()["providers"][0]["auth"] == "unknown"
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_explicit_login_failure_reports_auth_required(tmp_path):
    def fail(provider, prompt, workspace, emit, cancel, **options):
        return {"status": "failed", "exit_code": 1, "error": "Not logged in. Please run claude login."}

    async def scenario():
        selected, _ = service(tmp_path, runner=fail)
        try:
            selected.start_job("claude", "Review.")
            await until(lambda: selected.snapshot()["jobs"][0]["status"] == "failed")
            assert selected.snapshot()["providers"][1]["auth"] == "required"
            assert selected.snapshot()["providers"][0]["auth"] == "unknown"
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_private_prompt_is_not_exposed_and_public_result_can_replace_draft(tmp_path):
    received = []
    raw = "Review configuration api_key=sk-proj-example_secret_value_123456"

    def complete(provider, prompt, workspace, emit, cancel, **options):
        received.append(prompt)
        emit({"kind": "message", "text": "Draft answer."})
        emit({"kind": "result", "text": "Final public answer.", "public_output": True})
        emit({"kind": "result", "text": "Task completed."})
        return {"status": "completed", "exit_code": 0}

    async def scenario():
        selected, _ = service(tmp_path, runner=complete)
        try:
            selected.start_job("codex", raw)
            await until(lambda: selected.snapshot()["jobs"][0]["status"] == "completed")
            job = selected.snapshot()["jobs"][0]
            assert received == [raw]
            assert "[REDACTED]" in job["prompt"] and "example_secret" not in job["prompt"]
            assert job["latest_message"] == "Final public answer."
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_startup_workspace_is_fixed_and_supports_full_chinese_prompt(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path)
        app = create_app(web_root=frontend(tmp_path), monitor_service=selected, start_loop=False)
        async with TestClient(TestServer(app)) as client:
            prompt = "好" * 8000
            response = await client.post("/api/monitor/jobs", json={"provider": "codex", "prompt": prompt})
            assert response.status == 202
            await until(lambda: bool(runner.calls))
            assert runner.calls[0][1] == prompt and runner.calls[0][2] == tmp_path.resolve()
            assert (await (await client.get("/api/monitor")).json())["workspace"] == str(tmp_path.resolve())
    asyncio.run(scenario())


def test_claude_office_defaults_to_antigravity_sonnet_without_native_claude_login(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path)
        try:
            cards = {item["id"]: item for item in selected.snapshot()["providers"]}
            assert cards["claude"]["transport"] == "antigravity"
            assert cards["claude"]["configured_model"] == cards["claude"]["model"] == SONNET
            assert cards["claude"]["model_options"] == PROFILES["claude"]["model_options"]
            job = selected.start_job("claude", "Review with Sonnet.")
            assert job["transport"] == "antigravity" and job["configured_model"] == job["model"] == SONNET
            await until(lambda: bool(runner.calls))
            assert runner.calls[0][0] == "claude" and runner.calls[0][3]["model"] == SONNET
            runner.release.set()
            await until(lambda: selected.snapshot()["jobs"][0]["status"] == "completed")
            cards = {item["id"]: item for item in selected.snapshot()["providers"]}
            assert cards["claude"]["auth"] == "verified"
            assert cards["antigravity"]["auth"] == "unknown"
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_ws_and_http_allow_configured_office_models_and_keep_transport_visible(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path)
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            socket = await client.ws_connect("/ws/monitor")
            await receive(socket)
            await socket.send_json({"type": "monitor_control", "action": "start", "provider": "claude",
                                    "prompt": "Review with Opus.", "model": OPUS})
            state = await receive(socket, lambda value: bool(value["jobs"]) and value["jobs"][0]["status"] == "running")
            opus = state["jobs"][0]
            assert opus["provider"] == "claude" and opus["transport"] == "antigravity"
            assert opus["configured_model"] == OPUS
            response = await client.post("/api/monitor/jobs", json={"provider": "antigravity", "prompt": "Review with Gemini.",
                                                                   "model": GEMINI_MEDIUM})
            assert response.status == 202
            gemini = (await response.json())["job"]
            assert gemini["transport"] == "antigravity" and gemini["configured_model"] == GEMINI_MEDIUM
            await until(lambda: len(runner.calls) == 2)
            assert {call[0]: call[3]["model"] for call in runner.calls} == {"claude": OPUS, "antigravity": GEMINI_MEDIUM}
            await socket.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("provider,model", [
    ("claude", GEMINI), ("antigravity", SONNET), ("codex", OPUS),
    ("claude", "--model arbitrary"), ("claude", 1), ("claude", True), ("claude", {}),
])
def test_browser_cannot_choose_arbitrary_or_cross_office_models(tmp_path, provider, model):
    async def scenario():
        selected, runner = service(tmp_path)
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            response = await client.post("/api/monitor/jobs", json={"provider": provider, "prompt": "Review.", "model": model})
            assert response.status == 400
            assert (await response.json())["type"] == "monitor_error"
            assert runner.calls == []
    asyncio.run(scenario())


def test_selected_model_is_frozen_before_a_provider_refresh(tmp_path):
    profile = dict(PROFILES["claude"])

    def discover(commands=None):
        return [{"id": "claude", "name": "Claude 4.6", "available": True, "auth": "unknown", "reason": "", **profile}]

    async def scenario():
        runner = ControlledRunner()
        selected = MonitorService(tmp_path, runner=runner, discoverer=discover)
        try:
            selected.start_job("claude", "Use the selected model.", OPUS)
            profile.update(model=SONNET, model_options=[{"id": SONNET, "name": "Sonnet"}])
            selected.refresh()
            await until(lambda: bool(runner.calls))
            assert runner.calls[0][3]["model"] == OPUS
            assert selected.snapshot()["jobs"][0]["configured_model"] == OPUS
            assert selected.snapshot()["jobs"][0]["transport"] == "antigravity"
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_real_provider_discovery_defaults_and_shared_executable_are_frozen_per_job(tmp_path, monkeypatch):
    paths = {"codex": str(tmp_path / "codex.exe"), "shared-agy": str(tmp_path / "first-agy.exe")}
    monkeypatch.setattr(cli_providers.shutil, "which", lambda command: paths.get(command))
    monkeypatch.setattr(cli_providers, "_saved_model", lambda: "saved-codex-model")

    async def scenario():
        runner = ControlledRunner()
        selected = MonitorService(tmp_path, commands={"antigravity": "shared-agy"},
                                  runner=runner, discoverer=cli_providers.discover_providers)
        try:
            cards = {item["id"]: item for item in selected.snapshot()["providers"]}
            assert cards["codex"]["configured_model"] == "saved-codex-model"
            assert cards["claude"]["configured_model"] == cards["claude"]["model"] == SONNET
            assert cards["antigravity"]["configured_model"] == cards["antigravity"]["model"] == GEMINI
            assert cards["claude"]["transport"] == cards["antigravity"]["transport"] == "antigravity"
            claude = selected.start_job("claude", "Use default Sonnet.")
            antig = selected.start_job("antigravity", "Use default Gemini.")
            # Refresh before either queued worker starts: both jobs must retain
            # the executable that was actually discovered when they were queued.
            paths["shared-agy"] = str(tmp_path / "second-agy.exe")
            selected.refresh()
            await until(lambda: len(runner.calls) == 2)
            options = {call[0]: call[3] for call in runner.calls}
            assert options["claude"]["command"] == str(tmp_path / "first-agy.exe")
            assert options["antigravity"]["command"] == str(tmp_path / "first-agy.exe")
            assert options["claude"]["model"] == claude["configured_model"] == SONNET
            assert options["antigravity"]["model"] == antig["configured_model"] == GEMINI
            runner.release.set()
            await until(lambda: all(job["status"] == "completed" for job in selected.snapshot()["jobs"]))
            # A completed job from the old executable cannot verify its replacement.
            cards = {item["id"]: item for item in selected.snapshot()["providers"]}
            assert cards["claude"]["auth"] == cards["antigravity"]["auth"] == "unknown"
        finally:
            await selected.close()
    asyncio.run(scenario())


def reasoning_profiles():
    return {**PROFILES, "codex": {
        "transport": "codex", "configured_model": "codex-main", "configured_reasoning_effort": "high",
        "model_options": [
            {"id": "codex-main", "name": "Main", "reasoning_efforts": ["low", "medium", "high"],
             "default_reasoning_effort": "medium"},
            {"id": "codex-small", "name": "Small", "reasoning_efforts": ["low", "medium"],
             "default_reasoning_effort": "low"},
            {"id": "codex-deep", "name": "Deep", "reasoning_efforts": ["medium", "high", "xhigh"],
             "default_reasoning_effort": "medium"},
            {"id": "codex-no-effort", "name": "No effort setting", "reasoning_efforts": [],
             "default_reasoning_effort": None},
        ]}}


@pytest.mark.parametrize("model,requested,expected", [
    (None, None, "high"), ("codex-small", None, "low"),
    ("codex-deep", "xhigh", "xhigh"), ("codex-main", "low", "low"),
    ("codex-no-effort", None, None),
])
def test_codex_discovered_models_select_supported_effort_defaults(tmp_path, model, requested, expected):
    async def scenario():
        selected, runner = service(tmp_path, profiles=reasoning_profiles())
        runner.release.set()
        try:
            card = selected.snapshot()["providers"][0]
            assert card["configured_reasoning_effort"] == "high"
            assert card["model_options"][0]["reasoning_efforts"] == ["low", "medium", "high"]
            assert card["model_options"][0]["default_reasoning_effort"] == "medium"
            queued = selected.start_job("codex", "Review with selected effort.", model, requested)
            assert queued["reasoning_effort"] == expected
            await until(lambda: selected.snapshot()["jobs"][0]["status"] == "completed")
            options = runner.calls[0][3]
            assert options["model"] == (model or "codex-main")
            if expected is None:
                assert "reasoning_effort" not in options
            else:
                assert options["reasoning_effort"] == expected
            assert selected.snapshot()["jobs"][0]["reasoning_effort"] == expected
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_http_and_ws_start_accept_codex_model_and_effort(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path, profiles=reasoning_profiles())
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            socket = await client.ws_connect("/ws/monitor")
            await receive(socket)
            await socket.send_json({"type": "monitor_control", "action": "start", "provider": "codex",
                                    "prompt": "Review deeply.", "model": "codex-deep", "reasoning_effort": "xhigh"})
            active = await receive(socket, lambda state: bool(state["jobs"]) and state["jobs"][0]["status"] == "running")
            assert active["jobs"][0]["configured_model"] == "codex-deep"
            assert active["jobs"][0]["reasoning_effort"] == "xhigh"
            await client.post(f"/api/monitor/jobs/{active['jobs'][0]['id']}/cancel")
            await receive(socket, lambda state: state["jobs"][0]["status"] == "cancelled")
            runner.release.set()
            response = await client.post("/api/monitor/jobs", json={"provider": "codex", "prompt": "Review quickly.",
                                                                   "model": "codex-small", "reasoning_effort": "low"})
            assert response.status == 202
            assert (await response.json())["job"]["reasoning_effort"] == "low"
            await receive(socket, lambda state: len(state["jobs"]) == 2 and state["jobs"][0]["status"] == "completed")
            assert [call[3]["reasoning_effort"] for call in runner.calls] == ["xhigh", "low"]
            await socket.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("provider,model,effort", [
    ("codex", "codex-main", "xhigh"), ("codex", "codex-no-effort", "low"),
    ("codex", "codex-small", "ultra"), ("codex", "codex-main", ""),
    ("codex", "codex-main", True), ("codex", "codex-main", {}),
    ("codex", "codex-main", 1), ("claude", SONNET, "high"),
    ("antigravity", GEMINI, "low"),
])
def test_effort_is_validated_for_the_selected_model_and_codex_only(tmp_path, provider, model, effort):
    async def scenario():
        selected, runner = service(tmp_path, profiles=reasoning_profiles())
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            response = await client.post("/api/monitor/jobs", json={"provider": provider, "prompt": "Review.",
                                                                   "model": model, "reasoning_effort": effort})
            assert response.status == 400
            assert (await response.json())["type"] == "monitor_error"
            assert runner.calls == []
    asyncio.run(scenario())


def test_codex_effort_is_frozen_across_refresh_and_preserved_in_public_history(tmp_path):
    async def scenario():
        profiles = reasoning_profiles()
        selected, runner = service(tmp_path, profiles=profiles)
        restored, restored_runner = service(tmp_path, profiles=reasoning_profiles())
        try:
            queued = selected.start_job("codex", "Review with a frozen selection.", "codex-deep", "xhigh")
            profiles["codex"]["configured_reasoning_effort"] = "medium"
            profiles["codex"]["model_options"][2]["reasoning_efforts"] = ["medium"]
            selected.refresh()
            await until(lambda: bool(runner.calls))
            assert runner.calls[0][3]["reasoning_effort"] == "xhigh"
            assert selected.snapshot()["jobs"][0]["reasoning_effort"] == queued["reasoning_effort"] == "xhigh"
            exported = selected.export_history()
            assert exported["jobs"][0]["reasoning_effort"] == "xhigh"
            restored.restore_history(exported)
            historic = restored.snapshot()["jobs"][0]
            assert historic["reasoning_effort"] == "xhigh" and historic["configured_model"] == "codex-deep"
            assert historic["status"] == "failed" and restored_runner.calls == []
        finally:
            await selected.close()
            await restored.close()
    asyncio.run(scenario())


def test_absent_effort_keeps_strict_legacy_runner_signature_compatible(tmp_path):
    calls = []

    def legacy(provider, prompt, workspace, emit, cancel, *, command=None, timeout=300, model=None):
        calls.append(model)
        return {"status": "completed", "exit_code": 0}

    async def scenario():
        selected, _ = service(tmp_path, runner=legacy, profiles=reasoning_profiles())
        try:
            selected.start_job("codex", "Review.", "codex-no-effort")
            await until(lambda: selected.snapshot()["jobs"][0]["status"] == "completed")
            assert calls == ["codex-no-effort"]
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_ws_metadata_refresh_keeps_http_and_island_ticks_live(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        discovery_threads = []
        notifications = []
        calls = 0

        def discover(commands=None):
            nonlocal calls
            calls += 1
            if calls > 1:
                discovery_threads.append(threading.get_ident())
                entered.set()
                release.wait(3)
            return [{"id": "codex", "name": f"Codex discovery {calls}", "available": True,
                     "reason": "", "auth": "unknown", **reasoning_profiles()["codex"]}]

        runner = ControlledRunner()
        selected = MonitorService(tmp_path, runner=runner, discoverer=discover)
        selected.subscribe(lambda: notifications.append(threading.get_ident()))
        app = create_app(web_root=frontend(tmp_path), tick_seconds=0.02, monitor_service=selected)
        async with TestClient(TestServer(app)) as client:
            socket = await client.ws_connect("/ws/monitor")
            initial = await receive(socket)
            try:
                await socket.send_json({"type": "monitor_control", "action": "refresh"})
                assert await asyncio.to_thread(entered.wait, 1)
                assert len(discovery_threads) == 1 and discovery_threads[0] != loop_thread
                start_tick = app[RUNTIME_KEY].simulation.tick_count
                await until(lambda: app[RUNTIME_KEY].simulation.tick_count >= start_tick + 3)
                response = await asyncio.wait_for(client.get("/api/monitor"), timeout=1)
                during = await response.json()
                assert during["revision"] == initial["revision"]
                assert during["providers"][0]["name"] == "Codex discovery 1"
                assert during["jobs"] == [] and runner.calls == []
            finally:
                release.set()
            refreshed = await receive(socket, lambda state: state["revision"] > initial["revision"])
            assert refreshed["providers"][0]["name"] == "Codex discovery 2"
            assert notifications and all(identity == loop_thread for identity in notifications)
            await socket.close()
    asyncio.run(scenario())


def test_async_metadata_refreshes_are_serialized_before_applying_results(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        calls = 0
        active = 0
        maximum = 0
        counter_lock = threading.Lock()

        def discover(commands=None):
            nonlocal calls, active, maximum
            with counter_lock:
                calls += 1
                current = calls
                active += 1
                maximum = max(maximum, active)
            try:
                if current == 2:
                    entered.set()
                    release.wait(3)
                return [{"id": "codex", "name": f"Discovery {current}", "available": True,
                         "reason": "", "auth": "unknown", **reasoning_profiles()["codex"]}]
            finally:
                with counter_lock:
                    active -= 1

        selected = MonitorService(tmp_path, runner=ControlledRunner(), discoverer=discover)
        first = asyncio.create_task(selected.refresh_async())
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            second = asyncio.create_task(selected.refresh_async())
            await asyncio.sleep(0.03)
            assert calls == 2 and not second.done()
            release.set()
            await asyncio.gather(first, second)
            assert calls == 3 and maximum == 1
            assert selected.snapshot()["providers"][0]["name"] == "Discovery 3"
        finally:
            release.set()
            tasks = [first] + ([second] if second is not None else [])
            await asyncio.gather(*tasks, return_exceptions=True)
            await selected.close()
    asyncio.run(scenario())


def test_async_refresh_cannot_apply_after_service_closes(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        calls = 0

        def discover(commands=None):
            nonlocal calls
            calls += 1
            if calls > 1:
                entered.set()
                release.wait(3)
            return [{"id": "codex", "name": f"Discovery {calls}", "available": True,
                     "reason": "", "auth": "unknown", **reasoning_profiles()["codex"]}]

        selected = MonitorService(tmp_path, runner=ControlledRunner(), discoverer=discover)
        initial = selected.snapshot()
        task = asyncio.create_task(selected.refresh_async())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await selected.close()
            release.set()
            await task
            await selected.refresh_async()
            closed = selected.snapshot()
            assert closed["providers"] == initial["providers"]
            assert closed["revision"] == initial["revision"] and calls == 2
        finally:
            release.set()
            await task
            await selected.close()
    asyncio.run(scenario())


def test_ws_discovery_failure_reports_error_and_preserves_previous_metadata(tmp_path):
    async def scenario():
        calls = 0

        def discover(commands=None):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("synthetic metadata failure")
            return [{"id": "codex", "name": "Original metadata", "available": True,
                     "reason": "", "auth": "unknown", **reasoning_profiles()["codex"]}]

        selected = MonitorService(tmp_path, runner=ControlledRunner(), discoverer=discover)
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            socket = await client.ws_connect("/ws/monitor")
            initial = await receive(socket)
            await socket.send_json({"type": "monitor_control", "action": "refresh"})
            error = await asyncio.wait_for(socket.receive_json(), timeout=1)
            assert error["type"] == "monitor_error" and "discovery failed" in error["message"]
            current = await (await client.get("/api/monitor")).json()
            assert current["providers"] == initial["providers"]
            await socket.close()
    asyncio.run(scenario())


def install_coordinator(monkeypatch, run):
    class Coordinator:
        def __init__(self, selected):
            self.service = selected

        async def run(self, parent_id):
            return await run(self.service, parent_id)
    monkeypatch.setitem(sys.modules, "collaboration_service", types.SimpleNamespace(CollaborationCoordinator=Coordinator))


def artifact_for(parent_id, *, applied=False):
    base = f"/api/monitor/jobs/{parent_id}"
    return {"id": parent_id, "changed_files": [{"path": "src/example.py", "status": "modified", "additions": 2, "deletions": 1}],
            "diff_url": base + "/changes", "download_url": base + "/patch", "applied": applied}


class TeamRunner:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.active = 0
        self.lock = threading.Lock()

    def __call__(self, provider, prompt, workspace, emit, cancel, **options):
        with self.lock:
            self.calls.append((provider, prompt, workspace, options))
            self.active += 1
        try:
            if options.get("output_schema") is not None:
                output = json.dumps({"summary": "A public plan.", "details": "PLAN_PROTOCOL_BODY" + "x" * 12000})
                emit({"kind": "message", "text": output})
                emit({"kind": "result", "text": output, "public_output": True})
            else:
                output = f"{provider} finished delegated work."
                emit({"kind": "message", "text": output})
            emit({"kind": "tool", "tool": "read_file", "status": "running", "text": "Inspecting source."})
            while not self.release.wait(0.005):
                if cancel.is_set():
                    return {"status": "cancelled", "exit_code": None, "output": ""}
            return {"status": "completed", "exit_code": 0, "output": output, "model": "actual-test-model"}
        finally:
            with self.lock:
                self.active -= 1


def test_collaboration_reserves_members_atomically_and_does_not_fake_auth(tmp_path, monkeypatch):
    async def scenario():
        release = asyncio.Event()

        async def coordinate(selected, parent_id):
            selected.update_team(parent_id, phase="planning", round=1)
            await release.wait()
            return {"status": "completed", "output": "Team summary."}

        install_coordinator(monkeypatch, coordinate)
        selected, runner = service(tmp_path, profiles=reasoning_profiles())
        try:
            job = selected.start_job("codex", "Collaborate.", collaboration={"members": [{"provider": "claude"}, {"provider": "antigravity"}]})
            assert job["kind"] == "collaboration" and job["parent_job_id"] is None
            assert [item["provider"] for item in job["collaboration"]["members"]] == ["codex", "claude", "antigravity"]
            assert job["collaboration"]["max_rounds"] == 4
            assert all(item["team_run_id"] == item["active_job_id"] == job["id"] for item in selected.snapshot()["providers"])
            with pytest.raises(MonitorError, match="active"):
                selected.start_job("claude", "A conflicting solo task.")
            with pytest.raises(MonitorError, match="active"):
                selected.start_job("antigravity", "Another team.", collaboration={"members": [{"provider": "codex"}]})
            release.set()
            await until(lambda: selected.get_job(job["id"])["status"] == "completed")
            assert selected.get_job(job["id"])["collaboration"]["phase"] == "completed"
            assert all(item["team_run_id"] is None and item["active_job_id"] is None for item in selected.snapshot()["providers"])
            assert all(item["auth"] == "unknown" for item in selected.snapshot()["providers"])
            assert runner.calls == []
        finally:
            release.set()
            await selected.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("collaboration", [
    {}, [], {"members": []}, {"members": [{"provider": "codex"}]},
    {"members": [{"provider": "claude"}, {"provider": "claude"}]},
    {"members": [{"provider": "claude"}, {"provider": "antigravity"}, {"provider": "codex"}]},
    {"members": [{"provider": "unknown"}]}, {"members": [{"provider": "claude", "command": "arbitrary.exe"}]},
    {"members": [{"provider": "claude", "model": "wrong-model"}]},
    {"members": [{"provider": "claude", "reasoning_effort": "high"}]},
    {"members": [{"provider": "claude"}], "workspace": "C:/"},
])
def test_invalid_collaboration_has_no_partial_job_or_reservations(tmp_path, collaboration):
    async def scenario():
        selected, runner = service(tmp_path, profiles=reasoning_profiles())
        try:
            with pytest.raises(MonitorError):
                selected.start_job("codex", "Collaborate.", collaboration=collaboration)
            assert selected.snapshot()["jobs"] == [] and runner.calls == []
            assert all(item["active_job_id"] is None and item["team_run_id"] is None for item in selected.snapshot()["providers"])
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_missing_or_busy_peer_cannot_be_reserved_by_a_new_team(tmp_path):
    async def scenario():
        selected, runner = service(tmp_path, available=("codex", "claude"), profiles=reasoning_profiles())
        try:
            with pytest.raises(MonitorError):
                selected.start_job("codex", "Collaborate.", collaboration={"members": [{"provider": "antigravity"}]})
            selected.start_job("claude", "Already busy.")
            with pytest.raises(MonitorError, match="active"):
                selected.start_job("codex", "Collaborate.", collaboration={"members": [{"provider": "claude"}]})
            assert len(selected.snapshot()["jobs"]) == 1
            assert selected.snapshot()["providers"][0]["active_job_id"] is None
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_team_turns_keep_frozen_choices_shared_copy_and_public_planner_summary(tmp_path, monkeypatch):
    async def scenario():
        project = tmp_path / "original-project"
        project.mkdir()
        shared = tmp_path / "shared-copy"
        shared.mkdir()
        profiles = copy.deepcopy(reasoning_profiles())
        commands = {provider: f"original-{provider}.exe" for provider in PROFILES}
        runner = TeamRunner()
        runner.release.set()
        turn_results = []

        def discover(overrides=None):
            return [{"id": provider, "name": provider, "available": True, "reason": "", "auth": "unknown",
                     "command": commands[provider], **profiles[provider]} for provider in PROFILES]

        async def coordinate(selected, parent_id):
            private = selected.team_settings(parent_id)
            assert private["lead"] == "codex" and private["prompt"] == "Original team request."
            assert private["members"]["codex"]["model"] == "codex-deep"
            selected.update_team(parent_id, phase="planning", round=1, summary="Plan api_key=sk-proj-synthetic_secret_value_123456")
            turn_results.append(await selected.run_team_turn(parent_id, "codex", "Plan.", role="planning", workspace=shared,
                                                            output_schema={"type": "object"}))
            assert "PLAN_PROTOCOL_BODY" in turn_results[-1]["output"]
            assert selected._providers["codex"]["active_job_id"] == parent_id
            assert selected._providers["claude"]["team_run_id"] == parent_id
            with pytest.raises(MonitorError, match="active"):
                selected.start_job("codex", "Cannot take an unrelated turn.")
            turn_results.append(await selected.run_team_turn(parent_id, "claude", "Implement.", role="worker", task_mode="write", workspace=shared))
            turn_results.append(await selected.run_team_turn(parent_id, "codex", "Review.", role="synthesis", workspace=shared))
            return {"status": "completed", "output": "Combined public result.", "artifact": artifact_for(parent_id)}

        install_coordinator(monkeypatch, coordinate)
        selected = MonitorService(project, runner=runner, discoverer=discover)
        try:
            parent = selected.start_job("codex", "Original team request.", "codex-deep", "xhigh",
                                        {"members": [{"provider": "claude", "model": OPUS}]})
            commands.update({provider: f"replacement-{provider}.exe" for provider in PROFILES})
            profiles["codex"]["configured_model"] = "codex-small"
            selected.refresh()
            await until(lambda: selected.get_job(parent["id"])["status"] == "completed")
            state = selected.snapshot()
            parent = selected.get_job(parent["id"])
            children = [job for job in state["jobs"] if job["parent_job_id"] == parent["id"]]
            assert len(children) == 3 and all(job["kind"] == "delegation" for job in children)
            assert {job["role"] for job in children} == {"planning", "worker", "synthesis"}
            assert parent["latest_message"] == "Combined public result." and parent["artifact"]["id"] == parent["id"]
            assert all(call[2] == shared.resolve() and call[3]["shared_workspace"] for call in runner.calls)
            assert [call[3]["execution_mode"] for call in runner.calls] == ["read_only", "workspace_write", "read_only"]
            assert [call[3]["command"] for call in runner.calls] == ["original-codex.exe", "original-claude.exe", "original-codex.exe"]
            assert runner.calls[0][3]["model"] == "codex-deep" and runner.calls[0][3]["reasoning_effort"] == "xhigh"
            assert runner.calls[1][3]["model"] == OPUS
            planning = next(job for job in children if job["role"] == "planning")
            assert planning["latest_message"] == "A public plan."
            assert "PLAN_PROTOCOL_BODY" not in json.dumps(state)
            assert "synthetic_secret" not in json.dumps(state)
            assert not any(event["kind"] == "message" for event in planning["events"])
            exported = selected.export_history()
            assert "original-codex.exe" not in json.dumps(exported)
            restored, restored_runner = service(project)
            try:
                restored.restore_history(exported)
                assert restored.get_job(parent["id"])["artifact"] == parent["artifact"]
                assert len([job for job in restored.snapshot()["jobs"] if job["parent_job_id"] == parent["id"]]) == 3
                assert restored_runner.calls == [] and restored._teams == {}
            finally:
                await restored.close()
        finally:
            await selected.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stop_mode", ["parent", "child", "shutdown"])
def test_cancelling_any_team_job_stops_all_owned_children_and_future_turns(tmp_path, monkeypatch, stop_mode):
    async def scenario():
        project = tmp_path / "original-project"
        project.mkdir()
        shared = tmp_path / "shared-copy"
        shared.mkdir()
        runner = TeamRunner()

        async def coordinate(selected, parent_id):
            turns = await asyncio.gather(
                selected.run_team_turn(parent_id, "codex", "Lead task.", role="worker", workspace=shared),
                selected.run_team_turn(parent_id, "claude", "Peer task.", role="worker", workspace=shared))
            if selected.team_cancel_event(parent_id).is_set():
                return {"status": "cancelled", "output": "Stopped."}
            await selected.run_team_turn(parent_id, "codex", "Must not start after cancellation.", role="synthesis", workspace=shared)
            return {"status": "completed", "output": str(turns)}

        install_coordinator(monkeypatch, coordinate)
        selected, _ = service(project, runner=runner, profiles=reasoning_profiles())
        try:
            parent = selected.start_job("codex", "Collaborate.", collaboration={"members": [{"provider": "claude"}]})
            await until(lambda: len(runner.calls) == 2 and runner.active == 2)
            if stop_mode == "shutdown":
                await selected.close()
            else:
                target = parent["id"] if stop_mode == "parent" else next(
                    job["id"] for job in selected.snapshot()["jobs"] if job["kind"] == "delegation" and job["provider"] == "claude")
                selected.cancel_job(target)
            await until(lambda: selected.get_job(parent["id"])["status"] == "cancelled")
            assert runner.active == 0 and len(runner.calls) == 2
            assert all(item["team_run_id"] is None and item["active_job_id"] is None for item in selected.snapshot()["providers"])
            assert selected.get_job(parent["id"])["collaboration"]["phase"] == "cancelled"
        finally:
            await selected.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("transport", ["http", "ws"])
def test_collaboration_controls_accept_selected_members_and_models(tmp_path, monkeypatch, transport):
    async def scenario():
        async def coordinate(selected, parent_id):
            return {"status": "completed", "output": "Reviewed team result."}

        install_coordinator(monkeypatch, coordinate)
        selected, runner = service(tmp_path, profiles=reasoning_profiles())
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            payload = {"provider": "codex", "prompt": "Collaborate.", "model": "codex-deep", "reasoning_effort": "xhigh",
                       "collaboration": {"members": [{"provider": "claude", "model": OPUS}]}}
            if transport == "http":
                response = await client.post("/api/monitor/jobs", json=payload)
                assert response.status == 202
                job = (await response.json())["job"]
            else:
                socket = await client.ws_connect("/ws/monitor")
                await receive(socket)
                await socket.send_json({"type": "monitor_control", "action": "start", **payload})
                state = await receive(socket, lambda state: bool(state["jobs"]))
                job = state["jobs"][0]
                await socket.close()
            assert job["kind"] == "collaboration"
            assert job["collaboration"]["members"] == [
                {"provider": "codex", "model": "codex-deep", "reasoning_effort": "xhigh"},
                {"provider": "claude", "model": OPUS, "reasoning_effort": None}]
            assert runner.calls == []
    asyncio.run(scenario())


def test_team_turns_reject_original_or_different_copies_and_overlapping_member_turns(tmp_path, monkeypatch):
    async def scenario():
        project = tmp_path / "original-project"
        project.mkdir()
        nested = project / "nested-copy"
        nested.mkdir()
        shared, other = tmp_path / "shared-copy", tmp_path / "other-copy"
        shared.mkdir()
        other.mkdir()
        runner = TeamRunner()

        async def coordinate(selected, parent_id):
            for unsafe in (project, nested, tmp_path):
                with pytest.raises(MonitorError, match="isolated"):
                    await selected.run_team_turn(parent_id, "codex", "Unsafe original.", role="worker", task_mode="write", workspace=unsafe)
            assert runner.calls == []
            assert not any(job["kind"] == "delegation" for job in selected.snapshot()["jobs"])
            first = asyncio.create_task(selected.run_team_turn(parent_id, "codex", "First turn.", role="worker", workspace=shared))
            await until(lambda: bool(runner.calls))
            with pytest.raises(MonitorError, match="active turn"):
                await selected.run_team_turn(parent_id, "codex", "Overlapping turn.", role="worker", workspace=shared)
            with pytest.raises(MonitorError, match="same isolated"):
                await selected.run_team_turn(parent_id, "claude", "Different copy.", role="worker", workspace=other)
            selected.cancel_job(parent_id)
            assert (await first)["status"] == "cancelled"
            cancelled = await selected.run_team_turn(parent_id, "claude", "No next turn.", role="worker", workspace=shared)
            assert cancelled["status"] == "cancelled" and cancelled["job_id"] is None
            return {"status": "cancelled", "output": "Stopped."}

        install_coordinator(monkeypatch, coordinate)
        selected, _ = service(project, runner=runner, profiles=reasoning_profiles())
        try:
            parent = selected.start_job("codex", "Collaborate.", collaboration={"members": [{"provider": "claude"}]})
            await until(lambda: selected.get_job(parent["id"])["status"] == "cancelled")
            assert len(runner.calls) == 1
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_parent_keeps_reservations_until_unfinished_owned_child_exits(tmp_path, monkeypatch):
    async def scenario():
        project = tmp_path / "original-project"
        project.mkdir()
        shared = tmp_path / "shared-copy"
        shared.mkdir()
        started, stopping, allow_exit = threading.Event(), threading.Event(), threading.Event()
        wrappers = []

        def runner(provider, prompt, workspace, emit, cancel, **options):
            started.set()
            cancel.wait(3)
            stopping.set()
            allow_exit.wait(3)
            return {"status": "cancelled", "exit_code": None}

        async def coordinate(selected, parent_id):
            wrappers.append(asyncio.create_task(selected.run_team_turn(parent_id, "claude", "Outstanding work.", role="worker", workspace=shared)))
            assert await asyncio.to_thread(started.wait, 1)
            return {"status": "completed", "output": "Coordinator returned too early."}

        install_coordinator(monkeypatch, coordinate)
        selected, _ = service(project, runner=runner, profiles=reasoning_profiles())
        try:
            parent = selected.start_job("codex", "Collaborate.", collaboration={"members": [{"provider": "claude"}]})
            assert await asyncio.to_thread(stopping.wait, 1)
            assert selected.get_job(parent["id"])["status"] == "running"
            assert all(item["team_run_id"] == parent["id"] for item in selected.snapshot()["providers"][:2])
            allow_exit.set()
            await until(lambda: selected.get_job(parent["id"])["status"] == "failed")
            assert "delegated work" in selected.get_job(parent["id"])["error"]
            assert all(item["team_run_id"] is None for item in selected.snapshot()["providers"])
            await asyncio.gather(*wrappers)
        finally:
            allow_exit.set()
            await selected.close()
    asyncio.run(scenario())


def test_artifact_review_download_and_explicit_apply_are_gated_and_persisted(tmp_path, monkeypatch):
    async def scenario():
        release = asyncio.Event()
        operations = []
        conflict = [False]
        loop_thread = threading.get_ident()

        class ArtifactError(Exception):
            def __init__(self, message, status):
                super().__init__(message)
                self.status = status

        class ArtifactStore:
            def __init__(self, workspace):
                assert workspace == tmp_path.resolve()

            def review(self, job_id):
                operations.append(("review", threading.get_ident()))
                artifact = artifact_for(job_id)
                artifact["changed_files"] += [{"path": "../escape", "status": "modified", "additions": 1, "deletions": 0},
                                              {"path": "C:/absolute", "status": "modified", "additions": 1, "deletions": 0}]
                artifact["private_manifest"] = "must not be returned"
                return {"artifact": artifact, "diff": "--- a/src/example.py\n+++ b/src/example.py\n"}

            def patch(self, job_id):
                operations.append(("patch", threading.get_ident()))
                return "diff --git a/src/example.py b/src/example.py\n"

            def apply(self, job_id):
                operations.append(("apply", threading.get_ident()))
                if conflict[0]:
                    raise ArtifactError("Original file changed since review.", 409)
                return artifact_for(job_id, applied=True)

        monkeypatch.setitem(sys.modules, "collaboration_workspace", types.SimpleNamespace(ArtifactError=ArtifactError, ArtifactStore=ArtifactStore))

        async def coordinate(selected, parent_id):
            selected.update_team(parent_id, phase="reviewing", artifact=artifact_for(parent_id))
            await release.wait()
            return {"status": "completed", "output": "Review before applying.", "artifact": artifact_for(parent_id)}

        install_coordinator(monkeypatch, coordinate)
        selected, _ = service(tmp_path, profiles=reasoning_profiles())
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            response = await client.post("/api/monitor/jobs", json={"provider": "codex", "prompt": "Collaborate.",
                                                                   "collaboration": {"members": [{"provider": "claude"}]}})
            parent_id = (await response.json())["job"]["id"]
            await until(lambda: "artifact" in selected.get_job(parent_id))
            base = f"/api/monitor/jobs/{parent_id}"
            early = await client.post(base + "/apply", json={})
            assert early.status == 409 and not any(item[0] == "apply" for item in operations)
            review = await (await client.get(base + "/changes")).json()
            assert review["artifact"]["changed_files"] == artifact_for(parent_id)["changed_files"]
            assert "private_manifest" not in review["artifact"] and review["diff"].startswith("---")
            patch = await client.get(base + "/patch")
            assert patch.status == 200 and patch.headers["Content-Type"].startswith("text/x-diff")
            assert "attachment" in patch.headers["Content-Disposition"]
            release.set()
            await until(lambda: selected.get_job(parent_id)["status"] == "completed")
            foreign = await client.post(base + "/apply", json={}, headers={"Origin": "https://foreign.example"})
            assert foreign.status == 403
            arbitrary = await client.post(base + "/apply", json={"workspace": "C:/"})
            assert arbitrary.status == 400
            unknown = await client.post("/api/monitor/jobs/unknown/apply", json={})
            assert unknown.status == 404
            conflict[0] = True
            assert (await client.post(base + "/apply", json={})).status == 409
            assert selected.get_job(parent_id)["artifact"]["applied"] is False
            conflict[0] = False
            applied = await client.post(base + "/apply", json={})
            assert applied.status == 200 and (await applied.json())["artifact"]["applied"] is True
            assert selected.export_history()["jobs"][0]["artifact"]["applied"] is True
            assert all(identity != loop_thread for _, identity in operations)
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["changes", "patch", "apply"])
def test_artifact_store_constructor_errors_return_friendly_conflict(tmp_path, monkeypatch, action):
    class ArtifactError(Exception):
        status = 409

    class ArtifactStore:
        def __init__(self, workspace):
            raise ArtifactError("Artifact storage points outside the permitted workspace.")

    monkeypatch.setitem(sys.modules, "collaboration_workspace", types.SimpleNamespace(ArtifactError=ArtifactError, ArtifactStore=ArtifactStore))

    async def scenario():
        selected, runner = service(tmp_path)
        parent_id = "saved-parent"
        selected.restore_history({"version": 1, "events": [], "jobs": [{
            "id": parent_id, "provider": "codex", "kind": "collaboration", "status": "completed",
            "created_at": "2026-10-25T00:00:00.000Z", "prompt": "Review changes.",
            "collaboration": {"members": [{"provider": "codex"}, {"provider": "claude"}], "phase": "completed"},
            "artifact": artifact_for(parent_id),
        }]})
        async with TestClient(TestServer(app_for(tmp_path, selected))) as client:
            path = f"/api/monitor/jobs/{parent_id}/{action}"
            response = await client.post(path, json={}) if action == "apply" else await client.get(path)
            assert response.status == 409
            error = await response.json()
            assert error["type"] == "monitor_error" and "outside" in error["message"]
            assert runner.calls == []
    asyncio.run(scenario())


def test_team_turn_forwards_non_native_schema_without_exposing_planner_json(tmp_path, monkeypatch):
    async def scenario():
        project, shared = tmp_path / "project", tmp_path / "shared-copy"
        project.mkdir()
        shared.mkdir()
        runner = TeamRunner()
        runner.release.set()
        schema = {"type": "object", "required": ["summary"]}

        async def coordinate(selected, parent_id):
            with pytest.raises(MonitorError, match="boolean"):
                await selected.run_team_turn(parent_id, "claude", "Plan.", role="planning", workspace=shared,
                                             output_schema=schema, native_schema=0)
            assert runner.calls == []
            result = await selected.run_team_turn(parent_id, "claude", "Plan.", role="planning", workspace=shared,
                                                  output_schema=schema, native_schema=False)
            assert "PLAN_PROTOCOL_BODY" in result["output"]
            return {"status": "completed", "output": "Validated public summary."}

        install_coordinator(monkeypatch, coordinate)
        selected, _ = service(project, runner=runner)
        try:
            parent = selected.start_job("claude", "Collaborate.", collaboration={"members": [{"provider": "codex"}]})
            await until(lambda: selected.get_job(parent["id"])["status"] == "completed")
            options = runner.calls[0][3]
            assert options["native_schema"] is False and options["output_schema"] == schema
            assert options["execution_mode"] == "read_only" and options["shared_workspace"] is True
            child = next(job for job in selected.snapshot()["jobs"] if job["kind"] == "delegation")
            assert child["latest_message"] == "A public plan."
            assert "PLAN_PROTOCOL_BODY" not in json.dumps(selected.snapshot())
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_proposal_write_turn_keeps_native_cli_read_only_and_preserves_history_strategy(tmp_path, monkeypatch):
    async def scenario():
        project, shared = tmp_path / "project", tmp_path / "shared-copy"
        project.mkdir()
        shared.mkdir()
        runner = TeamRunner()
        runner.release.set()
        schema = {"type": "object", "required": ["summary", "changes"]}

        async def coordinate(selected, parent_id):
            with pytest.raises(MonitorError, match="boolean"):
                await selected.run_team_turn(parent_id, "codex", "Propose files.", role="worker", task_mode="write",
                                             output_schema=schema, proposal_mode="true", workspace=shared)
            with pytest.raises(MonitorError, match="write task"):
                await selected.run_team_turn(parent_id, "codex", "Propose files.", role="worker", proposal_mode=True,
                                             output_schema=schema, workspace=shared)
            assert runner.calls == []
            result = await selected.run_team_turn(parent_id, "codex", "Propose files.", role="worker", task_mode="write",
                                                  output_schema=schema, proposal_mode=True, workspace=shared)
            assert "PLAN_PROTOCOL_BODY" in result["output"]
            return {"status": "completed", "output": "Public proposal summary."}

        install_coordinator(monkeypatch, coordinate)
        selected, _ = service(project, runner=runner)
        restored, restored_runner = service(project)
        try:
            parent = selected.start_job("claude", "Collaborate.", collaboration={"members": [{"provider": "codex"}]})
            await until(lambda: selected.get_job(parent["id"])["status"] == "completed")
            child = next(job for job in selected.snapshot()["jobs"] if job["kind"] == "delegation")
            assert child["task_mode"] == "write" and child["write_strategy"] == "proposals"
            assert child["execution_mode"] == "read_only" and child["latest_message"] == "A public plan."
            options = runner.calls[0][3]
            assert options["execution_mode"] == "read_only" and options["output_schema"] == schema
            assert options["shared_workspace"] is True and "proposal_mode" not in options
            assert "PLAN_PROTOCOL_BODY" not in json.dumps(selected.snapshot())
            restored.restore_history(selected.export_history())
            saved_child = restored.get_job(child["id"])
            assert saved_child["write_strategy"] == "proposals" and saved_child["execution_mode"] == "read_only"
            assert saved_child["task_mode"] == "write" and restored_runner.calls == []
        finally:
            await selected.close()
            await restored.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("validated_status", ["completed", "failed"])
def test_host_validated_team_outcome_updates_report_without_releasing_or_revoking_auth(tmp_path, monkeypatch, validated_status):
    async def scenario():
        project, shared = tmp_path / "project", tmp_path / "shared-copy"
        project.mkdir()
        shared.mkdir()
        runner = TeamRunner()
        runner.release.set()
        child_ids = []
        report = "已檢查提供的來源檔。\n發現：入口缺少參數驗證。\n限制：未執行測試。"
        rejection = "Report incomplete api_key=sk-proj-synthetic_secret_value_123456" if validated_status == "failed" else None

        async def coordinate(selected, parent_id):
            native = await selected.run_team_turn(parent_id, "claude", "Review provided source.", role="worker", workspace=shared)
            child_id = native["job_id"]
            child_ids.append(child_id)
            before = selected.get_job(child_id)
            assert before["status"] == "completed"
            provider = next(item for item in selected.snapshot()["providers"] if item["id"] == "claude")
            assert provider["auth"] == "verified" and provider["team_run_id"] == parent_id
            selected.record_team_outcome(parent_id, child_id, status=validated_status, output=report, error=rejection)
            after = selected.get_job(child_id)
            assert after["status"] == validated_status and after["latest_message"] == report
            assert after["finished_at"] == before["finished_at"] and after["elapsed_seconds"] == before["elapsed_seconds"]
            provider = next(item for item in selected.snapshot()["providers"] if item["id"] == "claude")
            assert provider["auth"] == "verified" and provider["team_run_id"] == provider["active_job_id"] == parent_id
            assert provider["phase"] == "running"
            assert after["events"][-1]["kind"] == "result" and after["events"][-1]["status"] == validated_status
            if rejection:
                assert "[REDACTED]" in after["error"] and "synthetic_secret" not in after["error"]
            return {"status": validated_status, "output": "Validated parent result.", "error": "A worker report was incomplete." if rejection else None}

        install_coordinator(monkeypatch, coordinate)
        selected, _ = service(project, runner=runner)
        try:
            parent = selected.start_job("codex", "Collaborate.", collaboration={"members": [{"provider": "claude"}]})
            await until(lambda: selected.get_job(parent["id"])["status"] in ("completed", "failed"))
            assert selected.get_job(parent["id"])["status"] == validated_status
            assert len(runner.calls) == 1
            exported_child = next(job for job in selected.export_history()["jobs"] if job["id"] == child_ids[0])
            assert exported_child["status"] == validated_status and exported_child["latest_message"] == report
            with pytest.raises(MonitorError, match="active collaboration"):
                selected.record_team_outcome(parent["id"], child_ids[0], status="completed", output="Too late.")
        finally:
            await selected.close()
    asyncio.run(scenario())
