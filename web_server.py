"""Local browser bridge for the existing Python island simulation.

The server owns one world and one BrainLoop. Browser clients only observe state
and request pause/resume or boredom changes; Python still chooses and executes
every game action.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, WSCloseCode, web

from agent_brain import AgentBrain, BrainError, DemoBrain
from island_world import IslandWorld, Rock, Tree, Water
from main import Brain, BrainLoop
from simulation import Simulation

LOGGER = logging.getLogger("island.web")
WEB_ROOT = Path(__file__).resolve().parent / "web"


@dataclass(eq=False)
class _Client:
    socket: web.WebSocketResponse
    snapshots: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sender: asyncio.Task[None] | None = None

    def offer(self, state: dict[str, Any]) -> None:
        """Keep only the newest snapshot while this client is busy sending."""
        if self.snapshots.full():
            self.snapshots.get_nowait()
        self.snapshots.put_nowait(state)


class WebRuntime:
    """Adapt the unchanged simulation to versioned snapshots and visual events."""

    def __init__(
        self, *, world: IslandWorld, brain: Brain, brain_name: str,
        seed: int, tick_seconds: float = 0.5, think_delay: float = 1.0,
    ) -> None:
        self.world = world
        self.simulation = Simulation(world, LOGGER)
        self.simulation.internal_thought = "看看這座小島，下一步要做什麼呢？"
        self.brain_loop = BrainLoop(brain, think_delay=think_delay, logger=LOGGER)
        self.brain_name = brain_name
        self.seed = seed
        self.tick_seconds = tick_seconds
        self.paused = False
        self.revision = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=12)
        self._event_id = 0
        self._clients: set[_Client] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._last_tick = time.monotonic()
        self._last_fingerprint = self._fingerprint()

    def snapshot(self) -> dict[str, Any]:
        agent = self.world.agent
        current = self.simulation.pending_actions[0] if self.simulation.has_goal else None
        # Read-only adapter access: the game still owns mini-game lifecycle and
        # reward logic; the web bridge only exposes that existing state.
        game = self.simulation._mini_game
        tiles = []
        for y, row in enumerate(self.world.grid):
            cells = []
            for x, entity in enumerate(row):
                terrain = "water" if isinstance(entity, Water) else (
                    "sand" if x in (1, self.world.SIZE - 2) or y in (1, self.world.SIZE - 2)
                    else "grass")
                resource = "tree" if isinstance(entity, Tree) else "rock" if isinstance(entity, Rock) else None
                cells.append({"terrain": terrain, "entity": resource})
            tiles.append(cells)
        return {
            "type": "state", "version": 1, "revision": self.revision,
            "tick": self.simulation.tick_count, "tick_seconds": self.tick_seconds,
            "paused": self.paused, "brain": self.brain_name, "thinking": self.brain_loop.thinking,
            "world": {"size": self.world.SIZE, "seed": self.seed, "tiles": tiles},
            "agent": {"x": agent.x, "y": agent.y, "energy": agent.energy,
                      "boredom": agent.boredom, "inventory": dict(agent.inventory)},
            "path": [{"x": x, "y": y} for x, y in self.simulation.path],
            "internal_thought": self.simulation.internal_thought, "status": self.simulation.status,
            "current_action": None if current is None else {
                "name": current.name,
                "target": None if current.target is None else {"x": current.target[0], "y": current.target[1]}},
            "mini_game": None if game is None else {"name": game.name, "rules": game.rules},
            "counters": {"plans": self.simulation.decisions_completed,
                         "games": self.simulation.games_completed,
                         "decisions": self.brain_loop.decisions},
            "events": list(self.events),
        }

    def apply_control(self, payload: Any) -> None:
        if (not isinstance(payload, dict) or set(payload) != {"type", "action"}
                or payload.get("type") != "control" or not isinstance(payload.get("action"), str)):
            raise ValueError("Expected {type: 'control', action: 'toggle_pause' | 'pause' | 'resume' | 'boredom'}.")
        action = payload["action"]
        if action not in ("toggle_pause", "pause", "resume", "boredom"):
            raise ValueError("Unknown control action.")
        if action == "boredom":
            self.world.agent.boredom = min(100, self.world.agent.boredom + 25)
            LOGGER.info("BOREDOM %d", self.world.agent.boredom)
        else:
            self.paused = not self.paused if action == "toggle_pause" else action == "pause"
            # Resuming starts a fresh tick interval, rather than catching up on
            # the real time spent paused with a burst of game actions.
            self._last_tick = time.monotonic()
            LOGGER.info("%s", "PAUSED" if self.paused else "RESUMED")
        self.publish()

    def publish(self) -> None:
        self.revision += 1
        state = self.snapshot()
        self._last_fingerprint = self._fingerprint()
        for client in tuple(self._clients):
            client.offer(state)

    def advance(self, now: float) -> None:
        """Poll slow thinking, then execute at most one due world tick."""
        if self._stopped:
            return
        self.brain_loop.update(self.simulation, now, paused=self.paused)
        if not self.paused and now - self._last_tick >= self.tick_seconds:
            before = self._event_state()
            self.simulation.advance_tick()
            self._record_events(before)
            self._last_tick = now
        if self._fingerprint() != self._last_fingerprint:
            self.publish()

    async def start(self, *, start_loop: bool = True) -> None:
        self._last_tick = time.monotonic()
        if start_loop:
            self._task = asyncio.create_task(self._run(), name="island-world-ticks")

    async def _run(self) -> None:
        interval = min(0.05, self.tick_seconds)
        while not self._stopped:
            self.advance(time.monotonic())
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        # Closing sockets allows aiohttp's active request handlers to finish.
        await asyncio.gather(
            *(client.socket.close(code=WSCloseCode.GOING_AWAY, message=b"Server shutting down")
              for client in tuple(self._clients)), return_exceptions=True)
        # Codex termination and executor joining may wait for a worker. Neither
        # operation should hold the asyncio event loop or a browser connection.
        await asyncio.to_thread(self.brain_loop.close)

    def _fingerprint(self) -> tuple[Any, ...]:
        agent = self.world.agent
        return (self.simulation.tick_count, self.simulation.internal_thought, self.simulation.status,
                tuple(self.simulation.path), tuple(self.simulation.pending_actions),
                agent.x, agent.y, agent.energy, agent.boredom, tuple(sorted(agent.inventory.items())),
                self.brain_loop.thinking, self.brain_loop.decisions, self.brain_loop.failures,
                self.simulation.games_completed, self.paused, self._event_id)

    def _event_state(self) -> dict[str, Any]:
        agent = self.world.agent
        current = self.simulation.pending_actions[0] if self.simulation.has_goal else None
        return {"position": (agent.x, agent.y), "energy": agent.energy,
                "games": self.simulation.games_completed,
                "rest": current is not None and current.name == "rest" and current.target is None,
                "resources": {(entity.x, entity.y): entity.name for row in self.world.grid
                              for entity in row if isinstance(entity, (Tree, Rock))}}

    def _record_events(self, before: dict[str, Any]) -> None:
        agent = self.world.agent
        if before["position"] != (agent.x, agent.y):
            self._event("move", target={"x": agent.x, "y": agent.y})
        for (x, y), resource in before["resources"].items():
            if self.world.get_entity_at((x, y)) is None:
                self._event("harvest", target={"x": x, "y": y}, resource=resource)
        if agent.energy > before["energy"] or (before["rest"] and self.simulation.last_error is None):
            self._event("rest")
        if self.simulation.games_completed > before["games"]:
            self._event("game", target={"x": agent.x, "y": agent.y})

    def _event(self, kind: str, **details: Any) -> None:
        self._event_id += 1
        self.events.append({"id": self._event_id, "tick": self.simulation.tick_count,
                            "kind": kind, **details})


RUNTIME_KEY = web.AppKey("island_runtime", WebRuntime)
WEB_ROOT_KEY = web.AppKey("island_web_root", Path)


async def _index(request: web.Request) -> web.StreamResponse:
    index = request.app[WEB_ROOT_KEY] / "index.html"
    if not index.is_file():
        raise web.HTTPNotFound(text="The island frontend is missing: expected web/index.html.")
    return web.FileResponse(index, headers={"Cache-Control": "no-cache"})


async def _state(request: web.Request) -> web.Response:
    return web.json_response(request.app[RUNTIME_KEY].snapshot(), headers={"Cache-Control": "no-store"})


async def _send_snapshots(client: _Client) -> None:
    try:
        while not client.socket.closed:
            state = await client.snapshots.get()
            async with client.send_lock:
                await client.socket.send_json(state, dumps=lambda value: json.dumps(value, ensure_ascii=False))
    except (ConnectionError, RuntimeError):
        pass


async def _control_error(client: _Client, message: str) -> None:
    async with client.send_lock:
        await client.socket.send_json({"type": "error", "message": message})


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    runtime = request.app[RUNTIME_KEY]
    socket = web.WebSocketResponse(heartbeat=20.0, timeout=2.0, max_msg_size=4096)
    await socket.prepare(request)
    client = _Client(socket)
    runtime._clients.add(client)
    client.offer(runtime.snapshot())
    client.sender = asyncio.create_task(_send_snapshots(client), name="island-client-snapshots")
    try:
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                    runtime.apply_control(payload)
                except (ValueError, TypeError, RecursionError) as exc:
                    await _control_error(client, str(exc))
            elif message.type == WSMsgType.BINARY:
                await _control_error(client, "Controls must be JSON text messages.")
            elif message.type == WSMsgType.ERROR:
                break
    except ConnectionError:
        pass
    finally:
        runtime._clients.discard(client)
        if client.sender is not None:
            client.sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client.sender
        await socket.close()
    return socket


def create_app(
    *, brain: str = "demo", seed: int = 7, boredom: int = 0,
    tick_seconds: float = 0.5, think_delay: float = 1.0,
    brain_timeout: float = 90.0, codex_command: str = "codex", model: str | None = None,
    web_root: Path | str | None = None, brain_instance: Brain | None = None,
    world: IslandWorld | None = None, start_loop: bool = True,
    claude_command: str = "agy", antigravity_command: str = "agy",
    monitor_timeout: float = 300, monitor_service: Any = None,
    monitor_workspace: Path | str | None = None,
) -> web.Application:
    """Build a local app; optional injected world/brain support offline tests."""
    if brain not in ("demo", "codex"):
        raise ValueError("brain must be 'demo' or 'codex'.")
    if not math.isfinite(tick_seconds) or tick_seconds <= 0:
        raise ValueError("tick_seconds must be a finite positive number.")
    if not math.isfinite(think_delay) or think_delay < 0:
        raise ValueError("think_delay must be a finite nonnegative number.")
    if not 0 <= boredom <= 100:
        raise ValueError("boredom must be between 0 and 100.")
    root = Path(web_root).resolve() if web_root is not None else WEB_ROOT
    island = world if world is not None else IslandWorld(seed=seed)
    island.agent.boredom = boredom
    selected_brain = brain_instance if brain_instance is not None else (
        DemoBrain() if brain == "demo" else AgentBrain(
            executable=codex_command, model=model, timeout=brain_timeout))
    runtime = WebRuntime(world=island, brain=selected_brain, brain_name=brain,
                         seed=seed, tick_seconds=tick_seconds, think_delay=think_delay)
    app = web.Application(client_max_size=64 * 1024)
    app[RUNTIME_KEY] = runtime
    app[WEB_ROOT_KEY] = root
    app.router.add_get("/", _index)
    app.router.add_get("/api/state", _state)
    app.router.add_get("/ws", _websocket)
    if root.is_dir():
        app.router.add_static("/static/", root, show_index=False, follow_symlinks=False)

    from monitor_server import register_monitor
    register_monitor(app, workspace=monitor_workspace or Path(__file__).resolve().parent, web_root=root,
                     commands={name: command for name, command, default in (
                         ("codex", codex_command, "codex"), ("claude", claude_command, "agy"),
                         ("antigravity", antigravity_command, "agy")) if command != default},
                     timeout=monitor_timeout, service=monitor_service)

    async def lifecycle(application: web.Application):
        await runtime.start(start_loop=start_loop)
        yield
        await runtime.stop()

    async def shutdown(application: web.Application) -> None:
        await runtime.stop()

    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    return app


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _nonnegative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the island's local Three.js web view.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--brain", choices=("demo", "codex"), default="demo",
                        help="demo: immediate offline preview (default); codex: use your CLI login")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--boredom", type=int, default=0)
    parser.add_argument("--tick-seconds", type=_positive, default=0.5)
    parser.add_argument("--think-delay", type=_nonnegative, default=1.0)
    parser.add_argument("--brain-timeout", type=_positive, default=90.0)
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--claude-command", default="agy",
                        help="Antigravity executable for the Claude 4.6 office")
    parser.add_argument("--antigravity-command", default="agy")
    parser.add_argument("--monitor-timeout", type=_positive, default=300.0)
    parser.add_argument("--monitor-workspace", type=Path, default=Path(__file__).resolve().parent,
                        help="fixed coding-task workspace; browsers cannot override this path")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 0 <= args.boredom <= 100:
        parser.error("--boredom must be between 0 and 100")
    return args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    args = parse_args(argv)
    try:
        app = create_app(brain=args.brain, seed=args.seed, boredom=args.boredom,
                         tick_seconds=args.tick_seconds, think_delay=args.think_delay,
                         brain_timeout=args.brain_timeout, codex_command=args.codex_command,
                         model=args.model, claude_command=args.claude_command,
                         antigravity_command=args.antigravity_command,
                         monitor_timeout=args.monitor_timeout, monitor_workspace=args.monitor_workspace)
        LOGGER.info("WEB brain=%s tick=%.3fs seed=%d", args.brain, args.tick_seconds, args.seed)
        web.run_app(app, host=args.host, port=args.port, print=LOGGER.info, access_log=None)
    except (BrainError, OSError, ValueError) as exc:
        LOGGER.error("WEB STARTUP FAILED %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
