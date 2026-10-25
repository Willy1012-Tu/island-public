"""Owned CLI jobs and public telemetry, independent of the island simulation."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from cli_providers import redact

PROVIDER_NAMES = {"codex": "Codex", "claude": "Claude 4.6", "antigravity": "Antigravity"}
PERSONAS = {
    "codex": {"name": "沉穩築路者", "description": "先看清路線，再一步步把事情做好。",
              "idle_lines": ["先把下一段路看清楚。", "工具放好了，等你交付任務。", "走一小段，讓思緒慢慢整理。"]},
    "claude": {"name": "細心觀察家", "description": "留意細節，也留意每個問題背後的脈絡。",
               "idle_lines": ["這片葉子的紋路很有意思。", "我在這裡，等下一個值得仔細看的問題。", "先停一下，看看周圍有什麼不同。"]},
    "antigravity": {"name": "好奇探險家", "description": "喜歡新線索，總想知道下一個轉角有什麼。",
                   "idle_lines": ["那邊好像有條沒走過的小路！", "下一次探險，會從哪個問題開始呢？", "先繞一圈，收集一點新靈感。"]},
}
TERMINAL = frozenset(("completed", "failed", "cancelled", "timed_out"))
EVENT_KINDS = frozenset(("status", "message", "tool", "result", "error", "usage"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: Any, limit: int = 8000) -> str:
    return value[:limit] if isinstance(value, str) else ""


class MonitorError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class _Job:
    data: dict[str, Any]
    prompt: str
    command_key: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=100))
    task: asyncio.Task[dict[str, Any]] | None = None
    started: float | None = None
    finished: float | None = None
    restored_elapsed: float = 0.0
    workspace: Path | None = None
    runner_options: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Team:
    settings: dict[str, Any]
    cancel: threading.Event
    children: set[str] = field(default_factory=set)
    workspace: Path | None = None


class MonitorService:
    """Run at most one user-started job for each configured CLI provider.

    Only callbacks and the blocking runner cross threads. All public state and
    subscriber notifications are owned by the asyncio event loop.
    """

    def __init__(self, workspace: str | Path, *, commands: Mapping[str, str] | None = None,
                 timeout: float = 300, runner: Callable[..., dict[str, Any]] | None = None,
                 discoverer: Callable[..., list[dict[str, Any]]] | None = None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Monitor timeout must be a finite positive number.")
        if runner is None or discoverer is None:
            from cli_providers import discover_providers, run_cli
            runner = runner or run_cli
            discoverer = discoverer or discover_providers
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("Monitor workspace must be an existing directory.")
        self.commands = dict(commands or {})
        self.timeout = timeout
        self.runner = runner
        self.discoverer = discoverer
        self.revision = 0
        self._providers: dict[str, dict[str, Any]] = {}
        self._provider_commands: dict[str, str | None] = {}
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._teams: dict[str, _Team] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=300)
        self._event_id = 0
        self._subscribers: set[Callable[[], None]] = set()
        self._closed = False
        self._refresh_lock = asyncio.Lock()
        self.refresh(notify=False)

    def refresh(self, *, notify: bool = True) -> None:
        """Discover synchronously during startup or an explicit synchronous call."""
        self._apply_discovery(self.discoverer(self.commands), notify=notify)

    async def refresh_async(self) -> None:
        """Keep native metadata discovery off-loop and apply results in order."""
        async with self._refresh_lock:
            if self._closed:
                return
            discovery = asyncio.create_task(asyncio.to_thread(self.discoverer, dict(self.commands)))
            try:
                found = await asyncio.shield(discovery)
            except asyncio.CancelledError:
                # A cancelled caller must not release the serialization lock
                # while its native metadata worker is still running.
                with contextlib.suppress(Exception):
                    await discovery
                raise
            except Exception as error:
                raise MonitorError(f"Provider discovery failed ({type(error).__name__}).", 503) from None
            if not self._closed:
                self._apply_discovery(found)

    def _apply_discovery(self, results: list[dict[str, Any]], *, notify: bool = True) -> None:
        found = {item["id"]: item for item in results
                 if isinstance(item, dict) and item.get("id") in PROVIDER_NAMES}
        for provider, name in PROVIDER_NAMES.items():
            previous = self._providers.get(provider, {})
            info = found.get(provider, {"id": provider, "name": name, "available": False,
                                        "reason": "CLI not found.", "auth": "unknown"})
            active = previous.get("active_job_id")
            team_run_id = previous.get("team_run_id")
            command_key = info.get("command") if isinstance(info.get("command"), str) else None
            auth = _text(info.get("auth"), 40) or "unknown"
            if not info.get("available"):
                auth = "unknown"
            elif previous.get("auth") == "verified" and self._provider_commands.get(provider) == command_key:
                auth = "verified"
            configured_model = (_text(info.get("configured_model"), 240)
                                or _text(info.get("model"), 240) or None)
            model_options = []
            seen_models = set()
            for option in info.get("model_options", []) if isinstance(info.get("model_options"), list) else []:
                if not isinstance(option, dict):
                    continue
                model_id = _text(option.get("id"), 240)
                if model_id and model_id not in seen_models:
                    entry = {"id": model_id, "name": _text(option.get("name"), 240) or model_id}
                    if isinstance(option.get("reasoning_efforts"), list):
                        efforts = list(dict.fromkeys(value for value in option["reasoning_efforts"]
                                                     if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", value)))
                        entry["reasoning_efforts"] = efforts
                        preferred = option.get("default_reasoning_effort")
                        entry["default_reasoning_effort"] = preferred if preferred in efforts else (efforts[0] if efforts else None)
                    model_options.append(entry)
                    seen_models.add(model_id)
            state = {"id": provider, "name": _text(info.get("name"), 80) or name,
                     "available": bool(info.get("available")), "reason": _text(info.get("reason")),
                     "auth": auth,
                     "active_job_id": active, "team_run_id": team_run_id,
                     "phase": "running" if active or team_run_id else (
                         "idle" if info.get("available") else "missing"),
                     "persona": copy.deepcopy(PERSONAS[provider]),
                     "last_activity": previous.get("last_activity"),
                     "transport": _text(info.get("transport"), 80) or provider,
                     "configured_model": configured_model,
                     "configured_reasoning_effort": _text(info.get("configured_reasoning_effort"), 40) or None,
                     "model_options": model_options,
                     "model": previous.get("model") or configured_model}
            for key in ("execution_mode", "execution_note"):
                if key in info:
                    state[key] = _text(info[key])
            self._providers[provider] = state
            self._provider_commands[provider] = command_key
        if notify:
            self._notify()

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    def snapshot(self) -> dict[str, Any]:
        return {"type": "monitor_state", "version": 1, "revision": self.revision,
                "workspace": str(self.workspace), "providers": copy.deepcopy(list(self._providers.values())),
                "jobs": [self._job_snapshot(job) for job in reversed(self._jobs.values())],
                "events": copy.deepcopy(list(self._events)),
                "idle_behavior": "scripted", "closed": self._closed}

    def export_history(self) -> dict[str, Any]:
        """Return bounded public records only, never private prompts or commands."""
        jobs = []
        for current in reversed(self._jobs.values()):
            cleaned = self._history_job(self._job_snapshot(current), interrupt=False)
            if cleaned is not None:
                jobs.append(self._job_snapshot(cleaned))
        events = [event for raw in self._events if (event := self._history_event(raw)) is not None]
        return {"type": "monitor_history", "version": 1, "saved_at": _now(),
                "jobs": jobs[:20], "events": events[-300:]}

    def restore_history(self, data: Any) -> None:
        """Restore display history without creating tasks, workers, or auth claims."""
        if any(job.data["status"] not in TERMINAL for job in self._jobs.values()):
            raise MonitorError("Cannot restore history while a task is active.", 409)
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("jobs"), list) or not isinstance(data.get("events", []), list)):
            raise ValueError("Unsupported desktop task history format.")
        restored: OrderedDict[str, _Job] = OrderedDict()
        interrupted = []
        for raw in reversed(data["jobs"][:20]):
            job = self._history_job(raw, interrupt=True)
            if job is None or job.data["id"] in restored:
                continue
            restored[job.data["id"]] = job
            if isinstance(raw, dict) and raw.get("status") in ("queued", "running"):
                interrupted.append(job)
        events = {}
        for raw in data.get("events", [])[-300:]:
            event = self._history_event(raw)
            if event is not None:
                events[event["id"]] = event
        self._jobs = restored
        self._teams.clear()
        self._events = deque((events[key] for key in sorted(events)), maxlen=300)
        ids = [event["id"] for event in self._events]
        ids.extend(event["id"] for job in self._jobs.values() for event in job.events)
        self._event_id = max(ids, default=0)
        for state in self._providers.values():
            state.update(auth="unknown", active_job_id=None, team_run_id=None,
                         phase="idle" if state["available"] else "missing")
        for job in self._jobs.values():
            state = self._providers[job.data["provider"]]
            state["last_activity"] = job.data["finished_at"] or job.data["created_at"]
            if job.data["model"]:
                state["model"] = job.data["model"]
        for job in interrupted:
            self._append_event(job, {"kind": "status", "status": "failed", "text": job.data["error"]})
        self._notify()

    @staticmethod
    def _history_time(value: Any) -> str | None:
        if not isinstance(value, str) or not value or len(value) > 80:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def _history_id(value: Any) -> str | None:
        return value if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                         and redact(value) == value) else None

    def _history_event(self, raw: Any, *, job_id: str | None = None,
                       provider: str | None = None) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or raw["kind"] not in EVENT_KINDS:
            return None
        event_id, at = raw.get("id"), self._history_time(raw.get("at"))
        owner = job_id or self._history_id(raw.get("job_id"))
        source = provider or raw.get("provider")
        if (type(event_id) is not int or not 0 < event_id < 2**63 or at is None or owner is None
                or not isinstance(source, str) or source not in PROVIDER_NAMES):
            return None
        event = {"id": event_id, "at": at, "job_id": owner, "provider": source,
                 "kind": raw["kind"], "text": redact(_text(raw.get("text")))}
        for key in ("tool", "status"):
            if isinstance(raw.get(key), str):
                event[key] = redact(_text(raw[key], 240))
        for key in ("delta", "public_output"):
            if raw.get(key) is True:
                event[key] = True
        return event

    def _history_job(self, raw: Any, *, interrupt: bool) -> _Job | None:
        if not isinstance(raw, dict):
            return None
        job_id, source, status = self._history_id(raw.get("id")), raw.get("provider"), raw.get("status")
        created = self._history_time(raw.get("created_at"))
        if (job_id is None or not isinstance(source, str) or source not in PROVIDER_NAMES
                or not isinstance(status, str) or status not in TERMINAL | {"queued", "running"}
                or created is None):
            return None
        active = status in ("queued", "running") and interrupt
        elapsed = raw.get("elapsed_seconds", 0)
        if type(elapsed) not in (int, float) or not 0 <= elapsed <= 365 * 24 * 60 * 60:
            elapsed = 0
        usage = raw.get("usage")
        metrics = {key: value for key, value in list(usage.items())[:32]
                   if isinstance(key, str) and len(key) <= 80 and redact(key) == key and type(value) in (int, float)
                   and 0 <= value <= 10**18} if isinstance(usage, dict) else {}
        error = redact(_text(raw.get("error"))) or None
        if active:
            error = "上次關閉時仍在執行，已中止；請手動重新交辦。"
        data = {"id": job_id, "provider": source, "prompt": redact(_text(raw.get("prompt"))),
                "status": "failed" if active else status, "created_at": created,
                "started_at": self._history_time(raw.get("started_at")),
                "finished_at": _now() if active else self._history_time(raw.get("finished_at")),
                "transport": redact(_text(raw.get("transport"), 80)) or self._providers[source]["transport"],
                "configured_model": redact(_text(raw.get("configured_model"), 240)) or None,
                "reasoning_effort": redact(_text(raw.get("reasoning_effort"), 40)) or None,
                "model": redact(_text(raw.get("model"), 240)) or None,
                "session_id": redact(_text(raw.get("session_id"), 240)) or None,
                "latest_message": redact(_text(raw.get("latest_message"))),
                "active_tool": None, "usage": metrics, "error": error,
                "exit_code": raw.get("exit_code") if type(raw.get("exit_code")) is int else None,
                "cancel_requested": not active and raw.get("cancel_requested") is True}
        data["kind"] = raw.get("kind") if raw.get("kind") in ("solo", "collaboration", "delegation") else "solo"
        data["parent_job_id"] = self._history_id(raw.get("parent_job_id"))
        data["role"] = raw.get("role") if raw.get("role") in ("planning", "worker", "synthesis") else None
        data["task_mode"] = raw.get("task_mode") if raw.get("task_mode") in ("read", "write") else None
        if raw.get("execution_mode") in ("read_only", "workspace_write"):
            data["execution_mode"] = raw["execution_mode"]
        if raw.get("write_strategy") == "proposals":
            data["write_strategy"] = "proposals"
        if data["kind"] == "collaboration":
            collaboration = self._history_collaboration(raw.get("collaboration"))
            if collaboration is not None:
                if active:
                    collaboration["phase"] = "failed"
                data["collaboration"] = collaboration
            artifact = self._history_artifact(raw.get("artifact"), job_id)
            if artifact is not None:
                data["artifact"] = artifact
        job = _Job(data, prompt="", restored_elapsed=float(elapsed))
        records = {}
        for event in raw.get("events", [])[-100:] if isinstance(raw.get("events"), list) else []:
            cleaned = self._history_event(event, job_id=job_id, provider=source)
            if cleaned is not None:
                records[cleaned["id"]] = cleaned
        job.events.extend(records[key] for key in sorted(records))
        return job

    @staticmethod
    def _history_collaboration(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("members"), list):
            return None
        members = []
        seen = set()
        for member in raw["members"][:3]:
            if not isinstance(member, dict) or not isinstance(member.get("provider"), str):
                continue
            provider = member["provider"]
            if provider not in PROVIDER_NAMES or provider in seen:
                continue
            seen.add(provider)
            members.append({"provider": provider, "model": redact(_text(member.get("model"), 240)) or None,
                            "reasoning_effort": redact(_text(member.get("reasoning_effort"), 40)) or None})
        phase = redact(_text(raw.get("phase"), 80)) or "queued"
        round_number = raw.get("round")
        result = {"members": members, "phase": phase,
                  "round": round_number if type(round_number) is int and 0 <= round_number <= 4 else 0,
                  "max_rounds": 4}
        if isinstance(raw.get("summary"), str):
            result["summary"] = redact(raw["summary"])
        return result

    def _history_artifact(self, raw: Any, parent_id: str) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or raw.get("id") != parent_id or self._history_id(parent_id) is None:
            return None
        if not isinstance(raw.get("changed_files"), list):
            return None
        files = []
        for record in raw["changed_files"][:2000]:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                continue
            path = record["path"].replace("\\", "/")
            if (not path or len(path) > 1024 or ":" in path or any(part in ("", ".", "..") for part in path.split("/"))
                    or any(ord(char) < 32 for char in path)):
                continue
            files.append({"path": redact(path), "status": redact(_text(record.get("status"), 40)),
                          **{key: record[key] if type(record.get(key)) is int and 0 <= record[key] <= 10**9 else 0
                             for key in ("additions", "deletions")}})
        base = f"/api/monitor/jobs/{parent_id}"
        return {"id": parent_id, "changed_files": files, "diff_url": base + "/changes",
                "download_url": base + "/patch", "applied": raw.get("applied") is True}

    def _selection(self, provider: Any, model: Any, reasoning_effort: Any) -> dict[str, Any]:
        if not isinstance(provider, str) or provider not in self._providers:
            raise MonitorError("Choose Codex, Claude, or Antigravity.")
        state = self._providers[provider]
        if not state["available"]:
            raise MonitorError(state["reason"] or "This CLI is not installed.", 409)
        if state["active_job_id"] or state.get("team_run_id"):
            raise MonitorError("This provider already has an active task.", 409)
        selected_model = state["configured_model"]
        if model is not None:
            allowed = {option["id"] for option in state["model_options"]}
            if selected_model:
                allowed.add(selected_model)
            if not isinstance(model, str) or model not in allowed:
                raise MonitorError("Choose a configured model for this provider.")
            selected_model = model
        selected_effort = None
        if provider == "codex":
            option = next((item for item in state["model_options"] if item["id"] == selected_model), {})
            supported = option.get("reasoning_efforts", [])
            if reasoning_effort is not None:
                if not isinstance(reasoning_effort, str) or reasoning_effort not in supported:
                    raise MonitorError("Choose a supported reasoning effort for the selected Codex model.")
                selected_effort = reasoning_effort
            elif state["configured_reasoning_effort"] in supported:
                selected_effort = state["configured_reasoning_effort"]
            else:
                selected_effort = option.get("default_reasoning_effort")
        elif reasoning_effort is not None:
            raise MonitorError("Reasoning effort can only be selected for Codex tasks.")
        return {"provider": provider, "model": selected_model, "reasoning_effort": selected_effort,
                "command": self._provider_commands.get(provider) or self.commands.get(provider),
                "command_key": self._provider_commands.get(provider), "transport": state["transport"]}

    def _new_job(self, selection: dict[str, Any], prompt: str, *, kind: str = "solo",
                 parent_job_id: str | None = None, role: str | None = None,
                 task_mode: str | None = None) -> _Job:
        job_id = uuid.uuid4().hex
        raw_prompt = prompt.strip()
        job = _Job({"id": job_id, "provider": selection["provider"], "prompt": redact(raw_prompt), "status": "queued",
                    "kind": kind, "parent_job_id": parent_job_id, "role": role, "task_mode": task_mode,
                    "created_at": _now(), "started_at": None, "finished_at": None,
                    "transport": selection["transport"], "configured_model": selection["model"],
                    "reasoning_effort": selection["reasoning_effort"],
                    "model": selection["model"], "session_id": None, "latest_message": "", "active_tool": None,
                    "usage": {}, "error": None, "exit_code": None, "cancel_requested": False},
                   prompt=raw_prompt, command_key=selection["command_key"],
                   runner_options={"command": selection["command"]})
        self._jobs[job_id] = job
        self._append_event(job, {"kind": "status", "text": "Task queued.", "status": "queued"})
        return job

    def start_job(self, provider: Any, prompt: Any, model: Any = None,
                  reasoning_effort: Any = None, collaboration: Any = None) -> dict[str, Any]:
        if self._closed:
            raise MonitorError("Monitor is shutting down.", 503)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
            raise MonitorError("Task prompt must contain 1 to 8000 characters.")
        lead = self._selection(provider, model, reasoning_effort)
        selections = {provider: lead}
        if collaboration is not None:
            if (not isinstance(collaboration, dict) or set(collaboration) != {"members"}
                    or not isinstance(collaboration["members"], list) or not 1 <= len(collaboration["members"]) <= 2):
                raise MonitorError("Collaboration requires one or two peer members.")
            for member in collaboration["members"]:
                if (not isinstance(member, dict) or "provider" not in member
                        or not set(member) <= {"provider", "model", "reasoning_effort"}):
                    raise MonitorError("A peer requires provider and optional model/reasoning effort.")
                peer = member["provider"]
                if not isinstance(peer, str) or peer in selections:
                    raise MonitorError("Collaboration members must be distinct from the lead and each other.")
                selections[peer] = self._selection(peer, member.get("model"), member.get("reasoning_effort"))
        loop = asyncio.get_running_loop()
        job = self._new_job(lead, prompt, kind="collaboration" if collaboration is not None else "solo")
        job_id = job.data["id"]
        if collaboration is not None:
            public_members = [{"provider": choice["provider"], "model": choice["model"],
                               "reasoning_effort": choice["reasoning_effort"]} for choice in selections.values()]
            job.data["collaboration"] = {"members": public_members, "phase": "queued", "round": 0, "max_rounds": 4}
            self._teams[job_id] = _Team({"lead": provider, "members": copy.deepcopy(selections),
                                        "workspace": str(self.workspace), "prompt": job.prompt}, job.cancel)
        for member, choice in selections.items():
            state = self._providers[member]
            state.update(active_job_id=job_id, phase="running", last_activity=job.data["created_at"])
            if collaboration is not None:
                state["team_run_id"] = job_id
            if choice["model"]:
                state["model"] = choice["model"]
        self._prune_jobs()
        job.task = loop.create_task(self._run(job), name=f"monitor-{provider}-{job_id[:8]}")
        self._notify()
        return self._job_snapshot(job)

    def get_job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            raise MonitorError("Task not found.", 404)
        return self._job_snapshot(self._jobs[job_id])

    def team_settings(self, parent_id: str) -> dict[str, Any]:
        team = self._teams.get(parent_id)
        if team is None:
            raise MonitorError("Collaboration not found.", 404)
        return copy.deepcopy(team.settings)

    def team_cancel_event(self, parent_id: str) -> threading.Event:
        team = self._teams.get(parent_id)
        if team is None:
            raise MonitorError("Collaboration not found.", 404)
        return team.cancel

    def update_team(self, parent_id: str, **changes: Any) -> None:
        parent = self._jobs.get(parent_id)
        if parent is None or parent.data.get("kind") != "collaboration":
            raise MonitorError("Collaboration not found.", 404)
        if not set(changes) <= {"phase", "round", "summary", "artifact"}:
            raise MonitorError("Unknown collaboration update.")
        for key in ("phase", "summary"):
            if key in changes:
                if not isinstance(changes[key], str):
                    raise MonitorError("Collaboration text must be a string.")
                parent.data["collaboration"][key] = redact(_text(changes[key], 80 if key == "phase" else 8000))
        if "round" in changes:
            if type(changes["round"]) is not int or not 0 <= changes["round"] <= 4:
                raise MonitorError("Collaboration round must be between zero and four.")
            parent.data["collaboration"]["round"] = changes["round"]
        if "artifact" in changes:
            artifact = self._history_artifact(changes["artifact"], parent_id)
            if artifact is None:
                raise MonitorError("Invalid collaboration artifact.")
            parent.data["artifact"] = artifact
        if changes.get("summary"):
            self._append_event(parent, {"kind": "status", "text": parent.data["collaboration"]["summary"],
                                        "status": parent.data["collaboration"]["phase"]})
        self._notify()

    def record_team_outcome(self, parent_id: str, child_id: str, *, status: str,
                            output: str, error: str | None = None) -> None:
        """Record host-validated semantics after the owned CLI turn has stopped."""
        parent, child = self._jobs.get(parent_id), self._jobs.get(child_id)
        if (parent is None or parent_id not in self._teams or parent.data.get("kind") != "collaboration"
                or parent.data["status"] in TERMINAL or child is None
                or child.data.get("kind") != "delegation" or child.data.get("parent_job_id") != parent_id):
            raise MonitorError("Validated outcomes require a child of an active collaboration.", 409)
        if child.data["status"] not in TERMINAL:
            raise MonitorError("The delegated CLI must stop before recording its outcome.", 409)
        if status not in ("completed", "failed") or not isinstance(output, str) or (error is not None and not isinstance(error, str)):
            raise MonitorError("A validated outcome requires completed/failed status and public text.")
        if child.data["status"] in ("cancelled", "timed_out") or (status == "completed" and child.data["status"] != "completed"):
            raise MonitorError("A failed or cancelled CLI turn cannot become a completed report.", 409)
        child.data["status"] = status
        child.data["latest_message"] = redact(output)
        child.data["error"] = redact(error) if error is not None else (None if status == "completed" else child.data["error"])
        self._append_event(child, {"kind": "result", "status": status, "public_output": True,
                                   "text": child.data["latest_message"] or child.data["error"] or "Validated task outcome."})
        self._notify()

    async def run_team_turn(self, parent_id: str, provider: str, prompt: str, *, role: str,
                            task_mode: str = "read", output_schema: dict[str, Any] | None = None,
                            native_schema: bool = True, proposal_mode: bool = False,
                            workspace: Path) -> dict[str, Any]:
        team = self._teams.get(parent_id)
        parent = self._jobs.get(parent_id)
        if team is None or parent is None or provider not in team.settings["members"]:
            raise MonitorError("Unknown collaboration member.")
        if self._closed or team.cancel.is_set() or parent.data["status"] in TERMINAL:
            return {"status": "cancelled", "exit_code": None, "job_id": None, "latest_message": ""}
        if role not in ("planning", "worker", "synthesis") or task_mode not in ("read", "write"):
            raise MonitorError("Invalid delegated role or task mode.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 64000:
            raise MonitorError("A delegated prompt must contain one to 64000 characters.")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise MonitorError("Delegated output schema must be an object.")
        if type(native_schema) is not bool:
            raise MonitorError("Native schema selection must be a boolean.")
        if type(proposal_mode) is not bool:
            raise MonitorError("Proposal mode must be a boolean.")
        if proposal_mode and task_mode != "write":
            raise MonitorError("Proposal mode requires a delegated write task.")
        shared = Path(workspace).resolve()
        if not shared.is_dir():
            raise MonitorError("Shared collaboration workspace does not exist.")
        if (shared == self.workspace or shared.is_relative_to(self.workspace)
                or self.workspace.is_relative_to(shared)):
            raise MonitorError("Collaboration turns must use an isolated project copy.")
        if team.workspace is not None and shared != team.workspace:
            raise MonitorError("Every collaboration turn must use the same isolated project copy.")
        team.workspace = shared
        state = self._providers[provider]
        if state.get("team_run_id") != parent_id or state.get("active_job_id") not in (None, parent_id):
            raise MonitorError("Collaboration member already has an active turn.", 409)
        job = self._new_job(team.settings["members"][provider], prompt, kind="delegation",
                            parent_job_id=parent_id, role=role, task_mode=task_mode)
        job.cancel = team.cancel
        job.workspace = shared
        job.data["execution_mode"] = "workspace_write" if task_mode == "write" and not proposal_mode else "read_only"
        if proposal_mode:
            job.data["write_strategy"] = "proposals"
        job.runner_options.update(shared_workspace=True, execution_mode=job.data["execution_mode"])
        if output_schema is not None:
            job.runner_options["output_schema"] = copy.deepcopy(output_schema)
            if not native_schema:
                job.runner_options["native_schema"] = False
        job_id = job.data["id"]
        team.children.add(job_id)
        state.update(active_job_id=job_id, phase="running", last_activity=job.data["created_at"])
        self._prune_jobs()
        job.task = asyncio.create_task(self._run(job), name=f"team-{provider}-{job_id[:8]}")
        self._notify()
        try:
            result = await job.task
            return {**result, "job_id": job_id, "latest_message": job.data["latest_message"]}
        finally:
            team.children.discard(job_id)

    def cancel_job(self, job_id: Any) -> dict[str, Any]:
        if not isinstance(job_id, str) or job_id not in self._jobs:
            raise MonitorError("Task not found.", 404)
        job = self._jobs[job_id]
        team_id = job.data.get("parent_job_id") or (job_id if job.data.get("kind") == "collaboration" else None)
        if team_id in self._teams and self._jobs[team_id].data["status"] not in TERMINAL:
            parent = self._jobs[team_id]
            team = self._teams[team_id]
            team.cancel.set()
            for member in (parent, *(self._jobs[key] for key in team.children if key in self._jobs)):
                if member.data["status"] not in TERMINAL and not member.data["cancel_requested"]:
                    member.data["cancel_requested"] = True
                    self._append_event(member, {"kind": "status", "text": "Collaboration cancellation requested.",
                                                "status": "cancel_requested"})
            self._notify()
            return self._job_snapshot(job)
        if job.data["status"] not in TERMINAL and not job.cancel.is_set():
            job.cancel.set()
            job.data["cancel_requested"] = True
            self._append_event(job, {"kind": "status", "text": "Cancellation requested.",
                                     "status": "cancel_requested"})
            self._notify()
        return self._job_snapshot(job)

    async def _run(self, job: _Job) -> dict[str, Any]:
        if job.data.get("kind") == "collaboration":
            return await self._run_collaboration(job)
        loop = asyncio.get_running_loop()
        provider = job.data["provider"]
        if job.cancel.is_set():
            result = {"status": "cancelled", "exit_code": None}
        else:
            job.started = time.monotonic()
            job.data.update(status="running", started_at=_now())
            self._append_event(job, {"kind": "status", "text": "Task started.", "status": "running"})
            self._notify()

            def emit(event: dict[str, Any]) -> None:
                if not self._closed and isinstance(event, dict):
                    if "output_schema" in job.runner_options and (event.get("kind") == "message"
                            or event.get("kind") == "result" and event.get("public_output")):
                        return
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(self._receive_event, job.data["id"], dict(event))

            try:
                options = {**job.runner_options, "timeout": self.timeout,
                           "model": job.data["configured_model"]}
                if job.data["reasoning_effort"] is not None:
                    options["reasoning_effort"] = job.data["reasoning_effort"]
                result = await asyncio.to_thread(
                    self.runner, provider, job.prompt, job.workspace or self.workspace, emit, job.cancel,
                    **options)
                if not isinstance(result, dict) or result.get("status") not in TERMINAL:
                    result = {"status": "failed", "error": "Provider returned an invalid task result."}
            except Exception as exc:
                result = {"status": "failed", "error": f"Task runner failed ({type(exc).__name__})."}
        return self._finish_job(job, result)

    async def _run_collaboration(self, job: _Job) -> dict[str, Any]:
        team = self._teams[job.data["id"]]
        job.started = time.monotonic()
        job.data.update(status="running", started_at=_now())
        self._append_event(job, {"kind": "status", "text": "Collaboration started.", "status": "running"})
        self._notify()
        result: dict[str, Any] = {"status": "cancelled", "exit_code": None}
        try:
            if not self._closed and not team.cancel.is_set():
                from collaboration_service import CollaborationCoordinator
                result = await CollaborationCoordinator(self).run(job.data["id"])
                if not isinstance(result, dict) or result.get("status") not in TERMINAL:
                    result = {"status": "failed", "error": "Collaboration returned an invalid result."}
        except Exception as error:
            result = {"status": "failed", "error": f"Collaboration failed ({type(error).__name__})."}
        finally:
            pending = [self._jobs[key].task for key in tuple(team.children)
                       if key in self._jobs and self._jobs[key].task is not None and not self._jobs[key].task.done()]
            if pending:
                was_cancelled = team.cancel.is_set()
                team.cancel.set()
                await asyncio.gather(*pending, return_exceptions=True)
                if result.get("status") == "completed" and not was_cancelled:
                    result = {"status": "failed", "error": "Collaboration ended before delegated work completed."}
            if team.cancel.is_set() and result.get("status") == "completed":
                result = {**result, "status": "cancelled"}
            if team.cancel.is_set() and job.data["cancel_requested"]:
                result = {**result, "status": "cancelled", "error": None}
        return self._finish_job(job, result)

    def _finish_job(self, job: _Job, result: dict[str, Any]) -> dict[str, Any]:
        provider = job.data["provider"]
        job.finished = time.monotonic()
        status = result["status"]
        job.data.update(status=status, finished_at=_now(), active_tool=None,
                        exit_code=result.get("exit_code"))
        for key in ("model", "session_id"):
            if isinstance(result.get(key), str):
                job.data[key] = _text(result[key], 240)
        if isinstance(result.get("usage"), dict):
            job.data["usage"] = copy.deepcopy(result["usage"])
        if result.get("error"):
            job.data["error"] = redact(_text(result["error"]))
        output = result.get("output")
        if isinstance(output, str):
            if "output_schema" in job.runner_options:
                try:
                    structured = json.loads(output)
                    summary = structured.get("summary") if isinstance(structured, dict) else None
                    if isinstance(summary, str):
                        job.data["latest_message"] = redact(summary)
                except (ValueError, RecursionError):
                    pass
            else:
                job.data["latest_message"] = redact(output)
        is_parent = job.data.get("kind") == "collaboration"
        if is_parent:
            job.data["collaboration"]["phase"] = status
            if result.get("artifact") is not None:
                artifact = self._history_artifact(result["artifact"], job.data["id"])
                if artifact is not None:
                    job.data["artifact"] = artifact
        state = self._providers[provider]
        if not is_parent and self._provider_commands.get(provider) == job.command_key:
            if status == "completed":
                state["auth"] = "verified"
            elif job.data["error"] and re.search(
                    r"authenticat|\blog\s*in\b|\blogin\b|\bsign\s*in\b|not logged|登入|未授權",
                    job.data["error"], re.IGNORECASE):
                state["auth"] = "required"
        team_id = job.data.get("parent_job_id")
        reserved = team_id and state.get("team_run_id") == team_id
        state.update(active_job_id=team_id if reserved else None,
            phase="running" if reserved else ("missing" if not state["available"] else (
            "error" if status in ("failed", "timed_out") else "idle")),
            last_activity=job.data["finished_at"], model=job.data["model"] or state.get("model"))
        if is_parent:
            for member in self._teams[job.data["id"]].settings["members"]:
                member_state = self._providers[member]
                if member_state.get("team_run_id") == job.data["id"]:
                    member_state.update(active_job_id=None, team_run_id=None,
                                        phase="missing" if not member_state["available"] else (
                                            "error" if status in ("failed", "timed_out") else "idle"))
        self._append_event(job, {"kind": "status", "status": status,
                                 "text": job.data["error"] or f"Task {status}."})
        self._prune_jobs()
        self._notify()
        return result

    def _receive_event(self, job_id: str, event: dict[str, Any]) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.data["status"] in TERMINAL or self._closed:
            return
        kind = event.get("kind")
        if kind not in EVENT_KINDS:
            return
        event = {**event, "kind": kind, "text": _text(event.get("text"))}
        if (kind == "message" or (kind == "result" and event.get("public_output") is True)) and event["text"]:
            job.data["latest_message"] = (
                (job.data["latest_message"] + event["text"])[-8000:] if event.get("delta")
                else event["text"])
        if kind == "tool":
            done = event.get("status") in ("completed", "failed", "cancelled", "finished", "done")
            job.data["active_tool"] = None if done else _text(event.get("tool"), 240) or "tool"
        if kind == "error" and event["text"]:
            job.data["error"] = event["text"]
        for key in ("model", "session_id"):
            if isinstance(event.get(key), str):
                job.data[key] = _text(event[key], 240)
        if isinstance(event.get("usage"), dict):
            job.data["usage"] = copy.deepcopy(event["usage"])
        state = self._providers[job.data["provider"]]
        state["last_activity"] = _now()
        if job.data["model"]:
            state["model"] = job.data["model"]
        self._append_event(job, event)
        self._notify()

    def _append_event(self, job: _Job, event: dict[str, Any]) -> None:
        self._event_id += 1
        record = {"id": self._event_id, "at": _now(), "job_id": job.data["id"],
                  "provider": job.data["provider"], "kind": event["kind"], "text": _text(event.get("text"))}
        for key in ("tool", "status"):
            if isinstance(event.get(key), str):
                record[key] = _text(event[key], 240)
        if event.get("delta") is True:
            record["delta"] = True
        if event.get("public_output") is True:
            record["public_output"] = True
        job.events.append(record)
        self._events.append(record)

    def _job_snapshot(self, job: _Job) -> dict[str, Any]:
        elapsed = job.restored_elapsed if job.started is None else (job.finished or time.monotonic()) - job.started
        return {**copy.deepcopy(job.data), "elapsed_seconds": round(max(0, elapsed), 3),
                "events": copy.deepcopy(list(job.events))}

    def _prune_jobs(self) -> None:
        for job_id, job in list(self._jobs.items()):
            if len(self._jobs) <= 20:
                break
            parent_id = job.data.get("parent_job_id")
            if parent_id and parent_id in self._jobs:
                continue
            family = [key for key, member in self._jobs.items()
                      if key == job_id or member.data.get("parent_job_id") == job_id]
            if all(self._jobs[key].data["status"] in TERMINAL for key in family):
                for key in family:
                    self._jobs.pop(key, None)
                self._teams.pop(job_id, None)

    def _notify(self) -> None:
        self.revision += 1
        for callback in tuple(self._subscribers):
            callback()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for job in self._jobs.values():
            if job.data["status"] not in TERMINAL:
                job.cancel.set()
                job.data["cancel_requested"] = True
        tasks = [job.task for job in self._jobs.values() if job.task is not None and not job.task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._subscribers.clear()
