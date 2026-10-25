"""Check the public Python/browser boundary without a browser or model request."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from agent_brain import Action, Decision, MiniGame
from island_world import Agent, IslandWorld, Rock, Tree
from web_server import WebRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ScriptInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._current = {"attributes": dict(attrs), "content": ""}
            self.scripts.append(self._current)

    def handle_data(self, data):
        if self._current is not None:
            self._current["content"] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self._current = None


def frontend_scripts(page="index.html"):
    document = ScriptInventory()
    document.feed((PROJECT_ROOT / "web" / page).read_text(encoding="utf-8"))
    return document.scripts


class NoRequestBrain:
    def think(self, observation):
        raise AssertionError("Contract tests must not dispatch a model request")

    def close(self):
        pass


@pytest.fixture
def runtime():
    world = IslandWorld(tree_count=0, rock_count=0, seed=7)
    world.agent = Agent(world, 2, 2)
    world.grid[2][5] = Tree(5, 2)
    world.grid[5][2] = Rock(2, 5)
    bridge = WebRuntime(world=world, brain=NoRequestBrain(), brain_name="demo", seed=7)
    try:
        yield bridge
    finally:
        bridge.brain_loop.close()


def test_public_state_contract_is_complete_and_json_serializable(runtime):
    state = runtime.snapshot()
    required = {
        "type", "version", "revision", "tick", "tick_seconds", "paused", "brain",
        "thinking", "world", "agent", "path", "internal_thought", "status",
        "current_action", "mini_game", "counters", "events",
    }
    assert required <= state.keys()
    assert state["type"] == "state" and state["version"] == 1
    assert state["tick_seconds"] == 0.5
    assert state["brain"] == "demo" and state["thinking"] is False
    assert state["paused"] is False
    assert isinstance(state["revision"], int) and isinstance(state["tick"], int)
    assert state["world"]["size"] == 20 and state["world"]["seed"] == 7
    assert len(state["world"]["tiles"]) == 20
    assert all(len(row) == 20 for row in state["world"]["tiles"])
    assert all({"terrain", "entity"} <= cell.keys()
               for row in state["world"]["tiles"] for cell in row)
    # Deliberately asymmetric positions detect accidental transposition of x/y.
    assert state["world"]["tiles"][2][5]["entity"] == "tree"
    assert state["world"]["tiles"][5][2]["entity"] == "rock"
    assert {"x", "y", "energy", "boredom", "inventory"} <= state["agent"].keys()
    assert state["agent"]["x"] == 2 and state["agent"]["y"] == 2
    assert state["current_action"] is None and state["mini_game"] is None
    assert state["path"] == [] and state["events"] == []
    assert {"plans", "games", "decisions"} <= state["counters"].keys()
    assert json.loads(json.dumps(state, ensure_ascii=False, allow_nan=False)) == state


def test_plan_game_path_and_resources_are_stable_public_snapshots(runtime):
    simulation = runtime.simulation
    simulation.accept_decision(Decision(
        "我發明森林尋寶，走近樹木再採一份寶藏！",
        (Action("gather", (5, 2)),),
        MiniGame("森林尋寶", "走到指定樹木旁，採集一份木材。"),
    ))
    simulation.advance_tick()
    walking = runtime.snapshot()
    assert walking["current_action"] == {"name": "gather", "target": {"x": 5, "y": 2}}
    assert walking["mini_game"] == {"name": "森林尋寶", "rules": "走到指定樹木旁，採集一份木材。"}
    assert walking["internal_thought"] == "我發明森林尋寶，走近樹木再採一份寶藏！"
    assert walking["agent"]["x"] == 3 and walking["agent"]["y"] == 2
    assert walking["path"] == [{"x": 4, "y": 2}]
    assert walking["world"]["tiles"][2][5]["entity"] == "tree"
    simulation.advance_tick()
    simulation.advance_tick()
    finished = runtime.snapshot()
    assert finished["agent"]["inventory"] == {"wood": 1}
    assert finished["world"]["tiles"][2][5]["entity"] is None
    assert finished["current_action"] is None and finished["mini_game"] is None
    assert finished["counters"]["plans"] == finished["counters"]["games"] == 1
    # A pending WebSocket frame must not change under a slow client's feet.
    assert walking["agent"]["inventory"] == {}
    assert walking["agent"]["x"] == 3
    assert walking["path"] == [{"x": 4, "y": 2}]
    assert walking["world"]["tiles"][2][5]["entity"] == "tree"
    assert "森林尋寶" in json.dumps(walking, ensure_ascii=False)


def test_web_import_and_existing_headless_execution_do_not_import_pygame():
    source = """
import json
import sys
import web_server
from main import parse_args, run
result = run(parse_args([
    '--brain', 'demo', '--headless', '--ticks', '2',
    '--tick-seconds', '0.001', '--think-delay', '0',
]))
assert result == 0, result
assert 'pygame' not in sys.modules
assert 'renderer' not in sys.modules
print(json.dumps({'exit_code': result, 'pygame_imported': False}))
"""
    process = subprocess.run(
        [sys.executable, "-c", source], cwd=PROJECT_ROOT, capture_output=True,
        encoding="utf-8", errors="replace", timeout=10, check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads(process.stdout.strip().splitlines()[-1]) == {
        "exit_code": 0, "pygame_imported": False,
    }


@pytest.mark.parametrize("page", ["index.html", "monitor.html"])
def test_three_and_addons_use_one_pinned_cdn_version_before_module_execution(page):
    scripts = frontend_scripts(page)
    maps = [script for script in scripts if script["attributes"].get("type") == "importmap"]
    modules = [script for script in scripts if script["attributes"].get("type") == "module"]
    assert len(maps) == 1 and modules
    assert scripts.index(maps[0]) < min(scripts.index(script) for script in modules)
    imports = json.loads(maps[0]["content"])["imports"]
    core = urlsplit(imports["three"])
    addons = urlsplit(imports["three/addons/"])
    assert core.scheme == addons.scheme == "https"
    assert core.netloc == addons.netloc
    assert "/build/" in core.path and addons.path.endswith("/examples/jsm/")
    core_package = core.path.split("/build/", 1)[0]
    addons_package = addons.path.split("/examples/", 1)[0]
    assert core_package == addons_package
    assert re.search(r"/three@v?\d+\.\d+\.\d+$", core_package), "Use a pinned Three.js release"


@pytest.mark.parametrize("page", ["index.html", "monitor.html"])
def test_browser_module_entry_and_local_import_graph_resolve_without_a_bundler(page):
    scripts = frontend_scripts(page)
    imports = json.loads(next(script["content"] for script in scripts
                              if script["attributes"].get("type") == "importmap"))["imports"]
    entries = [script["attributes"]["src"] for script in scripts
               if script["attributes"].get("type") == "module" and "src" in script["attributes"]]
    assert entries
    web_root = PROJECT_ROOT / "web"
    pending = []
    for entry in entries:
        assert entry.startswith("/static/"), "The local Python static route must serve browser entry modules"
        pending.append(web_root / entry.removeprefix("/static/"))
    visited = set()
    external_imports = set()
    # Verify the actual cross-file references, including dynamic scene loading.
    pattern = re.compile(r"(?:\bfrom\s*|\bimport\s*\(\s*)(['\"])([^'\"]+)\1")
    while pending:
        module = pending.pop().resolve()
        if module in visited:
            continue
        visited.add(module)
        assert module.is_relative_to(web_root.resolve())
        assert module.is_file(), f"Missing browser module: {module}"
        for _, specifier in pattern.findall(module.read_text(encoding="utf-8")):
            if specifier.startswith("."):
                pending.append(module.parent / specifier)
            else:
                external_imports.add(specifier)
                assert specifier in imports or any(
                    key.endswith("/") and specifier.startswith(key) for key in imports
                ), f"Bare browser import has no import-map mapping: {specifier}"
    assert {"three", "three/addons/controls/OrbitControls.js"} <= external_imports
