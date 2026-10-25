"""Add coding-task monitoring routes without changing the island protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from aiohttp import WSMsgType, WSCloseCode, web

from monitor_service import MonitorError, MonitorService


@dataclass(eq=False)
class _Peer:
    socket: web.WebSocketResponse
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sender: asyncio.Task[None] | None = None

    def offer(self, state: dict[str, Any]) -> None:
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(state)


class MonitorHub:
    def __init__(self, service: MonitorService, web_root: Path):
        self.service = service
        self.web_root = web_root
        self.peers: set[_Peer] = set()
        self.closed = False
        self._pending: asyncio.Handle | None = None
        self._unsubscribe = service.subscribe(self.schedule_broadcast)

    def schedule_broadcast(self) -> None:
        if not self.closed and self._pending is None:
            # Multiple stdout events in one loop turn share a single snapshot.
            self._pending = asyncio.get_running_loop().call_soon(self.broadcast)

    def broadcast(self) -> None:
        self._pending = None
        if self.closed:
            return
        state = self.service.snapshot()
        for peer in tuple(self.peers):
            peer.offer(state)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._unsubscribe()
        if self._pending is not None:
            self._pending.cancel()
        await asyncio.gather(*(peer.socket.close(code=WSCloseCode.GOING_AWAY,
                                                message=b"Monitor shutting down")
                               for peer in tuple(self.peers)), return_exceptions=True)
        await self.service.close()


MONITOR_KEY = web.AppKey("coding_monitor", MonitorHub)


def _check_origin(request: web.Request) -> None:
    origin = request.headers.get("Origin")
    if origin is not None:
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("http", "https") or parsed.netloc.lower() != request.host.lower()
                or parsed.scheme != request.scheme or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise MonitorError("Cross-origin task controls are not allowed.", 403)


def _error(error: MonitorError) -> web.Response:
    return web.json_response({"type": "monitor_error", "message": str(error)}, status=error.status)


async def _page(request: web.Request) -> web.StreamResponse:
    name = "index.html" if request.path == "/island" else "monitor.html"
    target = request.app[MONITOR_KEY].web_root / name
    if not target.is_file():
        raise web.HTTPNotFound(text=f"Frontend file missing: web/{name}")
    return web.FileResponse(target, headers={"Cache-Control": "no-cache"})


async def _state(request: web.Request) -> web.Response:
    return web.json_response(request.app[MONITOR_KEY].service.snapshot(),
                             headers={"Cache-Control": "no-store"})


async def _json_body(request: web.Request) -> Any:
    _check_origin(request)
    if request.content_type != "application/json":
        raise MonitorError("Task controls require application/json.", 415)
    try:
        return await request.json()
    except (ValueError, RecursionError):
        raise MonitorError("Expected a JSON request body.") from None


async def _start_job(request: web.Request) -> web.Response:
    try:
        data = await _json_body(request)
        if (not isinstance(data, dict) or not {"provider", "prompt"} <= set(data)
                or not set(data) <= {"provider", "prompt", "model", "reasoning_effort", "collaboration"}):
            raise MonitorError("A task requires provider and prompt, with optional model and reasoning effort.")
        job = request.app[MONITOR_KEY].service.start_job(data["provider"], data["prompt"], data.get("model"),
                                                       data.get("reasoning_effort"), data.get("collaboration"))
        return web.json_response({"type": "monitor_job", "job": job}, status=202)
    except MonitorError as error:
        return _error(error)


async def _cancel_job(request: web.Request) -> web.Response:
    try:
        _check_origin(request)
        if request.can_read_body:
            data = await _json_body(request)
            if data != {}:
                raise MonitorError("Cancellation does not accept task arguments.")
        job = request.app[MONITOR_KEY].service.cancel_job(request.match_info["job_id"])
        return web.json_response({"type": "monitor_job", "job": job}, status=202)
    except MonitorError as error:
        return _error(error)


async def _control(service: MonitorService, data: Any) -> None:
    if not isinstance(data, dict) or data.get("type") != "monitor_control":
        raise MonitorError("Expected a monitor_control object.")
    action = data.get("action")
    if (action == "start" and {"type", "action", "provider", "prompt"} <= set(data)
            and set(data) <= {"type", "action", "provider", "prompt", "model", "reasoning_effort", "collaboration"}):
        service.start_job(data["provider"], data["prompt"], data.get("model"), data.get("reasoning_effort"),
                          data.get("collaboration"))
    elif action == "cancel" and set(data) == {"type", "action", "job_id"}:
        service.cancel_job(data["job_id"])
    elif action == "refresh" and set(data) == {"type", "action"}:
        await service.refresh_async()
    else:
        raise MonitorError("Unknown or malformed monitor control.")


async def _send_states(peer: _Peer) -> None:
    try:
        while not peer.socket.closed:
            state = await peer.queue.get()
            async with peer.lock:
                await peer.socket.send_json(state, dumps=lambda value: json.dumps(value, ensure_ascii=False))
    except (OSError, RuntimeError):
        pass


async def _artifact(request: web.Request) -> web.StreamResponse:
    service = request.app[MONITOR_KEY].service
    job_id = request.match_info["job_id"]
    action = request.match_info["artifact_action"]
    try:
        if action not in ("changes", "patch", "apply") or (action == "apply") != (request.method == "POST"):
            raise MonitorError("Unknown artifact action.", 404)
        if action == "apply":
            data = await _json_body(request)
            if data != {}:
                raise MonitorError("Applying reviewed changes does not accept task arguments.")
        job = service.get_job(job_id)
        if job.get("kind") != "collaboration" or job.get("artifact", {}).get("id") != job_id:
            raise MonitorError("Collaboration artifact not found.", 404)
        if action == "apply" and (job["status"] != "completed" or any(
                item.get("team_run_id") == job_id for item in service.snapshot()["providers"])):
            raise MonitorError("Changes can only be applied after the collaboration has completed.", 409)
        try:
            from collaboration_workspace import ArtifactError, ArtifactStore
        except ImportError:
            raise MonitorError("Collaboration artifacts are unavailable.", 503) from None
        try:
            store = ArtifactStore(service.workspace)
            if action == "changes":
                review = await asyncio.to_thread(store.review, job_id)
                service.update_team(job_id, artifact=review["artifact"])
                return web.json_response({"artifact": service.get_job(job_id)["artifact"], "diff": review["diff"]},
                                         headers={"Cache-Control": "no-store"})
            if action == "patch":
                patch = await asyncio.to_thread(store.patch, job_id)
                return web.Response(text=patch, content_type="text/x-diff",
                                    headers={"Cache-Control": "no-store",
                                             "Content-Disposition": f'attachment; filename="collaboration-{job_id}.patch"'})
            artifact = await asyncio.to_thread(store.apply, job_id)
            service.update_team(job_id, artifact=artifact)
            return web.json_response({"type": "monitor_artifact", "artifact": service.get_job(job_id)["artifact"]})
        except ArtifactError as error:
            raise MonitorError(str(error), error.status) from None
    except MonitorError as error:
        return _error(error)


async def _websocket(request: web.Request) -> web.StreamResponse:
    try:
        _check_origin(request)
    except MonitorError as error:
        return _error(error)
    hub = request.app[MONITOR_KEY]
    socket = web.WebSocketResponse(heartbeat=20, timeout=2, max_msg_size=64 * 1024)
    await socket.prepare(request)
    peer = _Peer(socket)
    hub.peers.add(peer)
    peer.offer(hub.service.snapshot())
    peer.sender = asyncio.create_task(_send_states(peer), name="monitor-client-snapshots")
    try:
        async for message in socket:
            if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                try:
                    if message.type != WSMsgType.TEXT:
                        raise MonitorError("Monitor controls must be JSON text.")
                    try:
                        data = json.loads(message.data)
                    except (ValueError, RecursionError):
                        raise MonitorError("Expected a JSON monitor control.") from None
                    await _control(hub.service, data)
                except MonitorError as error:
                    async with peer.lock:
                        await socket.send_json({"type": "monitor_error", "message": str(error)})
            elif message.type == WSMsgType.ERROR:
                break
    except OSError:
        pass
    finally:
        hub.peers.discard(peer)
        if peer.sender is not None:
            peer.sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await peer.sender
        await socket.close()
    return socket


def register_monitor(app: web.Application, *, workspace: Path | str, web_root: Path | str,
                     commands: Mapping[str, str] | None = None, timeout: float = 300,
                     service: MonitorService | None = None) -> MonitorService:
    """Install additive monitor routes; no task or model starts on registration."""
    selected = service if service is not None else MonitorService(workspace, commands=commands, timeout=timeout)
    hub = MonitorHub(selected, Path(web_root).resolve())
    app[MONITOR_KEY] = hub
    app.router.add_get("/monitor", _page)
    app.router.add_get("/island", _page)
    app.router.add_get("/api/monitor", _state)
    app.router.add_get("/ws/monitor", _websocket)
    app.router.add_post("/api/monitor/jobs", _start_job)
    app.router.add_post("/api/monitor/jobs/{job_id}/cancel", _cancel_job)
    app.router.add_get("/api/monitor/jobs/{job_id}/{artifact_action:changes|patch}", _artifact)
    app.router.add_post("/api/monitor/jobs/{job_id}/{artifact_action:apply}", _artifact)

    async def shutdown(application: web.Application) -> None:
        await hub.close()

    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(shutdown)
    return selected
