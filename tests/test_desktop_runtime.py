"""Real local listener lifecycle checks without a browser or model request."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientSession, web

from desktop_runtime import DesktopServer
from monitor_server import register_monitor
from monitor_service import MonitorError, MonitorService

HISTORY_TIME = "2026-10-25T02:03:04.000Z"
INTERRUPTED = "上次關閉時仍在執行，已中止；請手動重新交辦。"


def fake_discovery(commands=None):
    return [{"id": provider, "name": provider.title(), "available": True, "auth": "unknown",
             "command": "fake-cli.exe", "transport": "codex" if provider == "codex" else "antigravity",
             "configured_model": "claude-sonnet-4-6" if provider == "claude" else "test-model",
             "model_options": [], "reason": ""} for provider in ("codex", "claude", "antigravity")]


def history_job(job_id="saved-job", status="completed"):
    return {"id": job_id, "provider": "claude", "prompt": "Review source.", "status": status,
            "created_at": HISTORY_TIME, "started_at": HISTORY_TIME,
            "finished_at": HISTORY_TIME if status == "completed" else None,
            "transport": "antigravity", "configured_model": "claude-sonnet-4-6",
            "model": "reported-sonnet", "session_id": "saved-session", "latest_message": "Public result.",
            "active_tool": "read_file", "usage": {"input_tokens": 12, "output_tokens": 8},
            "error": None, "exit_code": 0, "cancel_requested": False, "elapsed_seconds": 2.5, "events": []}


def monitor_app_factory(web_root, calls):
    def runner(provider, prompt, workspace, emit, cancel, **options):
        calls.append((provider, prompt))
        emit({"kind": "message", "text": "Public result with api_key=sk-proj-synthetic_secret_value_123456"})
        time.sleep(0.02)
        return {"status": "completed", "exit_code": 0, "model": "reported-sonnet", "session_id": "saved-session"}

    def build(**options):
        selected = MonitorService(options["monitor_workspace"], runner=runner, discoverer=fake_discovery)
        app = web.Application()
        register_monitor(app, workspace=options["monitor_workspace"], web_root=web_root, service=selected)
        return app
    return build


def post_json(url, data):
    async def request():
        async with ClientSession() as client:
            async with client.post(url, json=data) as response:
                assert response.status == 202
                return await response.json()
    return asyncio.run(request())


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Expected desktop runtime state did not arrive.")


def app_factory(records):
    def build(**options):
        records.setdefault("factories", []).append((threading.get_ident(), options))
        app = web.Application()

        async def monitor(request):
            return web.json_response({"ready": True, "workspace": str(options["monitor_workspace"])})

        async def cleanup(application):
            await asyncio.sleep(0.01)
            records.setdefault("cleanups", []).append(threading.get_ident())

        app.router.add_get("/monitor", monitor)
        app.on_cleanup.append(cleanup)
        return app
    return build


def request_json(url):
    async def request():
        async with ClientSession() as client:
            async with client.get(url) as response:
                assert response.status == 200
                return await response.json()
    return asyncio.run(request())


def test_owned_ephemeral_server_is_ready_and_cleans_up_off_ui_thread(tmp_path):
    records = {}
    ui_thread = threading.get_ident()
    data = tmp_path / "desktop-data"
    server = DesktopServer(tmp_path, data, app_factory=app_factory(records), app_kwargs={"seed": 42})
    try:
        url = server.start()
        assert url == server.url and server.running and server.error is None
        assert urlsplit(url).hostname == "127.0.0.1"
        assert urlsplit(url).port > 0 and urlsplit(url).path == "/monitor"
        assert request_json(url) == {"ready": True, "workspace": str(tmp_path.resolve())}
        assert data.is_dir()
        worker, options = records["factories"][0]
        assert worker != ui_thread and options["brain"] == "demo" and options["seed"] == 42
        assert options["monitor_workspace"] == tmp_path.resolve()
        assert server.start() == url and len(records["factories"]) == 1
    finally:
        server.stop()
    assert not server.running and server.url is None and server.error is None
    assert records["cleanups"] == [worker]
    server.stop()


def test_stopping_owned_server_does_not_touch_another_listener(tmp_path):
    first = DesktopServer(tmp_path, tmp_path / "first", app_factory=app_factory({}))
    second = DesktopServer(tmp_path, tmp_path / "second", app_factory=app_factory({}))
    try:
        first_url, second_url = first.start(), second.start()
        assert urlsplit(first_url).port != urlsplit(second_url).port
        first.stop()
        assert request_json(second_url)["ready"] and second.running
    finally:
        first.stop()
        second.stop()


def test_same_controller_can_restart_with_a_fresh_app(tmp_path):
    records = {}
    server = DesktopServer(tmp_path, tmp_path / "data", app_factory=app_factory(records))
    try:
        assert request_json(server.start())["ready"]
        server.stop()
        assert request_json(server.start())["ready"]
        assert len(records["factories"]) == 2
    finally:
        server.stop()
    assert len(records["cleanups"]) == 2


def test_concurrent_start_calls_share_one_backend(tmp_path):
    records = {}
    server = DesktopServer(tmp_path, tmp_path / "data", app_factory=app_factory(records))
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            urls = list(executor.map(lambda _: server.start(), range(2)))
        assert urls[0] == urls[1]
        assert len(records["factories"]) == 1 and request_json(urls[0])["ready"]
    finally:
        server.stop()


def test_startup_failure_is_reported_and_controller_can_retry(tmp_path):
    records = {}

    def retryable(**options):
        if not records.get("failed"):
            records["failed"] = True
            raise OSError("synthetic listener failure")
        return app_factory(records)(**options)

    server = DesktopServer(tmp_path, tmp_path / "data", app_factory=retryable)
    with pytest.raises(RuntimeError, match="synthetic listener failure"):
        server.start()
    assert "OSError" in server.error and not server.running
    try:
        assert request_json(server.start())["ready"]
        assert server.error is None
    finally:
        server.stop()


def test_port_conflict_does_not_reuse_or_stop_existing_server(tmp_path):
    owner = DesktopServer(tmp_path, tmp_path / "owner", app_factory=app_factory({}))
    contender = None
    try:
        owned_url = owner.start()
        contender = DesktopServer(tmp_path, tmp_path / "contender", port=urlsplit(owned_url).port,
                                  app_factory=app_factory({}))
        with pytest.raises(RuntimeError, match="OSError"):
            contender.start()
        assert not contender.running
        assert request_json(owned_url)["ready"] and owner.running
    finally:
        if contender:
            contender.stop()
        owner.stop()


def test_startup_timeout_requests_cleanup_without_leaving_a_listener(tmp_path):
    entered, release = threading.Event(), threading.Event()
    records = {}

    def delayed(**options):
        entered.set()
        release.wait(2)
        return app_factory(records)(**options)

    server = DesktopServer(tmp_path, tmp_path / "data", app_factory=delayed)
    try:
        with pytest.raises(TimeoutError, match="did not start"):
            server.start(timeout=0.03)
        assert entered.is_set() and server.error is not None
        release.set()
        server.stop(timeout=2)
        assert not server.running and server.url is None
        assert len(records["cleanups"]) == 1
    finally:
        release.set()
        server.stop(timeout=3)


def test_default_app_serves_monitor_without_starting_model_jobs(tmp_path):
    server = DesktopServer(tmp_path, tmp_path / "data", app_kwargs={"start_loop": False})
    try:
        url = server.start()
        base = url.rsplit("/", 1)[0]
        state = request_json(base + "/api/monitor")
        assert state["type"] == "monitor_state" and state["jobs"] == []
        assert state["workspace"] == str(tmp_path.resolve())
        assert len(state["providers"]) == 3
        island = request_json(base + "/api/state")
        assert island["brain"] == "demo" and island["tick"] == 0
    finally:
        server.stop()


def test_workspace_cannot_be_replaced_by_app_kwargs(tmp_path):
    records = {}
    server = DesktopServer(tmp_path, tmp_path / "data", app_factory=app_factory(records),
                           app_kwargs={"monitor_workspace": tmp_path / "other"})
    try:
        assert request_json(server.start())["workspace"] == str(tmp_path.resolve())
    finally:
        server.stop()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_lifecycle_timeouts_fail_without_starting_threads(tmp_path, timeout):
    server = DesktopServer(tmp_path, tmp_path / "data", app_factory=app_factory({}))
    with pytest.raises(ValueError, match="Timeout"):
        server.start(timeout)
    with pytest.raises(ValueError, match="Timeout"):
        server.stop(timeout)
    assert not server.running


@pytest.mark.parametrize("options", [{"host": "0.0.0.0"}, {"host": "example.com"},
                                     {"port": -1}, {"port": 65536}, {"port": True}])
def test_desktop_listener_configuration_is_local_and_valid(tmp_path, options):
    with pytest.raises(ValueError):
        DesktopServer(tmp_path, tmp_path / "data", **options)


def test_desktop_public_history_is_saved_during_run_and_restored_without_jobs(tmp_path):
    calls = []
    data = tmp_path / "data"
    factory = monitor_app_factory(tmp_path, calls)
    raw_prompt = "Review source api_key=sk-proj-synthetic_secret_value_123456"
    first = DesktopServer(tmp_path, data, app_factory=factory)
    try:
        base = first.start().rsplit("/", 1)[0]
        post_json(base + "/api/monitor/jobs", {"provider": "claude", "prompt": raw_prompt})
        wait_for(lambda: request_json(base + "/api/monitor")["jobs"][0]["status"] == "completed")
        saved = wait_for(lambda: json.loads((data / "history.json").read_text(encoding="utf-8"))
                         if (data / "history.json").is_file() else None)
        assert saved["jobs"][0]["status"] == "completed"
        assert calls == [("claude", raw_prompt)]
    finally:
        first.stop()
    contents = (data / "history.json").read_text(encoding="utf-8")
    assert "synthetic_secret" not in contents and "[REDACTED]" in contents
    assert "command_key" not in contents and "fake-cli.exe" not in contents
    assert not list(data.glob(".history-*.tmp"))
    second = DesktopServer(tmp_path, data, app_factory=factory)
    try:
        base = second.start().rsplit("/", 1)[0]
        restored = request_json(base + "/api/monitor")
        job = restored["jobs"][0]
        assert job["status"] == "completed" and job["model"] == "reported-sonnet"
        assert job["configured_model"] == "claude-sonnet-4-6" and job["transport"] == "antigravity"
        assert job["session_id"] == "saved-session" and job["elapsed_seconds"] > 0
        assert all(item["auth"] == "unknown" and item["active_job_id"] is None for item in restored["providers"])
        assert calls == [("claude", raw_prompt)]
    finally:
        second.stop()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_desktop_interrupted_history_is_failed_without_automatic_retry(tmp_path, status):
    calls = []
    data = tmp_path / "data"
    data.mkdir()
    (data / "history.json").write_text(json.dumps({"version": 1, "jobs": [history_job(status=status)], "events": []}), encoding="utf-8")
    server = DesktopServer(tmp_path, data, app_factory=monitor_app_factory(tmp_path, calls))
    try:
        base = server.start().rsplit("/", 1)[0]
        state = request_json(base + "/api/monitor")
        job = state["jobs"][0]
        assert job["status"] == "failed" and job["error"] == INTERRUPTED
        assert job["active_tool"] is None and job["finished_at"]
        assert job["elapsed_seconds"] == 2.5
        assert job["events"][-1]["text"] == INTERRUPTED
        assert all(item["active_job_id"] is None for item in state["providers"])
        assert calls == []
    finally:
        server.stop()
    assert json.loads((data / "history.json").read_text(encoding="utf-8"))["jobs"][0]["status"] == "failed"


def test_corrupt_history_logs_warning_but_desktop_starts_normally(tmp_path, caplog):
    calls = []
    data = tmp_path / "data"
    data.mkdir()
    (data / "history.json").write_text("{not valid json", encoding="utf-8")
    server = DesktopServer(tmp_path, data, app_factory=monitor_app_factory(tmp_path, calls))
    try:
        base = server.start().rsplit("/", 1)[0]
        assert request_json(base + "/api/monitor")["jobs"] == []
        assert server.error is None and "Could not restore desktop task history" in caplog.text
        assert calls == []
    finally:
        server.stop()


def test_restore_validates_bounds_and_never_reinstates_execution_fields(tmp_path):
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return {"status": "completed", "exit_code": 0}

    selected = MonitorService(tmp_path, runner=runner, discoverer=fake_discovery)
    jobs = []
    for index in range(25):
        record = history_job(f"job-{index}")
        record.update(command_key="arbitrary.exe", raw_prompt="private", task="execute")
        record["events"] = [{"id": index * 1000 + event + 1, "at": HISTORY_TIME,
                             "kind": "message", "text": "x" * 9000} for event in range(120)]
        jobs.append(record)
    global_events = [{"id": 30000 + index, "at": HISTORY_TIME, "job_id": "job-0", "provider": "claude",
                      "kind": "message", "text": "api_key=sk-proj-synthetic_secret_value_123456"} for index in range(350)]
    selected.restore_history({"version": 1, "jobs": jobs, "events": global_events})
    exported = selected.export_history()
    assert len(exported["jobs"]) == 20 and len(exported["events"]) == 300
    assert all(len(job["events"]) <= 100 for job in exported["jobs"])
    assert all(len(event["text"]) <= 8000 for job in exported["jobs"] for event in job["events"])
    encoded = json.dumps(exported)
    assert "arbitrary.exe" not in encoded and "raw_prompt" not in encoded
    assert "synthetic_secret" not in encoded and "[REDACTED]" in encoded
    assert calls == [] and all(job.task is None and job.prompt == "" for job in selected._jobs.values())
    # Untrusted objects in identifier/status/kind positions are skipped, rather
    # than becoming executable records or breaking restoration of valid data.
    malformed = [{"id": [], "provider": {}, "status": {}}, history_job("valid")]
    selected.restore_history({"version": 1, "jobs": malformed,
                              "events": [{"kind": {}}, {"kind": "message", "provider": {}}]})
    assert [job["id"] for job in selected.snapshot()["jobs"]] == ["valid"]


def test_restore_cannot_replace_an_owned_active_job(tmp_path):
    async def scenario():
        selected = MonitorService(tmp_path, runner=lambda *args, **kwargs: {"status": "completed", "exit_code": 0},
                                  discoverer=fake_discovery)
        try:
            selected.start_job("codex", "Review.")
            with pytest.raises(MonitorError, match="active"):
                selected.restore_history({"version": 1, "jobs": [], "events": []})
        finally:
            await selected.close()
    asyncio.run(scenario())


def test_desktop_shutdown_cancels_owned_work_before_final_history_save(tmp_path):
    started = threading.Event()
    finished = threading.Event()

    def runner(provider, prompt, workspace, emit, cancel, **options):
        started.set()
        emit({"kind": "tool", "tool": "read_file", "status": "running", "text": "Reading source."})
        cancel.wait(3)
        finished.set()
        return {"status": "cancelled", "exit_code": None}

    def build(**options):
        selected = MonitorService(options["monitor_workspace"], runner=runner, discoverer=fake_discovery)
        app = web.Application()
        register_monitor(app, workspace=options["monitor_workspace"], web_root=tmp_path, service=selected)
        return app

    data = tmp_path / "data"
    server = DesktopServer(tmp_path, data, app_factory=build)
    try:
        base = server.start().rsplit("/", 1)[0]
        post_json(base + "/api/monitor/jobs", {"provider": "codex", "prompt": "Review source."})
        assert started.wait(1)
    finally:
        server.stop()
    assert finished.is_set()
    job = json.loads((data / "history.json").read_text(encoding="utf-8"))["jobs"][0]
    assert job["status"] == "cancelled" and job["finished_at"]
    assert job["cancel_requested"] is True and job["active_tool"] is None


def test_desktop_bridge_is_published_only_after_listener_ready_and_removed_on_stop(tmp_path):
    calls = []
    data = tmp_path / "data"
    original = monitor_app_factory(tmp_path, calls)

    def build(**options):
        assert not (data / "office-bridge.json").exists()
        return original(**options)

    server = DesktopServer(tmp_path, data, app_factory=build)
    try:
        url = server.start()
        record = json.loads((data / "office-bridge.json").read_text(encoding="utf-8"))
        assert record["port"] == urlsplit(url).port
        assert record["workspace"] == str(tmp_path.resolve())
        assert len(record["token"]) >= 32 and len(record["nonce"]) >= 32
        assert calls == []

        async def inspect():
            async with ClientSession() as client:
                async with client.get(url.replace("/monitor", "/api/bridge/info"),
                                      headers={"Authorization": "Bearer " + record["token"]}) as response:
                    assert response.status == 200
                    return await response.json()
        info = asyncio.run(inspect())
        assert info == {"version": 1, "workspace": record["workspace"], "nonce": record["nonce"]}
    finally:
        server.stop()
    assert not (data / "office-bridge.json").exists()
    assert calls == []


def test_desktop_bridge_and_ui_share_exact_service_and_cancellation(tmp_path, monkeypatch):
    import collaboration_service
    started = threading.Event()
    captured = []

    class FakeCoordinator:
        def __init__(self, service):
            self.service = service
        async def run(self, parent_id):
            captured.append(self.service)
            started.set()
            cancel = self.service.team_cancel_event(parent_id)
            while not cancel.is_set():
                await asyncio.sleep(0.01)
            return {"status": "cancelled", "output": "Stopped."}

    monkeypatch.setattr(collaboration_service, "CollaborationCoordinator", FakeCoordinator)
    services = []

    def no_cli(*args, **kwargs):
        raise AssertionError("Bridge tests must not call a CLI")

    def build(**options):
        selected = MonitorService(options["monitor_workspace"], runner=no_cli, discoverer=fake_discovery)
        services.append(selected)
        app = web.Application()
        register_monitor(app, workspace=options["monitor_workspace"], web_root=tmp_path, service=selected)
        return app

    data = tmp_path / "data"
    server = DesktopServer(tmp_path, data, app_factory=build)
    try:
        base = server.start().rsplit("/", 1)[0]
        record = json.loads((data / "office-bridge.json").read_text())

        async def control():
            async with ClientSession() as client:
                headers = {"Authorization": "Bearer " + record["token"]}
                async with client.post(base + "/api/bridge/start", headers=headers,
                                       json={"provider": "codex", "prompt": "Review source.",
                                             "collaboration": {"members": [{"provider": "antigravity"}]}}) as response:
                    assert response.status == 202
                    job = (await response.json())["job"]
                async with client.get(base + "/api/monitor") as response:
                    state = await response.json()
                    assert state["jobs"][0]["id"] == job["id"]
                    assert all(item["team_run_id"] == job["id"] for item in state["providers"]
                               if item["id"] in {"codex", "antigravity"})
                async with client.post(base + f"/api/bridge/jobs/{job['id']}/cancel", headers=headers, json={}) as response:
                    assert response.status == 202
                    assert (await response.json())["job"]["cancel_requested"] is True
                return job["id"]
        job_id = asyncio.run(control())
        assert started.wait(1)
        state = wait_for(lambda: request_json(base + "/api/monitor")
                         if request_json(base + "/api/monitor")["jobs"][0]["status"] == "cancelled" else None)
        assert state["jobs"][0]["id"] == job_id
        assert captured == services and len(services) == 1
    finally:
        server.stop()


def test_injected_app_without_monitor_does_not_publish_bridge(tmp_path):
    data = tmp_path / "data"
    server = DesktopServer(tmp_path, data, app_factory=app_factory({}))
    try:
        assert request_json(server.start())["ready"]
        assert not (data / "office-bridge.json").exists()
    finally:
        server.stop()


def test_bridge_registration_failure_preserves_foreign_record_without_starting_jobs(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    record = data / "office-bridge.json"
    record.write_text("foreign record")
    calls = []
    server = DesktopServer(tmp_path, data, app_factory=monitor_app_factory(tmp_path, calls))
    try:
        with pytest.raises(RuntimeError, match="preserved"):
            server.start()
        assert not server.running and calls == []
        assert record.read_text() == "foreign record"
    finally:
        server.stop()
