"""Browser protocol to actual copy, persisted patch and explicit source apply."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from monitor_service import MonitorService, TERMINAL
from web_server import create_app


def test_team_http_flow_creates_reviewable_artifact_and_applies_only_after_post(tmp_path):
    turns = 0
    def discover(*_):
        return [{"id": provider, "available": True, "command": provider + ".exe"}
                for provider in ("codex", "antigravity")]
    def runner(provider, prompt, workspace, emit, cancel, **options):
        nonlocal turns
        if provider == "codex":
            assert options["execution_mode"] == "read_only"
            output = {"summary": "Prepared file contents", "changes": [
                {"path": "feature.py", "operation": "create", "content": "answer = 42\n"}]}
        else:
            turns += 1
            output = ({"action": "delegate", "summary": "Request implementation", "tasks": [
                {"provider": "codex", "task": "Create feature.py", "task_mode": "write"}]} if turns == 1
                else {"action": "finish", "summary": "Integrated implementation", "tasks": []})
        return {"status": "completed", "output": json.dumps(output)}
    async def exercise():
        service = MonitorService(tmp_path, runner=runner, discoverer=discover)
        client = TestClient(TestServer(create_app(start_loop=False, monitor_service=service)))
        await client.start_server()
        try:
            response = await client.post("/api/monitor/jobs", json={"provider": "antigravity", "prompt": "Create a feature",
                                        "collaboration": {"members": [{"provider": "codex"}]}})
            assert response.status == 202
            parent_id = (await response.json())["job"]["id"]
            async def poll():
                while True:
                    snapshot = await (await client.get("/api/monitor")).json()
                    parent = next(item for item in snapshot["jobs"] if item["id"] == parent_id)
                    if parent["status"] in TERMINAL:
                        return parent
                    await asyncio.sleep(0.01)
            parent = await asyncio.wait_for(poll(), 5)
            assert parent["status"] == "completed"
            assert not (tmp_path / "feature.py").exists()
            review = await (await client.get(parent["artifact"]["diff_url"])).json()
            assert "answer = 42" in review["diff"]
            assert review["artifact"]["id"] == parent_id and not review["artifact"]["applied"]
            download = await client.get(parent["artifact"]["download_url"])
            assert download.status == 200 and await download.text() == review["diff"]
            assert not (tmp_path / "feature.py").exists()
            applied = await client.post(f"/api/monitor/jobs/{parent_id}/apply", json={})
            assert applied.status == 200 and (await applied.json())["artifact"]["applied"]
            assert (tmp_path / "feature.py").read_bytes() == b"answer = 42\n"
        finally:
            await client.close()
    asyncio.run(exercise())
