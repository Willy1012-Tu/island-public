"""Workspace MCP registration never mutates global settings or permissions."""
import json
import sys

import pytest

from tools.setup_remote import RemoteSetup, RemoteSetupError, SERVER_NAME, TOOLS


@pytest.fixture
def setup(tmp_path):
    project = tmp_path / "project"
    (project / "tools").mkdir(parents=True)
    (project / "tools" / "island_mcp.py").write_text("# test placeholder\n")
    return RemoteSetup(project, tmp_path / "data", python=sys.executable)


def test_project_registration_is_idempotent_and_preserves_other_servers(setup):
    setup.folder.mkdir()
    other = {"command": "existing-tool", "args": [], "disabled": True}
    setup.config.write_text(json.dumps({"mcpServers": {"other": other}, "custom": "keep"}))
    assert setup.setup()["permissions_changed"] is False
    first = setup.config.read_bytes()
    assert setup.setup()["configured"]
    assert setup.config.read_bytes() == first
    value = json.loads(first)
    assert value["mcpServers"]["other"] == other and value["custom"] == "keep"
    definition = value["mcpServers"][SERVER_NAME]
    assert definition["enabledTools"] == TOOLS and len(TOOLS) == 5
    assert not any("apply" in name for name in TOOLS)
    assert "--project" in definition["args"] and "--data-dir" in definition["args"]
    assert not any("permission" in key.lower() for key in definition)


def test_foreign_same_named_server_is_not_overwritten(setup):
    setup.folder.mkdir()
    original = json.dumps({"mcpServers": {SERVER_NAME: {"command": "some-other-server"}}})
    setup.config.write_text(original)
    with pytest.raises(RemoteSetupError):
        setup.setup()
    assert setup.config.read_text() == original and not setup.marker.exists()


@pytest.mark.parametrize("content", ['// JSONC is intentionally preserved\n{}', '{"mcpServers":{},"mcpServers":{}}', '[]', '{"mcpServers":[]}'])
def test_unsupported_or_malformed_existing_settings_are_preserved(setup, content):
    setup.folder.mkdir()
    setup.config.write_text(content)
    with pytest.raises(RemoteSetupError):
        setup.setup()
    assert setup.config.read_text() == content


def test_status_does_not_provision_a_backend_or_read_focus_token(setup):
    setup.setup()
    state = setup.status()
    assert state["configured"] and state["office_ready"] is False
    assert state["code"] == "office_not_ready"
    assert not setup.data_dir.exists()
