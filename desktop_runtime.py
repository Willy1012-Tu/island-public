"""Own one local aiohttp backend on a thread separate from the desktop UI.

The controller never discovers, reuses, or stops an existing browser server.
Port zero asks the operating system for a new private listener for this window.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
import logging
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from aiohttp import web

LOGGER = logging.getLogger("island.desktop")


class _HistoryPersistence:
    """Coalesce public-history writes without holding the server's event loop."""

    def __init__(self, service: Any, path: Path):
        self.service = service
        self.path = path
        self._handle: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._dirty = False
        self._closed = False
        try:
            if path.is_file():
                if path.stat().st_size > 96 * 1024 * 1024:
                    raise ValueError("History file exceeds the supported size.")
                service.restore_history(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
            LOGGER.warning("Could not restore desktop task history: %s", error)
        self._unsubscribe = service.subscribe(self.changed)

    def changed(self) -> None:
        self._dirty = True
        if not self._closed and self._handle is None and self._task is None:
            # One write per half second during a long stream also preserves an
            # in-flight job if the desktop process exits unexpectedly.
            self._handle = asyncio.get_running_loop().call_later(0.5, self._begin_save)

    def _begin_save(self) -> None:
        self._handle = None
        if self._closed:
            return
        self._dirty = False
        data = self.service.export_history()
        self._task = asyncio.create_task(self._save(data), name="desktop-history-save")
        self._task.add_done_callback(self._saved)

    def _saved(self, task: asyncio.Task[None]) -> None:
        self._task = None
        if self._dirty and not self._closed:
            self.changed()

    async def _save(self, data: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._write_atomic, data)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            LOGGER.warning("Could not save desktop task history: %s", error)

    def _write_atomic(self, data: dict[str, Any]) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".history-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(data, output, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)

    async def close(self) -> None:
        self._closed = True
        self._unsubscribe()
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._task is not None:
            await self._task
        await self._save(self.service.export_history())


def _timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Timeout must be a finite positive number.")
    return value


class DesktopServer:
    """Start and stop only the backend owned by this desktop application.

    ``app_factory`` is an optional callable receiving the same keyword options
    as ``web_server.create_app``. Tests can inject a small, model-free app.
    ``app_kwargs`` supplies startup settings; the workspace argument remains
    authoritative and cannot be replaced through this mapping or a browser.
    """

    def __init__(
        self, workspace: Path | str, data_dir: Path | str, *, host: str = "127.0.0.1",
        port: int = 0, app_factory: Callable[..., Any] | None = None,
        app_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        workspace_path = Path(workspace).resolve()
        if not workspace_path.is_dir():
            raise ValueError("Desktop workspace must be an existing directory.")
        try:
            local_host = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local_host = False
        if not local_host:
            raise ValueError("Desktop backend must use a loopback host.")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Desktop port must be an integer between 0 and 65535.")
        self.workspace = workspace_path
        self.data_dir = Path(data_dir).resolve()
        self.host = host
        self.port = port
        self._app_factory = app_factory
        self._app_kwargs = dict(app_kwargs or {})
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown: asyncio.Event | None = None
        self._url: str | None = None
        self._error: str | None = None

    @property
    def url(self) -> str | None:
        with self._lock:
            return self._url

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive() and self._url
                        and not self._stop_requested.is_set())

    def start(self, timeout: float = 15) -> str:
        """Return the ready monitor URL, raising if startup fails or times out."""
        _timeout(timeout)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._stop_requested.is_set():
                    raise RuntimeError("Desktop backend is still stopping.")
                if self._url is not None:
                    return self._url
            else:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self._ready = threading.Event()
                self._stop_requested = threading.Event()
                self._url = None
                self._error = None
                self._thread = threading.Thread(target=self._thread_main,
                                                name="island-desktop-server", daemon=True)
                self._thread.start()
            ready = self._ready
        if not ready.wait(timeout):
            message = f"Desktop backend did not start within {timeout:g} seconds."
            with self._lock:
                self._error = message
            self._request_stop()
            raise TimeoutError(message)
        with self._lock:
            if self._error is not None:
                raise RuntimeError(self._error)
            if self._url is None or self._stop_requested.is_set():
                raise RuntimeError("Desktop backend startup was stopped.")
            return self._url

    def stop(self, timeout: float = 10) -> None:
        """Wait for this server's cleanup, including its owned CLI job workers."""
        _timeout(timeout)
        self._request_stop()
        with self._lock:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout)
        if thread.is_alive():
            message = f"Desktop backend did not stop within {timeout:g} seconds."
            with self._lock:
                self._error = message
            raise TimeoutError(message)

    def _request_stop(self) -> None:
        with self._lock:
            self._stop_requested.set()
            loop, shutdown = self._loop, self._shutdown
        if loop is not None and shutdown is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(shutdown.set)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as error:
            with self._lock:
                self._error = f"{type(error).__name__}: {error}"[:2000]
        finally:
            with self._lock:
                self._url = None
                self._loop = None
                self._shutdown = None
            self._ready.set()

    async def _serve(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._shutdown = asyncio.Event()
        if self._stop_requested.is_set():
            return
        options = {"brain": "demo", **self._app_kwargs,
                   "monitor_workspace": self.workspace}
        factory = self._app_factory
        if factory is None:
            from web_server import create_app
            factory = create_app
        app = factory(**options)
        if inspect.isawaitable(app):
            app = await app
        if not isinstance(app, web.Application):
            raise TypeError("Desktop app factory must return an aiohttp Application.")
        from monitor_server import MONITOR_KEY
        monitor = app.get(MONITOR_KEY)
        bridge = None
        if monitor is not None:
            from office_bridge import register_office_bridge
            bridge = register_office_bridge(app, monitor.service, self.data_dir)
        history = _HistoryPersistence(monitor.service, self.data_dir / "history.json") if monitor is not None else None
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            if self._stop_requested.is_set():
                return
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
            addresses = runner.addresses
            if not addresses:
                raise RuntimeError("Desktop backend did not expose a listener.")
            address = next((item for item in addresses if len(item) == 2), addresses[0])
            hostname, bound_port = address[0], address[1]
            if ":" in hostname:
                hostname = f"[{hostname}]"
            if bridge is not None:
                bridge.publish(bound_port)
            with self._lock:
                self._url = f"http://{hostname}:{bound_port}/monitor"
            self._ready.set()
            if self._stop_requested.is_set():
                self._shutdown.set()
            await self._shutdown.wait()
        finally:
            try:
                if bridge is not None:
                    bridge.close()
            finally:
                try:
                    await runner.cleanup()
                finally:
                    if history is not None:
                        await history.close()
