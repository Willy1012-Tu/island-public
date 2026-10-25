"""Single-instance ownership and authenticated, local-only focus requests.

The operating-system file lock is authoritative; the JSON file is only a
rendezvous record. Crashed processes automatically lose their lock. No PID is
ever signalled, and no fixed or externally reachable TCP port is used.
"""

from __future__ import annotations

import contextlib
import errno
import hmac
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_MAX_MESSAGE = 2048
_STARTUP_TIMEOUT = 2.0
_SOCKET_TIMEOUT = 0.25


class SingleInstanceError(RuntimeError):
    """An existing desktop owner could not acknowledge a focus request."""


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise
    return True


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_message(connection: socket.socket) -> dict | None:
    """Accept one small UTF-8 JSON line, without unbounded socket reads."""
    message = bytearray()
    try:
        while len(message) <= _MAX_MESSAGE:
            part = connection.recv(min(512, _MAX_MESSAGE + 1 - len(message)))
            if not part:
                return None
            message.extend(part)
            if b"\n" in part:
                line, _, _ = message.partition(b"\n")
                if len(line) > _MAX_MESSAGE:
                    return None
                value = json.loads(line.decode("utf-8"))
                return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError):
        return None
    return None


class SingleInstance:
    """Own one desktop per data directory, or focus its existing owner.

    ``acquire()`` returns True for the owner and False only after an existing
    owner acknowledges a focus request. An unresponsive owner raises
    :class:`SingleInstanceError`; it never permits a second desktop to start.

    ``on_show`` runs on the control-server worker thread. The desktop wrapper
    must retain a pending request before its GUI exists, and marshal window
    operations through its GUI toolkit. ``close()`` is idempotent.

    Use a current-user application-data directory. The rendezvous token is a
    random local control secret, not a model/API credential. It is never logged.
    File creation uses owner-only POSIX permissions; Windows inherits the
    application's current-user data-directory ACLs.
    """

    def __init__(
        self, data_dir: Path | str, on_show: Callable[[], None],
        name: str = "IslandWorldDesktop",
    ) -> None:
        if not callable(on_show):
            raise TypeError("on_show must be callable.")
        if not isinstance(name, str) or not name or len(name) > 128:
            raise ValueError("Instance name must contain between 1 and 128 characters.")
        self.data_dir = Path(data_dir).resolve()
        self.name = name
        self._on_show = on_show
        self._state_path = self.data_dir / "instance.json"
        self._guard = threading.RLock()
        self._handle: BinaryIO | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._token: str | None = None

    def acquire(self) -> bool:
        with self._guard:
            if self._handle is not None:
                return True
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            handle = os.fdopen(os.open(self.data_dir / "instance.lock", flags, 0o600), "r+b")
            deadline = time.monotonic() + _STARTUP_TIMEOUT
            try:
                while True:
                    if _try_lock(handle):
                        self._handle = handle
                        try:
                            if os.fstat(handle.fileno()).st_size == 0:
                                handle.write(b"\0")
                                handle.flush()
                            self._start_control_server()
                        except Exception:
                            self.close()
                            raise
                        return True
                    if self._notify_existing():
                        return False
                    if time.monotonic() >= deadline:
                        raise SingleInstanceError(
                            "Another desktop instance owns this data directory but did not "
                            "answer the focus request. Wait for it to finish starting, then retry."
                        )
                    time.sleep(0.025)
            finally:
                if self._handle is not handle:
                    handle.close()

    def _read_state(self) -> dict | None:
        try:
            with self._state_path.open("rb") as state_file:
                raw = state_file.read(_MAX_MESSAGE + 1)
            if len(raw) > _MAX_MESSAGE:
                return None
            state = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(state, dict)
                or state.get("version") != 1
                or state.get("name") != self.name
                or type(state.get("pid")) is not int
                or state["pid"] <= 0
                or type(state.get("port")) is not int
                or not 1 <= state["port"] <= 65535
                or not isinstance(state.get("token"), str)
                or not 32 <= len(state["token"]) <= 128
            ):
                return None
            return state
        except (OSError, UnicodeError, ValueError):
            return None

    def _notify_existing(self) -> bool:
        state = self._read_state()
        if state is None:
            return False
        try:
            # Never accept a host from the rendezvous file.
            with socket.create_connection(("127.0.0.1", state["port"]), _SOCKET_TIMEOUT) as connection:
                connection.settimeout(_SOCKET_TIMEOUT)
                connection.sendall(json.dumps({"action": "show", "token": state["token"]}).encode() + b"\n")
                response = _read_message(connection)
                return response == {"ok": True}
        except OSError:
            return False

    def _start_control_server(self) -> None:
        self._stop.clear()
        self._token = secrets.token_urlsafe(32)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener = listener
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(_SOCKET_TIMEOUT)
        state = {
            "version": 1, "name": self.name, "pid": os.getpid(),
            "port": listener.getsockname()[1], "token": self._token,
        }
        self._publish_state(state)
        self._thread = threading.Thread(
            target=self._serve, args=(listener, self._token),
            name=f"{self.name}-focus", daemon=True,
        )
        self._thread.start()

    def _publish_state(self, state: dict) -> None:
        temporary = self.data_dir / f".instance-{secrets.token_hex(8)}.tmp"
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
                json.dump(state, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._state_path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def _serve(self, listener: socket.socket, token: str) -> None:
        while not self._stop.is_set():
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(_SOCKET_TIMEOUT)
                request = _read_message(connection)
                valid = (
                    address[0] == "127.0.0.1" and request is not None
                    and set(request) == {"action", "token"}
                    and request.get("action") == "show"
                    and isinstance(request.get("token"), str)
                    and request["token"].isascii()
                    and hmac.compare_digest(request["token"].encode("utf-8"), token.encode("utf-8"))
                )
                with contextlib.suppress(OSError):
                    connection.sendall(b'{"ok":true}\n' if valid else b'{"ok":false}\n')
                if valid and not self._stop.is_set():
                    # Acknowledge before GUI dispatch, which can wait for readiness.
                    # User callback failures must not terminate instance ownership.
                    with contextlib.suppress(Exception):
                        self._on_show()

    def close(self) -> None:
        with self._guard:
            handle = self._handle
            if handle is None:
                return
            self._stop.set()
            if self._listener is not None:
                with contextlib.suppress(OSError):
                    self._listener.close()
            state = self._read_state()
            if state is not None and state.get("token") == self._token:
                with contextlib.suppress(OSError):
                    self._state_path.unlink()
            # Never unlink the lock file: another process may already have it open.
            with contextlib.suppress(OSError):
                _unlock(handle)
            handle.close()
            self._handle = None
            self._listener = None
            self._token = None
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_SOCKET_TIMEOUT + 0.1)

    def __enter__(self) -> SingleInstance:
        if not self.acquire():
            raise SingleInstanceError("The existing desktop was focused; this instance is not the owner.")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
