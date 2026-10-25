"""Real local single-instance handoffs without a GUI or model request."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import desktop_instance
from desktop_instance import SingleInstance, SingleInstanceError


def control(data_dir: Path, message: bytes) -> dict | None:
    state = json.loads((data_dir / "instance.json").read_text(encoding="utf-8"))
    with socket.create_connection(("127.0.0.1", state["port"]), timeout=1) as client:
        client.settimeout(1)
        client.sendall(message)
        try:
            response = client.recv(1024)
        except ConnectionResetError:
            return None
    return json.loads(response) if response else None


def test_second_launch_focuses_first_on_worker_and_does_not_remove_its_state(tmp_path):
    calls = []
    shown = threading.Event()

    def show():
        calls.append(threading.get_ident())
        shown.set()

    owner = SingleInstance(tmp_path / "辦公室 data", show)
    second = SingleInstance(owner.data_dir, lambda: pytest.fail("Second instance must not own UI"))
    try:
        assert owner.acquire() is True
        assert owner.acquire() is True
        state = json.loads((owner.data_dir / "instance.json").read_text(encoding="utf-8"))
        assert state["pid"] == os.getpid() and 0 < state["port"] < 65536
        assert len(state["token"]) >= 32
        assert second.acquire() is False
        assert shown.wait(1) and len(calls) == 1
        assert calls[0] != threading.get_ident()
        second.close()
        assert (owner.data_dir / "instance.json").exists()
    finally:
        second.close()
        owner.close()
    assert not (owner.data_dir / "instance.json").exists()
    owner.close()
    assert owner.acquire() is True
    owner.close()


def test_free_os_lock_overrides_stale_rendezvous_without_contacting_foreign_port(tmp_path):
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen()
        unrelated.settimeout(0.05)
        stale = {
            "version": 1, "name": "IslandWorldDesktop", "pid": 123,
            "port": unrelated.getsockname()[1], "token": "x" * 43,
        }
        (tmp_path / "instance.json").write_text(json.dumps(stale), encoding="utf-8")
        owner = SingleInstance(tmp_path, lambda: None)
        try:
            assert owner.acquire() is True
            current = json.loads((tmp_path / "instance.json").read_text(encoding="utf-8"))
            assert current["token"] != stale["token"]
            with pytest.raises(socket.timeout):
                unrelated.accept()
        finally:
            owner.close()


@pytest.mark.parametrize("payload", [
    b'{"action":"show","token":"wrong"}\n',
    b'{"action":"quit","token":"TOKEN"}\n',
    b'{"action":"show","token":"TOKEN","extra":true}\n',
    b'{"action":"show","token":null}\n',
    b'{"action":"show","token":"\\ud800"}\n',
    b'[]\n', b'not JSON\n', b'\xff\n', b'x' * 2050 + b'\n',
])
def test_unauthorized_or_malformed_control_never_invokes_ui_and_keeps_server_alive(tmp_path, payload):
    shown = threading.Event()
    owner = SingleInstance(tmp_path, shown.set)
    second = SingleInstance(tmp_path, lambda: None)
    try:
        assert owner.acquire()
        token = json.loads((tmp_path / "instance.json").read_text(encoding="utf-8"))["token"]
        response = control(tmp_path, payload.replace(b"TOKEN", token.encode()))
        assert response in ({"ok": False}, None)
        assert not shown.is_set()
        assert second.acquire() is False
        assert shown.wait(1)
    finally:
        second.close()
        owner.close()


def test_different_data_directories_have_independent_owners_and_ephemeral_ports(tmp_path):
    shown = threading.Event()
    first = SingleInstance(tmp_path / "one", lambda: None)
    other = SingleInstance(tmp_path / "two", shown.set)
    try:
        assert first.acquire() and other.acquire()
        states = [json.loads((item.data_dir / "instance.json").read_text(encoding="utf-8")) for item in (first, other)]
        assert states[0]["port"] != states[1]["port"]
        first.close()
        assert SingleInstance(other.data_dir, lambda: None).acquire() is False
        assert shown.wait(1)
    finally:
        first.close()
        other.close()


def test_crashed_process_releases_os_lock_and_next_launch_replaces_stale_state(tmp_path):
    script = (
        "import sys; from desktop_instance import SingleInstance; "
        "instance=SingleInstance(sys.argv[1],lambda:print('FOCUSED',flush=True)); "
        "assert instance.acquire(); print('READY',flush=True); sys.stdin.read()"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(tmp_path / "獨立 process")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", shell=False, cwd=Path(__file__).resolve().parents[1],
    )
    replacement = SingleInstance(tmp_path / "獨立 process", lambda: None)
    try:
        assert process.stdout.readline().strip() == "READY"
        state_file = replacement.data_dir / "instance.json"
        stale_token = json.loads(state_file.read_text(encoding="utf-8"))["token"]
        assert SingleInstance(replacement.data_dir, lambda: None).acquire() is False
        assert process.stdout.readline().strip() == "FOCUSED"
        process.terminate()  # Only this test's own child, simulating a crash.
        process.wait(timeout=5)
        assert state_file.exists()
        assert replacement.acquire() is True
        assert json.loads(state_file.read_text(encoding="utf-8"))["token"] != stale_token
    finally:
        replacement.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_startup_race_waits_for_owner_to_publish_focus_address(tmp_path, monkeypatch):
    entered, release, shown = threading.Event(), threading.Event(), threading.Event()
    owner = SingleInstance(tmp_path, shown.set)
    original_publish = owner._publish_state

    def delayed_publish(state):
        entered.set()
        assert release.wait(2)
        original_publish(state)

    monkeypatch.setattr(owner, "_publish_state", delayed_publish)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(owner.acquire()))
    thread.start()
    try:
        assert entered.wait(1)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        second = SingleInstance(tmp_path, lambda: None)
        assert second.acquire() is False
        assert shown.wait(1)
        thread.join(2)
        assert outcomes == [True]
    finally:
        release.set()
        thread.join(2)
        owner.close()


def test_live_owner_with_corrupt_record_fails_bounded_without_starting_duplicate(tmp_path, monkeypatch):
    owner = SingleInstance(tmp_path, lambda: None)
    second = SingleInstance(tmp_path, lambda: None)
    monkeypatch.setattr(desktop_instance, "_STARTUP_TIMEOUT", 0.08)
    try:
        assert owner.acquire()
        (tmp_path / "instance.json").write_text("not a rendezvous", encoding="utf-8")
        with pytest.raises(SingleInstanceError, match="Another desktop instance"):
            second.acquire()
        second.close()
        owner.close()
        assert second.acquire() is True
    finally:
        second.close()
        owner.close()


def test_callback_failure_does_not_break_instance_ownership(tmp_path):
    calls = []
    twice = threading.Event()

    def failing_show():
        calls.append(True)
        if len(calls) == 2:
            twice.set()
        raise RuntimeError("UI is still starting")

    owner = SingleInstance(tmp_path, failing_show)
    try:
        assert owner.acquire()
        assert SingleInstance(tmp_path, lambda: None).acquire() is False
        assert SingleInstance(tmp_path, lambda: None).acquire() is False
        assert twice.wait(1)
    finally:
        owner.close()


def test_close_from_focus_worker_does_not_join_itself(tmp_path):
    closed = threading.Event()

    def close_self():
        owner.close()
        closed.set()

    owner = SingleInstance(tmp_path, close_self)
    try:
        assert owner.acquire()
        assert SingleInstance(tmp_path, lambda: None).acquire() is False
        assert closed.wait(1)
        assert not (tmp_path / "instance.json").exists()
    finally:
        owner.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows uses current-user directory ACL inheritance")
def test_new_posix_rendezvous_files_are_owner_only(tmp_path):
    owner = SingleInstance(tmp_path / "private", lambda: None)
    try:
        assert owner.acquire()
        for path in (owner.data_dir / "instance.json", owner.data_dir / "instance.lock"):
            assert path.stat().st_mode & 0o077 == 0
        assert owner.data_dir.stat().st_mode & 0o077 == 0
    finally:
        owner.close()
