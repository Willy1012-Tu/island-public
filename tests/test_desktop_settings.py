"""Versioned local settings remain usable and preserve unreadable user data."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from desktop_settings import DesktopConfig, SettingsStore, default_data_dir


def test_unicode_settings_roundtrip_is_versioned_atomic_and_readable(tmp_path):
    workspace = tmp_path / "中文 工作專案"
    workspace.mkdir()
    store = SettingsStore(tmp_path / "本機設定")
    config = DesktopConfig(
        workspace=str(workspace), start_minimized=True, close_to_tray=False,
        window_width=1280, window_height=800, port=49155, monitor_timeout=120.5,
    )
    store.save(config)
    assert store.load() == config
    text = store.path.read_text(encoding="utf-8")
    assert "中文 工作專案" in text
    assert json.loads(text) == {"version": 1, "settings": asdict(config)}
    assert list(store.data_dir.iterdir()) == [store.path]


def test_missing_settings_use_defaults_without_creating_files(tmp_path):
    store = SettingsStore(tmp_path / "not-created")
    assert store.load() == DesktopConfig()
    assert not store.data_dir.exists()


@pytest.mark.parametrize("changes", [
    {"workspace": ""}, {"workspace": None}, {"workspace": 42},
    {"start_minimized": 1}, {"start_minimized": "false"}, {"close_to_tray": None},
    {"window_width": True}, {"window_width": 779}, {"window_width": 7681},
    {"window_height": 559}, {"window_height": 4321}, {"window_height": 800.0},
    {"port": True}, {"port": -1}, {"port": 65536}, {"port": "8000"},
    {"monitor_timeout": True}, {"monitor_timeout": 0}, {"monitor_timeout": -1},
    {"monitor_timeout": 86401}, {"monitor_timeout": float("nan")},
    {"monitor_timeout": float("inf")}, {"monitor_timeout": "300"},
])
def test_invalid_settings_types_and_ranges_are_rejected_and_preserved(tmp_path, changes):
    store = SettingsStore(tmp_path)
    document = {"version": 1, "settings": {"workspace": str(tmp_path), **changes}}
    original = json.dumps(document).encode("utf-8")
    store.path.write_bytes(original)
    assert store.load() == DesktopConfig()
    assert store.path.read_bytes() == original
    with pytest.raises(ValueError):
        DesktopConfig.from_dict(document["settings"])


@pytest.mark.parametrize("document", [
    [], None, "settings", {"version": 2, "settings": {}},
    {"version": "1", "settings": {}}, {"version": True, "settings": {"start_minimized": True}},
    {"settings": {}}, {"version": 1, "settings": []},
])
def test_unknown_version_and_wrong_document_shape_preserve_original_file(tmp_path, document):
    store = SettingsStore(tmp_path)
    original = json.dumps(document).encode("utf-8")
    store.path.write_bytes(original)
    assert store.load() == DesktopConfig()
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("original", [b"{broken", b"\xff\xfe", b" " * (64 * 1024 + 1)],
                         ids=["broken-json", "invalid-utf8", "oversized"])
def test_corrupt_invalid_utf8_or_oversized_settings_are_preserved(tmp_path, original):
    store = SettingsStore(tmp_path)
    store.path.write_bytes(original)
    assert store.load() == DesktopConfig()
    assert store.path.read_bytes() == original


def test_failed_atomic_replace_leaves_previous_settings_intact(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path)
    store.save(DesktopConfig())
    original = store.path.read_bytes()
    original_replace = Path.replace

    def unavailable_replace(path, target):
        if Path(target) == store.path:
            raise OSError("Disk unavailable")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", unavailable_replace)
    with pytest.raises(OSError, match="Disk unavailable"):
        store.save(DesktopConfig(start_minimized=True))
    assert store.path.read_bytes() == original
    assert store.load() == DesktopConfig()


def test_invalid_save_does_not_overwrite_valid_settings(tmp_path):
    store = SettingsStore(tmp_path)
    store.save(DesktopConfig())
    original = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save(DesktopConfig(port=True))
    assert store.path.read_bytes() == original


def test_default_app_data_uses_current_user_environment_without_creating_it(tmp_path, monkeypatch):
    isolated_base = tmp_path / "AppData 本機"
    monkeypatch.setenv("LOCALAPPDATA", str(isolated_base))
    monkeypatch.setenv("XDG_DATA_HOME", str(isolated_base))
    assert default_data_dir() == isolated_base / "IslandWorldDesktop"
    assert not isolated_base.exists()


def test_workspace_must_be_existing_directory_not_file(tmp_path):
    regular = tmp_path / "project.txt"
    regular.write_text("text", encoding="utf-8")
    for path in (regular, tmp_path / "missing"):
        with pytest.raises(ValueError, match="existing directory"):
            DesktopConfig.from_dict({"workspace": str(path)})


def test_boundary_values_are_valid_and_timeout_is_normalized(tmp_path):
    low = DesktopConfig.from_dict({
        "workspace": str(tmp_path), "window_width": 780,
        "window_height": 560, "port": 0, "monitor_timeout": 1,
    })
    high = DesktopConfig.from_dict({
        "workspace": str(tmp_path), "window_width": 7680,
        "window_height": 4320, "port": 65535, "monitor_timeout": 86400,
    })
    assert low.monitor_timeout == 1.0 and type(low.monitor_timeout) is float
    assert high.monitor_timeout == 86400.0
