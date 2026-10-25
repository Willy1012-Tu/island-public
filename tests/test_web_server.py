"""Exercise the real local HTTP/WebSocket bridge without calling a model."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import web_server
from agent_brain import Action, Decision, MiniGame
from island_world import Agent, IslandWorld, Tree
from web_server import RUNTIME_KEY, create_app, parse_args


class PassiveBrain:
    def __init__(self):
        self.calls = 0
        self.closed = False
        self.closed_off_event_loop = False

    def think(self, observation):
        self.calls += 1
        return Decision("休息一下。", (Action("rest"),))

    def close(self):
        self.closed = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.closed_off_event_loop = True


def frontend(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>Island test</title>", encoding="utf-8")
    (root / "app.js").write_text("export const island = true;", encoding="utf-8")
    return root


def empty_world() -> IslandWorld:
    world = IslandWorld(tree_count=0, rock_count=0, seed=7)
    world.agent = Agent(world, 2, 2)
    return world


async def receive_state(socket, predicate=lambda state: True, *, timeout=2):
    async def receive():
        while True:
            message = await socket.receive_json()
            if message.get("type") == "state" and predicate(message):
                return message
    return await asyncio.wait_for(receive(), timeout=timeout)


def test_http_serves_frontend_and_authoritative_terrain(tmp_path):
    async def scenario():
        brain = PassiveBrain()
        app = create_app(web_root=frontend(tmp_path), brain_instance=brain,
                         world=empty_world(), start_loop=False)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/")
            assert response.status == 200 and "Island test" in await response.text()
            script = await client.get("/static/app.js")
            assert script.status == 200 and "export const island" in await script.text()
            state_response = await client.get("/api/state")
            assert state_response.status == 200
            state = await state_response.json()
            assert state_response.headers["Cache-Control"] == "no-store"
            assert state["brain"] == "demo" and state["tick"] == 0
            tiles = state["world"]["tiles"]
            assert len(tiles) == len(tiles[0]) == 20
            assert all(tile["terrain"] == "water" for tile in tiles[0] + tiles[-1])
            assert all(row[0]["terrain"] == row[-1]["terrain"] == "water" for row in tiles)
            assert tiles[1][7] == {"terrain": "sand", "entity": None}
            assert tiles[7][18] == {"terrain": "sand", "entity": None}
            assert tiles[7][7] == {"terrain": "grass", "entity": None}
            assert (await client.get("/static/%2e%2e/agent_brain.py")).status == 404
            assert brain.calls == 0
        assert brain.closed and brain.closed_off_event_loop
    asyncio.run(scenario())


def test_websocket_controls_broadcast_one_shared_world_and_pause_freezes_it(tmp_path):
    async def scenario():
        app = create_app(web_root=frontend(tmp_path), world=empty_world(),
                         tick_seconds=0.03, think_delay=1)
        async with TestClient(TestServer(app)) as client:
            first = await client.ws_connect("/ws")
            second = await client.ws_connect("/ws")
            initial_first = await receive_state(first)
            initial_second = await receive_state(second)
            assert initial_first["world"] == initial_second["world"]
            await first.send_json({"type": "control", "action": "pause"})
            paused = await receive_state(first, lambda state: state["paused"])
            shared = await receive_state(second, lambda state: state["paused"])
            assert paused["tick"] == shared["tick"]
            await asyncio.sleep(0.13)
            frozen = await (await client.get("/api/state")).json()
            assert frozen["tick"] == paused["tick"]
            assert frozen["agent"] == paused["agent"]
            assert frozen["counters"] == paused["counters"]
            for _ in range(4):
                await second.send_json({"type": "control", "action": "boredom"})
            bored = await receive_state(first, lambda state: state["agent"]["boredom"] == 100)
            assert bored["paused"] and bored["tick"] == paused["tick"]
            assert bored["revision"] > paused["revision"]
            await first.send_json({"type": "control", "action": "resume"})
            resumed = await receive_state(second, lambda state: not state["paused"] and state["tick"] > paused["tick"])
            assert resumed["agent"]["boredom"] <= 100
            assert len(app[RUNTIME_KEY]._clients) == 2
            await first.close()
            await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    [], {"type": "control"}, {"type": "move", "action": "pause"},
    {"type": "control", "action": "execute", "target": {"x": 3, "y": 2}},
    {"type": "control", "action": "move"}, {"type": "control", "action": 25},
])
def test_malformed_controls_return_error_without_mutating_world(tmp_path, payload):
    async def scenario():
        app = create_app(web_root=frontend(tmp_path), brain_instance=PassiveBrain(),
                         world=empty_world(), start_loop=False)
        async with TestClient(TestServer(app)) as client:
            socket = await client.ws_connect("/ws")
            original = await receive_state(socket)
            await socket.send_json(payload)
            reply = await asyncio.wait_for(socket.receive_json(), timeout=1)
            assert reply["type"] == "error" and reply["message"]
            current = await (await client.get("/api/state")).json()
            assert current == original
            await socket.close()
    asyncio.run(scenario())


def test_non_json_and_binary_controls_are_rejected(tmp_path):
    async def scenario():
        app = create_app(web_root=frontend(tmp_path), brain_instance=PassiveBrain(), start_loop=False)
        async with TestClient(TestServer(app)) as client:
            socket = await client.ws_connect("/ws")
            await receive_state(socket)
            await socket.send_str("not json")
            assert (await socket.receive_json())["type"] == "error"
            await socket.send_bytes(b"pause")
            assert (await socket.receive_json())["type"] == "error"
            await socket.close()
    asyncio.run(scenario())


def test_slow_snapshot_sender_cannot_block_ticks_or_other_clients(tmp_path, monkeypatch):
    async def scenario():
        original_sender = web_server._send_snapshots
        blocked = asyncio.Event()
        release = asyncio.Event()
        first_client = None

        async def slow_first_sender(connection):
            nonlocal first_client
            if first_client is None:
                first_client = connection
                blocked.set()
                await release.wait()
            await original_sender(connection)

        monkeypatch.setattr(web_server, "_send_snapshots", slow_first_sender)
        app = create_app(web_root=frontend(tmp_path), world=empty_world(),
                         tick_seconds=0.025, think_delay=1)
        async with TestClient(TestServer(app)) as client:
            slow = await client.ws_connect("/ws")
            await asyncio.wait_for(blocked.wait(), timeout=1)
            fast = await client.ws_connect("/ws")
            state = await receive_state(fast, lambda value: value["tick"] >= 4)
            assert first_client.snapshots.maxsize == first_client.snapshots.qsize() == 1
            assert app[RUNTIME_KEY].simulation.tick_count >= 4
            release.set()
            latest = await receive_state(slow)
            assert latest["tick"] >= state["tick"]
            assert latest["revision"] >= state["revision"]
            await slow.close()
            await fast.close()
    asyncio.run(scenario())


def test_before_after_events_report_harvest_game_and_bounded_monotonic_history(tmp_path):
    async def scenario():
        world = empty_world()
        world.grid[2][3] = Tree(3, 2)
        brain = PassiveBrain()
        app = create_app(web_root=frontend(tmp_path), world=world,
                         brain_instance=brain, start_loop=False)
        async with TestClient(TestServer(app)) as client:
            runtime = app[RUNTIME_KEY]
            now = runtime._last_tick
            runtime.simulation.accept_decision(Decision("拿一份木頭。", (Action("gather", (3, 2)),)))
            now += 0.51
            runtime.advance(now)
            state = await (await client.get("/api/state")).json()
            assert state["world"]["tiles"][2][3]["entity"] is None
            assert state["agent"]["inventory"] == {"wood": 1}
            assert state["events"] == [{"id": 1, "tick": 1, "kind": "harvest",
                                         "target": {"x": 3, "y": 2}, "resource": "tree"}]
            game = MiniGame("登月一步", "走到剛剛清空的格子。")
            runtime.simulation.accept_decision(Decision("來玩登月一步。", (Action("move", (3, 2)),), game))
            now += 0.51
            runtime.advance(now)
            assert [event["kind"] for event in runtime.events] == ["harvest", "move", "game"]
            assert runtime.snapshot()["counters"]["games"] == 1
            for _ in range(20):
                runtime.simulation.accept_decision(Decision("休息。", (Action("rest"),)))
                now += 0.51
                runtime.advance(now)
            events = runtime.snapshot()["events"]
            assert len(events) == 12
            assert [event["id"] for event in events] == list(range(12, 24))
            assert all(event["kind"] == "rest" for event in events)
            assert brain.calls == 0
    asyncio.run(scenario())


def test_background_thought_does_not_block_http_ticks_or_shutdown(tmp_path):
    class WaitingBrain(PassiveBrain):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def think(self, observation):
            self.calls += 1
            self.started.set()
            self.release.wait(timeout=3)
            return Decision("休息。", (Action("rest"),))

        def close(self):
            super().close()
            self.release.set()

    async def scenario():
        brain = WaitingBrain()
        app = create_app(web_root=frontend(tmp_path), world=empty_world(),
                         brain_instance=brain, tick_seconds=0.02)
        async with TestClient(TestServer(app)) as client:
            assert await asyncio.to_thread(brain.started.wait, 1)
            async def ticks_continue():
                while True:
                    current = await (await client.get("/api/state")).json()
                    if current["tick"] >= 3:
                        return current
                    await asyncio.sleep(0.01)
            state = await asyncio.wait_for(ticks_continue(), timeout=1)
            assert state["thinking"] and state["tick"] >= 3
            assert brain.calls == 1 and state["counters"]["decisions"] == 0
        assert brain.closed and brain.closed_off_event_loop
        assert app[RUNTIME_KEY].brain_loop.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [
    ["--port", "0"], ["--port", "65536"], ["--boredom", "-1"], ["--boredom", "101"],
    ["--tick-seconds", "0"], ["--tick-seconds", "nan"], ["--think-delay", "-1"],
    ["--brain-timeout", "inf"],
])
def test_invalid_server_cli_options_fail_early(arguments):
    with pytest.raises(SystemExit):
        parse_args(arguments)


def test_server_defaults_are_local_and_demo():
    args = parse_args([])
    assert args.host == "127.0.0.1" and args.port == 8000
    assert args.brain == "demo" and args.tick_seconds == 0.5 and args.seed == 7
