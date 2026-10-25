"""Catalog discovery is metadata-only and never launches model inference."""

from __future__ import annotations

import copy
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import codex_catalog as catalog


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch, tmp_path):
    home = tmp_path / "separate-codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(catalog, "_CACHE", {})
    monkeypatch.setattr(catalog.subprocess, "run", lambda *args, **kwargs: pytest.fail("Unexpected native command"))
    return home


def model(identifier="gpt-visible", *, efforts=("low", "medium", "high"), default="medium", **extra):
    return {
        "slug": identifier, "display_name": "Friendly " + identifier,
        "visibility": "list", "supported_in_api": True,
        "default_reasoning_level": default,
        "supported_reasoning_levels": [{"effort": effort, "description": "public effort description"} for effort in efforts],
        **extra,
    }


def executable(tmp_path):
    path = tmp_path / "codex.exe"
    path.write_bytes(b"not executed")
    return str(path)


def test_picker_visibility_and_public_schema_never_export_identity_or_model_instructions():
    payload = {
        "identity": {"account_id": "PRIVATE_ACCOUNT_MARKER"},
        "models": [
            model("gpt-hidden", visibility="hide"),
            model("gpt-visible", model_messages={"instructions": "PRIVATE_INSTRUCTIONS_MARKER"},
                  available_access_programs=["PRIVATE_PROGRAM_MARKER"]),
            model("gpt-not-listed", visibility=None),
        ],
    }
    options = catalog.parse_models(payload)
    assert options == [{"id": "gpt-visible", "name": "Friendly gpt-visible",
                        "reasoning_efforts": ["low", "medium", "high"], "default_reasoning_effort": "medium"}]
    assert set(options[0]) == {"id", "name", "reasoning_efforts", "default_reasoning_effort"}
    output = json.dumps(options)
    assert "PRIVATE_" not in output
    # API availability is not the UI visibility gate: hidden review models also advertise it.
    assert payload["models"][0]["supported_in_api"] is True


def test_efforts_are_model_specific_with_valid_defaults_and_no_invented_ultra():
    options = catalog.parse_models({"models": [
        model("gpt-sol", efforts=("low", "medium", "high", "xhigh", "max", "ultra"), default="low"),
        model("gpt-luna", efforts=("low", "medium", "high", "xhigh", "max"), default="medium"),
        model("gpt-bad-default", efforts=("high", "low"), default="ultra"),
    ]})
    assert options[0]["default_reasoning_effort"] == "low"
    assert options[1]["default_reasoning_effort"] == "medium"
    assert "ultra" in options[0]["reasoning_efforts"]
    assert "ultra" not in options[1]["reasoning_efforts"]
    assert options[2]["default_reasoning_effort"] == "high", "invalid defaults fall back only to an advertised effort"


def test_catalog_retains_explicit_default_without_treating_truthy_values_as_flags():
    options = catalog.parse_models({"models": [model("first", is_default="true"), model("default", is_default=True)]})
    assert "is_default" not in options[0]
    assert options[1]["is_default"] is True


def test_malformed_unsupported_and_duplicate_levels_do_not_crash_or_become_options():
    options = catalog.parse_models({"models": [
        model("gpt-mixed", supported_reasoning_levels=[
            {"effort": "high"}, {"effort": "high"}, {"effort": "turbo"},
            {"effort": ["low"]}, {"effort": None}, "medium", 7, {}, {"effort": "low"},
        ], default_reasoning_level={"unexpected": True}),
        model("gpt-no-efforts", supported_reasoning_levels=None),
        model("gpt-future-level", efforts=("unknown-new-level",), default="unknown-new-level"),
    ]})
    assert options[0]["reasoning_efforts"] == ["high", "low"]
    assert options[0]["default_reasoning_effort"] == "high"
    for option in options[1:]:
        assert option["reasoning_efforts"] == []
        assert option["default_reasoning_effort"] is None


@pytest.mark.parametrize("payload", [None, [], {"data": []}, {"models": None}, {"models": "not an array"}])
def test_wrong_catalog_shape_is_empty(payload):
    assert catalog.parse_models(payload) == []


def test_invalid_ids_duplicates_and_unbounded_names_are_sanitized():
    raw = model("gpt-valid", display_name="Readable\nName\t" + "x" * 300)
    options = catalog.parse_models({"models": [
        None, 7, model("bad name"), model("../bad"), model("bad;command"),
        model("x" * 241), raw, model("gpt-valid", display_name="duplicate must not replace first"),
        model("gpt-empty-name", display_name="\n\t"),
    ]})
    assert [option["id"] for option in options] == ["gpt-valid", "gpt-empty-name"]
    assert options[0]["name"].startswith("ReadableName")
    assert len(options[0]["name"]) <= 240
    assert all(character.isprintable() for character in options[0]["name"])
    assert options[1]["name"] == "gpt-empty-name"


def test_catalog_and_nested_effort_entry_count_are_bounded():
    payload = {"models": [model("first", supported_reasoning_levels=[{}] * 16 + [{"effort": "ultra"}])]
               + [None] * 199 + [model("outside-bound")]}
    options = catalog.parse_models(payload)
    assert [option["id"] for option in options] == ["first"]
    assert options[0]["reasoning_efforts"] == []


def test_json_reads_are_bounded_and_invalid_or_non_utf8_cache_is_ignored(monkeypatch):
    monkeypatch.setattr(catalog, "_MAX_BYTES", 64)

    class Source:
        def __init__(self): self.reads = []
        def read(self, count): self.reads.append(count); return b"x" * count

    source = Source()
    assert catalog._load_json(source) == []
    assert source.reads == [65]
    assert catalog._load_json(io.BytesIO(b"{broken")) == []
    assert catalog._load_json(io.BytesIO(b"\xff\xfe\xff")) == []
    assert catalog._load_json(io.BytesIO(b'{"identity":"private","models":[]}')) == []


def test_excessively_nested_cache_falls_back_instead_of_breaking_startup():
    assert catalog._load_json(io.BytesIO(b"[" * 5000 + b"0" + b"]" * 5000)) == []


@pytest.mark.parametrize("configuration,expected", [
    (b'model_reasoning_effort = "ultra"\n', "ultra"),
    (b'model_reasoning_effort = "low"\nmodel = "other"\n', "low"),
    (b'model_reasoning_effort = "turbo"\n', None),
    (b'model_reasoning_effort = ["high"]\n', None),
    (b'model_reasoning_effort = 123\n', None),
    (b'model_reasoning_effort = { value = "high" }\n', None),
    (b'model_reasoning_effort = "unfinished\n', None),
    (b'\xffnot-utf8', None),
    (b'model = "saved"\n', None),
])
def test_saved_effort_reads_only_supported_top_level_preference(isolated_catalog, configuration, expected):
    (isolated_catalog / "config.toml").write_bytes(configuration)
    assert catalog.saved_reasoning_effort() == expected


def test_saved_effort_missing_and_oversized_config_is_ignored(monkeypatch, isolated_catalog):
    assert catalog.saved_reasoning_effort() is None
    monkeypatch.setattr(catalog, "_MAX_BYTES", 64)
    (isolated_catalog / "config.toml").write_bytes(b'model_reasoning_effort = "high"\n#' + b"x" * 80)
    assert catalog.saved_reasoning_effort() is None


def test_fresh_native_discovery_is_only_a_bounded_metadata_command(monkeypatch, tmp_path, isolated_catalog):
    exe = executable(tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert Path(kwargs["cwd"]).is_dir()
        assert Path(kwargs["cwd"]).resolve() != Path.cwd().resolve(), "project instructions must not be the discovery working directory"
        kwargs["stdout"].write(json.dumps({"models": [model("fresh")], "identity": "must never escape"}).encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(catalog.subprocess, "run", run)
    (isolated_catalog / "models_cache.json").write_text(json.dumps({"models": [model("stale")]}))
    options = catalog.discover_models(exe, "fresh", "high")
    assert [option["id"] for option in options] == ["fresh"]
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [exe, "--no-daemon", "debug", "models"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["shell"] is False and kwargs["timeout"] == 5
    assert "exec" not in argv and "thread/start" not in argv and "turn/start" not in argv


@pytest.mark.parametrize("failure", ["timeout", "missing", "exit", "json", "oversized"])
def test_native_failure_falls_back_to_bounded_codex_home_cache(monkeypatch, tmp_path, isolated_catalog, failure):
    exe = executable(tmp_path)
    monkeypatch.setattr(catalog, "_MAX_BYTES", 1024)
    (isolated_catalog / "models_cache.json").write_text(json.dumps({"models": [model("from-cache", default="low")]}), encoding="utf-8")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "missing":
            raise FileNotFoundError("fixture command unavailable")
        if failure == "json": kwargs["stdout"].write(b"{not json")
        if failure == "oversized": kwargs["stdout"].write(b"x" * 1025)
        return SimpleNamespace(returncode=1 if failure == "exit" else 0)

    monkeypatch.setattr(catalog.subprocess, "run", run)
    options = catalog.discover_models(exe, "from-cache", "ultra")
    assert [option["id"] for option in options] == ["from-cache"]
    assert options[0]["default_reasoning_effort"] == "low"
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [b"{broken", b"\xffbad", b"x" * 1025])
def test_invalid_offline_cache_keeps_only_saved_model(monkeypatch, tmp_path, isolated_catalog, raw):
    exe = executable(tmp_path)
    monkeypatch.setattr(catalog, "_MAX_BYTES", 1024)
    (isolated_catalog / "models_cache.json").write_bytes(raw)
    monkeypatch.setattr(catalog.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    assert catalog.discover_models(exe, "saved-only", "high") == [{
        "id": "saved-only", "name": "saved-only", "reasoning_efforts": ["high"], "default_reasoning_effort": "high",
    }]


def test_nonexistent_executable_does_not_spawn_and_preserves_saved_settings(tmp_path):
    missing = str(tmp_path / "does-not-exist.exe")
    assert catalog.discover_models(missing, "saved-model", "low") == [{
        "id": "saved-model", "name": "saved-model", "reasoning_efforts": ["low"], "default_reasoning_effort": "low",
    }]
    assert catalog.discover_models(None, None, None) == []
    assert catalog.discover_models(missing, "invalid;name", "low") == []


@pytest.mark.parametrize("bad_effort", [None, "unsupported", 12, ["high"], {"effort": "high"}])
def test_fallback_ignores_malformed_or_unknown_configured_effort(tmp_path, bad_effort):
    assert catalog.discover_models(str(tmp_path / "missing.exe"), "saved", bad_effort) == [{
        "id": "saved", "name": "saved", "reasoning_efforts": [], "default_reasoning_effort": None,
    }]


def test_ttl_cache_is_keyed_by_executable_and_codex_home_and_returns_independent_data(monkeypatch, tmp_path, isolated_catalog):
    exe = executable(tmp_path)
    now = [100.0]
    reads = []
    options = catalog.parse_models({"models": [model("advertised")]})
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now[0])

    def read(command):
        reads.append(command)
        return copy.deepcopy(options)

    monkeypatch.setattr(catalog, "_read_catalog", read)
    first = catalog.discover_models(exe, "advertised", "high")
    first[0]["reasoning_efforts"].append("caller-mutation")
    second = catalog.discover_models(exe, "advertised", "low")
    assert second == options and len(reads) == 1
    second[0]["name"] = "another mutation"
    now[0] = 159.99
    assert catalog.discover_models(exe, None, None) == options and len(reads) == 1
    now[0] = 160
    assert catalog.discover_models(exe, None, None) == options and len(reads) == 2
    alternate = tmp_path / "another-home"
    alternate.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(alternate))
    catalog.discover_models(exe, None, None)
    assert len(reads) == 3
    other_exe = tmp_path / "other-codex.exe"
    other_exe.write_bytes(b"not executed")
    catalog.discover_models(str(other_exe), None, None)
    assert len(reads) == 4


def test_failed_discovery_is_cached_but_saved_fallback_does_not_leak_between_calls(monkeypatch, tmp_path):
    exe = executable(tmp_path)
    reads = []
    monkeypatch.setattr(catalog, "_read_catalog", lambda command: reads.append(command) or [])
    first = catalog.discover_models(exe, "first-saved", "low")
    second = catalog.discover_models(exe, "second-saved", "high")
    assert first[0]["id"] == "first-saved" and first[0]["default_reasoning_effort"] == "low"
    assert second[0]["id"] == "second-saved" and second[0]["default_reasoning_effort"] == "high"
    assert len(reads) == 1


def test_preserved_saved_model_is_distinguished_from_catalog_and_not_cached_as_an_advertised_choice(monkeypatch, tmp_path):
    exe = executable(tmp_path)
    advertised = catalog.parse_models({"models": [model("advertised")]})
    reads = []
    monkeypatch.setattr(catalog, "_read_catalog", lambda command: reads.append(command) or copy.deepcopy(advertised))
    first = catalog.discover_models(exe, "saved-first", "low")
    second = catalog.discover_models(exe, "saved-second", "high")
    assert [option["id"] for option in first] == ["saved-first", "advertised"]
    assert [option["id"] for option in second] == ["saved-second", "advertised"]
    assert first[0]["name"].startswith("saved-first") and first[0]["name"] != first[0]["id"]
    assert first[0]["reasoning_efforts"] == ["low"]
    assert second[0]["reasoning_efforts"] == ["high"]
    assert first[1] == second[1] == advertised[0]
    assert len(reads) == 1
