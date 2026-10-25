"""Authenticated local access to the desktop's existing collaboration service.

The rendezvous secret is local control data, never a provider credential. It is
published only after the owning desktop listener is ready and is never returned
by an HTTP route. Importing this module starts no service or model request.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any, BinaryIO

from aiohttp import web

from cli_providers import redact
from monitor_server import _check_origin
from monitor_service import MonitorError

RECORD_NAME = "office-bridge.json"
VERSION = 1
MAX_RECORD_BYTES = 16 * 1024
_ID = re.compile(r"[0-9a-f]{32}")
_SECRET = re.compile(r"[A-Za-z0-9_-]{32,128}")


class BridgeError(RuntimeError):
    """A safe bridge lifecycle error containing no secret material."""


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _text(value: Any, limit: int = 8000) -> str | None:
    return redact(value[:limit]) if isinstance(value, str) else None


def _artifact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    files = value.get("changed_files", [])
    return {"id": _text(value.get("id"), 32), "applied": value.get("applied") is True,
            "changed_files": [{"path": _text(item.get("path"), 1024),
                               "status": _text(item.get("status"), 40),
                               **{key: item.get(key, 0) for key in ("additions", "deletions")
                                  if type(item.get(key, 0)) is int}}
                              for item in files[:100] if isinstance(item, dict)],
            "changed_files_total": len(files)}


def compact_job(job: dict[str, Any], *, events: bool = False) -> dict[str, Any]:
    """Whitelist public telemetry; task prompts and execution internals stay private."""
    result = {key: job.get(key) for key in (
        "id", "provider", "kind", "status", "parent_job_id", "role", "task_mode",
        "created_at", "started_at", "finished_at", "elapsed_seconds", "cancel_requested",
        "model", "configured_model", "reasoning_effort", "transport", "execution_mode", "exit_code")}
    result = {key: _text(value, 240) if isinstance(value, str) else value for key, value in result.items()}
    result.update(latest_message=_text(job.get("latest_message")),
                  active_tool=_text(job.get("active_tool"), 240), error=_text(job.get("error")))
    if isinstance(job.get("usage"), dict):
        result["usage"] = {key: value for key, value in job["usage"].items()
                           if isinstance(key, str) and len(key) <= 80
                           and type(value) in (int, float) and 0 <= value <= 10**12}
    team = job.get("collaboration")
    if isinstance(team, dict):
        result["collaboration"] = {key: team.get(key) for key in ("phase", "round", "max_rounds")}
        result["collaboration"]["summary"] = _text(team.get("summary"))
        result["collaboration"]["members"] = [
            {key: _text(member.get(key), 240) for key in ("provider", "model", "reasoning_effort")}
            for member in team.get("members", [])[:3] if isinstance(member, dict)]
    if "artifact" in job:
        artifact = _artifact(job["artifact"])
        result["artifact"] = (artifact if events or artifact is None else
                              {key: artifact[key] for key in ("id", "applied", "changed_files_total")})
    if events:
        result["events"] = []
        for event in job.get("events", [])[-20:]:
            if not isinstance(event, dict) or event.get("kind") not in {"status", "message", "tool", "result", "error", "usage"}:
                continue
            # The MonitorService already filters private reasoning/native schema
            # payloads. Do not add raw CLI events or tool arguments here.
            result["events"].append({key: event.get(key) for key in ("id", "at", "kind", "status")})
            result["events"][-1].update(text=_text(event.get("text"), 2000),
                                         tool=_text(event.get("tool"), 240))
    return result


class OfficeBridge:
    """Own routes and a rendezvous record for exactly one MonitorService."""
    def __init__(self, service: Any, data_dir: Path | str) -> None:
        self.service = service
        self.workspace = str(Path(service.workspace).resolve())
        raw_data = Path(data_dir).expanduser().absolute()
        if any(_linked(path) for path in (raw_data, *raw_data.parents)):
            raise BridgeError("Bridge data directory cannot contain links or junctions.")
        self.data_dir = raw_data.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data_dir / RECORD_NAME
        self._nonce = secrets.token_urlsafe(24)
        self._token = secrets.token_urlsafe(32)
        self._handle: BinaryIO | None = None
        self._published = False
        self._acquire_owner()
        try:
            old = self._read_record()
            if old is not None and old["workspace"] != self.workspace:
                raise BridgeError("The existing bridge record belongs to another workspace; it was preserved.")
        except BaseException:
            self.close()
            raise

    def _acquire_owner(self) -> None:
        path = self.data_dir / "office-bridge.lock"
        if _linked(path):
            raise BridgeError("Bridge ownership lock cannot be a link.")
        handle = None
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            handle = os.fdopen(os.open(path, flags, 0o600), "r+b")
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("not a regular lock")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle = handle
        except OSError as error:
            if handle is not None:
                handle.close()
            raise BridgeError("Another bridge owns this data directory, or its lock is unavailable.") from error

    def _read_record(self) -> dict[str, Any] | None:
        if _linked(self.path):
            raise BridgeError("Bridge record cannot be a link or junction.")
        if not self.path.exists():
            return None
        try:
            if not self.path.is_file() or self.path.stat().st_size > MAX_RECORD_BYTES:
                raise ValueError("record size")
            def pairs(values):
                result = {}
                for key, value in values:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result
            value = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
            if (not isinstance(value, dict) or set(value) != {"version", "workspace", "port", "nonce", "token"}
                    or type(value["version"]) is not int or value["version"] != VERSION
                    or not isinstance(value["workspace"], str) or not value["workspace"]
                    or type(value["port"]) is not int or not 1 <= value["port"] <= 65535
                    or not isinstance(value["nonce"], str) or not _SECRET.fullmatch(value["nonce"])
                    or not isinstance(value["token"], str) or not _SECRET.fullmatch(value["token"])):
                raise ValueError("record schema")
            return value
        except (OSError, UnicodeError, ValueError) as error:
            raise BridgeError("The existing bridge record is unrecognized; it was preserved.") from error

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/bridge/info", self._info)
        app.router.add_get("/api/bridge/status", self._status)
        app.router.add_post("/api/bridge/start", self._start)
        app.router.add_get("/api/bridge/jobs/{job_id}", self._job)
        app.router.add_post("/api/bridge/jobs/{job_id}/cancel", self._cancel)
        app.router.add_get("/api/bridge/jobs/{job_id}/changes", self._changes)

    def publish(self, port: int) -> None:
        if self._handle is None or type(port) is not int or not 1 <= port <= 65535:
            raise BridgeError("A live bridge owner and bound loopback port are required.")
        old = self._read_record()
        if old is not None and old["workspace"] != self.workspace:
            raise BridgeError("The existing bridge record belongs to another workspace; it was preserved.")
        record = {"version": VERSION, "workspace": self.workspace, "port": port,
                  "nonce": self._nonce, "token": self._token}
        temporary: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".office-bridge-", suffix=".tmp", dir=self.data_dir)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            self._published = True
        except OSError as error:
            raise BridgeError("Could not publish the local bridge record.") from error
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            if self._published:
                with contextlib.suppress(BridgeError, OSError):
                    record = self._read_record()
                    if (record is not None and record["nonce"] == self._nonce
                            and hmac.compare_digest(record["token"], self._token)):
                        self.path.unlink()
        finally:
            handle, self._handle = self._handle, None
            with contextlib.suppress(OSError):
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _authorize(self, request: web.Request) -> None:
        if self._handle is None:
            raise MonitorError("Local bridge is shutting down.", 503)
        try:
            local = ipaddress.ip_address(request.remote or "").is_loopback
        except ValueError:
            local = False
        if not local:
            raise MonitorError("Bridge requests must originate on this computer.", 403)
        authorization = request.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not _SECRET.fullmatch(token) or not hmac.compare_digest(token, self._token):
            raise MonitorError("Local bridge authentication is required.", 401)
        _check_origin(request)

    def _response(self, value: dict[str, Any], *, status: int = 200) -> web.Response:
        return web.json_response(value, status=status, headers={"Cache-Control": "no-store"},
                                 dumps=lambda item: json.dumps(item, ensure_ascii=False, allow_nan=False))

    async def _body(self, request: web.Request) -> Any:
        self._authorize(request)
        if request.content_type != "application/json":
            raise MonitorError("Bridge controls require application/json.", 415)
        try:
            raw = await request.read()
            self._authorize(request)
            if len(raw) > 64 * 1024:
                raise MonitorError("Bridge request exceeds its size limit.", 413)
            def pairs(values):
                result = {}
                for key, value in values:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result
            return json.loads(raw, object_pairs_hook=pairs,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError, RecursionError):
            raise MonitorError("Expected a strict JSON bridge request.") from None

    async def _info(self, request: web.Request) -> web.Response:
        try:
            self._authorize(request)
            return self._response({"version": VERSION, "workspace": self.workspace, "nonce": self._nonce})
        except MonitorError as error:
            return self._response({"type": "monitor_error", "message": str(error)}, status=error.status)

    async def _status(self, request: web.Request) -> web.Response:
        try:
            self._authorize(request)
            state = self.service.snapshot()
            providers = []
            for provider in state["providers"][:3]:
                providers.append({key: provider.get(key) for key in
                                  ("id", "name", "available", "auth", "phase", "model", "configured_model",
                                   "configured_reasoning_effort", "active_job_id", "team_run_id", "execution_mode")})
                providers[-1]["reason"] = _text(provider.get("reason"), 1000)
                providers[-1]["model_options"] = [
                    {key: option.get(key) for key in ("id", "name", "reasoning_efforts", "default_reasoning_effort")}
                    for option in provider.get("model_options", [])[:30] if isinstance(option, dict)]
            return self._response({"version": VERSION, "workspace": self.workspace,
                                   "providers": providers, "jobs": [compact_job(job) for job in state["jobs"][:20]]})
        except MonitorError as error:
            return self._response({"type": "monitor_error", "message": str(error)}, status=error.status)

    async def _start(self, request: web.Request) -> web.Response:
        try:
            data = await self._body(request)
            if (not isinstance(data, dict) or not {"provider", "prompt", "collaboration"} <= set(data)
                    or not set(data) <= {"provider", "prompt", "model", "reasoning_effort", "collaboration"}
                    or not isinstance(data["collaboration"], dict)):
                raise MonitorError("Bridge start requires an existing collaboration request; solo tasks are not accepted.")
            job = self.service.start_job(data["provider"], data["prompt"], data.get("model"),
                                         data.get("reasoning_effort"), data["collaboration"])
            return self._response({"type": "monitor_job", "job": compact_job(job)}, status=202)
        except MonitorError as error:
            return self._response({"type": "monitor_error", "message": str(error)}, status=error.status)

    def _get_job(self, request: web.Request) -> dict[str, Any]:
        job_id = request.match_info["job_id"]
        if not _ID.fullmatch(job_id):
            raise MonitorError("Collaboration not found.", 404)
        job = self.service.get_job(job_id)
        if job.get("kind") != "collaboration":
            raise MonitorError("Bridge routes accept only collaboration parent jobs.", 404)
        return job

    async def _job(self, request: web.Request) -> web.Response:
        try:
            self._authorize(request)
            job = self._get_job(request)
            children = [compact_job(child, events=True) for child in self.service.snapshot()["jobs"]
                        if child.get("parent_job_id") == job["id"]][:12]
            return self._response({"job": compact_job(job, events=True), "children": children,
                                   "artifact": _artifact(job.get("artifact"))})
        except MonitorError as error:
            return self._response({"type": "monitor_error", "message": str(error)}, status=error.status)

    async def _cancel(self, request: web.Request) -> web.Response:
        try:
            if await self._body(request) != {}:
                raise MonitorError("Bridge cancellation requires an empty JSON object.")
            job = self._get_job(request)
            cancelled = self.service.cancel_job(job["id"])
            return self._response({"type": "monitor_job", "job": compact_job(cancelled)}, status=202)
        except MonitorError as error:
            return self._response({"type": "monitor_error", "message": str(error)}, status=error.status)

    async def _changes(self, request: web.Request) -> web.Response:
        try:
            self._authorize(request)
            job = self._get_job(request)
            if job.get("artifact", {}).get("id") != job["id"]:
                raise MonitorError("Collaboration artifact not found.", 404)
            from collaboration_workspace import ArtifactError, ArtifactStore
            try:
                review = await asyncio.to_thread(ArtifactStore(self.service.workspace).review, job["id"])
            except ArtifactError as error:
                raise MonitorError(str(error), error.status) from None
            diff = review.get("diff", "")
            return self._response({"artifact": _artifact(review.get("artifact")),
                                   "diff": _text(diff, 64000), "truncated": len(diff) > 64000})
        except MonitorError as error:
            return self._response({"type": "monitor_error", "message": str(error)}, status=error.status)


def register_office_bridge(app: web.Application, service: Any, data_dir: Path | str) -> OfficeBridge:
    bridge = OfficeBridge(service, data_dir)
    try:
        bridge.register(app)
    except BaseException:
        bridge.close()
        raise
    return bridge
