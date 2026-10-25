"""Model-led cross-provider handoffs through real monitor job lifecycles."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

import collaboration_service as coordinator
from collaboration_service import DecisionError, parse_decision
from collaboration_workspace import ArtifactStore
from monitor_service import MonitorService, TERMINAL


def discovery(*_):
    return [{"id": provider, "available": True, "command": f"{provider}.exe",
             "configured_model": provider + "-model", "configured_reasoning_effort": "high" if provider == "codex" else None,
             "model_options": [{"id": provider + "-model", "name": provider,
                                "reasoning_efforts": ["low", "high"], "default_reasoning_effort": "low"}]}
            for provider in ("codex", "antigravity", "claude")]


def decision(action, peers=(), summary="Public plan"):
    return json.dumps({"action": action, "summary": summary, "tasks": [
        {"provider": provider, "task": "Implement and verify the requested feature", "task_mode": mode}
        for provider, mode in peers]})


async def finished(service, parent_id):
    async def poll():
        while True:
            result = next(job for job in service.snapshot()["jobs"] if job["id"] == parent_id)
            if result["status"] in TERMINAL:
                return result
            await asyncio.sleep(0.005)
    return await asyncio.wait_for(poll(), 5)


@pytest.mark.parametrize("lead,peer", [("codex", "antigravity"), ("antigravity", "codex"), ("claude", "codex")])
def test_bidirectional_team_writes_shared_copy_returns_results_and_requires_explicit_apply(tmp_path, lead, peer):
    (tmp_path / "main.py").write_text("value = 1\n")
    calls = []
    lead_turns = 0

    def runner(provider, prompt, workspace, emit, cancel, **options):
        nonlocal lead_turns
        calls.append((provider, workspace, options))
        assert workspace.resolve() != tmp_path.resolve()
        assert options["shared_workspace"] is True
        if provider == lead:
            lead_turns += 1
            assert options["execution_mode"] == "read_only"
            assert options.get("native_schema", True) is (lead == "codex")
            if lead_turns == 1:
                output = decision("delegate", [(peer, "write")])
            else:
                assert "Worker completed and tested" in prompt
                assert (workspace / "main.py").read_text() == "value = 2\n"
                output = decision("finish", summary="Integrated and verified result")
        else:
            assert provider == peer and options["execution_mode"] == "read_only"
            emit({"kind": "tool", "tool": "Read", "text": "main.py", "status": "completed"})
            output = json.dumps({"summary": "Worker completed and tested", "changes": [
                {"path": "main.py", "operation": "replace", "content": "value = 2\n"}]})
        emit({"kind": "message", "text": output})
        return {"status": "completed", "output": output, "exit_code": 0}

    async def exercise():
        service = MonitorService(tmp_path, runner=runner, discoverer=discovery)
        try:
            parent = service.start_job(lead, "Update value and verify it", collaboration={"members": [{"provider": peer}]})
            result = await finished(service, parent["id"])
            assert result["status"] == "completed" and result["latest_message"] == "Integrated and verified result"
            children = [item for item in service.snapshot()["jobs"] if item.get("parent_job_id") == parent["id"]]
            assert {item["role"] for item in children} == {"planning", "worker", "synthesis"}
            assert all(item["kind"] == "delegation" and item["status"] == "completed" for item in children)
            assert len({str(call[1]) for call in calls}) == 1
            assert not calls[0][1].exists(), "Shared temporary workspace should be cleaned only after all turns finish"
            assert (tmp_path / "main.py").read_text() == "value = 1\n"
            assert not result["artifact"]["applied"]
            assert all(not item.get("team_run_id") and not item["active_job_id"] for item in service.snapshot()["providers"])
            assert "\"action\"" not in json.dumps([event for job in children for event in job["events"]])
            ArtifactStore(tmp_path).apply(parent["id"])
            assert (tmp_path / "main.py").read_text() == "value = 2\n"
        finally:
            await service.close()
    asyncio.run(exercise())


def test_multiple_workers_run_in_order_and_read_each_others_changes(tmp_path):
    calls = []

    def runner(provider, prompt, workspace, emit, cancel, **options):
        calls.append(provider)
        if provider == "antigravity":
            output = decision("delegate", [("codex", "write"), ("claude", "read")]) if len(calls) == 1 else decision("finish")
        elif provider == "codex":
            assert options["execution_mode"] == "read_only"
            output = json.dumps({"summary": "Implemented feature", "changes": [
                {"path": "feature.py", "operation": "create", "content": "result = 42\n"}]})
        else:
            assert (workspace / "feature.py").read_text() == "result = 42\n"
            assert options["execution_mode"] == "read_only"
            output = json.dumps({"summary": "Reviewed the actual implementation", "findings": ["result is assigned 42"],
                                 "limitations": [], "complete": True})
        return {"status": "completed", "output": output}

    async def exercise():
        service = MonitorService(tmp_path, runner=runner, discoverer=discovery)
        try:
            parent = service.start_job("antigravity", "Build feature", collaboration={"members": [{"provider": "codex"}, {"provider": "claude"}]})
            assert (await finished(service, parent["id"]))["status"] == "completed"
            assert calls == ["antigravity", "codex", "claude", "antigravity"]
            assert not (tmp_path / "feature.py").exists()
        finally:
            await service.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["invalid_provider", "truncated", "finish_without_worker"])
def test_invalid_or_premature_decisions_are_bounded_and_never_spawn_workers(tmp_path, failure):
    calls = []
    def runner(provider, prompt, workspace, emit, cancel, **options):
        calls.append(provider)
        output = decision("delegate", [("not-selected", "write")]) if failure == "invalid_provider" else decision("finish")
        return {"status": "completed", "output": output, "output_truncated": failure == "truncated"}

    async def exercise():
        service = MonitorService(tmp_path, runner=runner, discoverer=discovery)
        try:
            parent = service.start_job("antigravity", "Build feature", collaboration={"members": [{"provider": "codex"}]})
            result = await finished(service, parent["id"])
            assert result["status"] == "failed" and len(calls) == 4
            assert set(calls) == {"antigravity"}
            assert result["artifact"]["changed_files"] == []
        finally:
            await service.close()
    asyncio.run(exercise())


def test_failed_worker_cannot_be_reported_as_a_successful_team(tmp_path):
    lead_turns = 0
    def runner(provider, prompt, workspace, emit, cancel, **options):
        nonlocal lead_turns
        if provider == "codex":
            return {"status": "failed", "error": "Test failed", "output": "Implementation incomplete"}
        lead_turns += 1
        return {"status": "completed", "output": decision("delegate", [("codex", "write")]) if lead_turns == 1 else decision("finish")}

    async def exercise():
        service = MonitorService(tmp_path, runner=runner, discoverer=discovery)
        try:
            parent = service.start_job("antigravity", "Build feature", collaboration={"members": [{"provider": "codex"}]})
            result = await finished(service, parent["id"])
            assert result["status"] == "failed" and result["error"]
        finally:
            await service.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", ["outside_path", "truncated"])
def test_host_rejects_invalid_worker_proposals_without_claiming_success_or_changing_source(tmp_path, bad):
    lead_turns = 0
    def runner(provider, prompt, workspace, emit, cancel, **options):
        nonlocal lead_turns
        if provider == "antigravity":
            lead_turns += 1
            return {"status": "completed", "output": decision("delegate", [("codex", "write")]) if lead_turns == 1 else decision("finish")}
        return {"status": "completed", "output_truncated": bad == "truncated", "output": json.dumps({
            "summary": "Proposed implementation", "changes": [{"path": "../escape.py" if bad == "outside_path" else "new.py",
                                                                  "operation": "create", "content": "result = 42\n"}]})}
    async def exercise():
        service = MonitorService(tmp_path, runner=runner, discoverer=discovery)
        try:
            parent = service.start_job("antigravity", "Build feature", collaboration={"members": [{"provider": "codex"}]})
            result = await finished(service, parent["id"])
            assert result["status"] == "failed" and result["artifact"]["changed_files"] == []
            assert not (tmp_path / "new.py").exists() and not (tmp_path.parent / "escape.py").exists()
        finally:
            await service.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("report", ["I will review the file.", json.dumps({
    "summary": "Review is not finished", "findings": [], "limitations": ["Could not analyze"], "complete": False})])
def test_a_review_intention_or_incomplete_report_is_not_a_completed_child_or_parent(tmp_path, report):
    lead_turns = 0
    def runner(provider, prompt, workspace, emit, cancel, **options):
        nonlocal lead_turns
        if provider == "antigravity":
            lead_turns += 1
            return {"status": "completed", "output": decision("delegate", [("claude", "read")]) if lead_turns == 1 else decision("finish")}
        assert "host_owned_isolated_copy" in prompt
        return {"status": "completed", "output": report}
    async def exercise():
        service = MonitorService(tmp_path, runner=runner, discoverer=discovery)
        try:
            parent = service.start_job("antigravity", "Review source", collaboration={"members": [{"provider": "claude"}]})
            result = await finished(service, parent["id"])
            assert result["status"] == "failed"
            worker = next(item for item in service.snapshot()["jobs"] if item.get("role") == "worker")
            assert worker["status"] == "failed" and worker["error"]
        finally:
            await service.close()
    asyncio.run(exercise())


def test_source_context_and_long_reports_stay_below_delegated_prompt_limit():
    settings = {"lead": "codex", "members": {"codex": {}, "antigravity": {}}, "prompt": "x" * 8000}
    reports = [{"provider": "antigravity", "task": "x" * 4000, "output": "x" * 6000, "error": "x" * 1000}
               for _ in range(6)]
    prompt = coordinator._prompt(settings, 4, reports, None, {"files": [{"content": "x" * 12000}]})
    assert len(prompt) < 64000
    assert '"report_truncated": true' in prompt


@pytest.mark.parametrize("deadline", [False, True])
def test_cancel_or_deadline_stops_worker_before_any_further_model_call(tmp_path, monkeypatch, deadline):
    started = threading.Event()
    calls = []
    if deadline:
        monkeypatch.setattr(coordinator, "MAX_TOTAL_SECONDS", 0.1)
    def runner(provider, prompt, workspace, emit, cancel, **options):
        calls.append(provider)
        if provider == "antigravity":
            return {"status": "completed", "output": decision("delegate", [("codex", "write")])}
        started.set()
        assert cancel.wait(3)
        return {"status": "cancelled", "output": "Stopped"}

    async def exercise():
        service = MonitorService(tmp_path, runner=runner, discoverer=discovery)
        try:
            parent = service.start_job("antigravity", "Build feature", collaboration={"members": [{"provider": "codex"}]})
            assert await asyncio.to_thread(started.wait, 2)
            if not deadline:
                child = next(job for job in service.snapshot()["jobs"] if job.get("role") == "worker")
                service.cancel_job(child["id"])
            result = await finished(service, parent["id"])
            assert result["status"] == ("timed_out" if deadline else "cancelled")
            assert calls == ["antigravity", "codex"]
            assert all(not item.get("team_run_id") for item in service.snapshot()["providers"])
        finally:
            await service.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("payload", [
    '```json\n{}\n```', '{"action":"finish","summary":"ok","tasks":[],"command":"rm"}',
    '{"action":"delegate","summary":"ok","tasks":[{"provider":"codex","task":"x","task_mode":"exec"}]}',
    '{"action":"delegate","summary":"ok","tasks":[{"provider":"antigravity","task":"x","task_mode":"write"}]}',
    '{"action":"finish","summary":"","tasks":[]}',
])
def test_dispatch_protocol_never_interprets_arbitrary_text_tools_or_unselected_peers(payload):
    with pytest.raises(DecisionError):
        parse_decision(payload, ["codex"])
