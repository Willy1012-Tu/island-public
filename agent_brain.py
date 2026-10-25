"""Translate island observations to constrained decisions using a logged-in Codex CLI.

No API key is read by this module. Codex uses its own existing account login.
``internal_thought`` is a short fictional character thought, not model reasoning.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from island_world import Coordinate, IslandWorld, Rock, Tree
from pathfinding import find_interaction_path, find_path

ACTION_NAMES = ("move", "gather", "chop_tree", "mine_rock", "rest")
TARGET_SCHEMA = {
    "anyOf": [
        {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
         "required": ["x", "y"], "additionalProperties": False},
        {"type": "null"},
    ]
}
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "internal_thought": {"type": "string", "minLength": 1, "maxLength": 240},
        "mini_game": {"anyOf": [
            {"type": "object", "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 60},
                "rules": {"type": "string", "minLength": 1, "maxLength": 240}},
             "required": ["name", "rules"], "additionalProperties": False},
            {"type": "null"}]},
        "actions": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object", "properties": {
                "name": {"type": "string", "enum": list(ACTION_NAMES)},
                "target": TARGET_SCHEMA},
            "required": ["name", "target"], "additionalProperties": False}},
    },
    "required": ["internal_thought", "mini_game", "actions"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the brain of a fictional character on a small island.
This is a game decision task, not a programming task. Do not inspect files, run
commands, use tools, edit code, or search the web. All game state is below.
Return ONLY a JSON object matching the supplied schema. Use Traditional Chinese
for a brief imaginative in-character thought and any game name/rules. The field
internal_thought is a short visible character diary line, not private reasoning.

Choose 1 to 8 actions from move, gather, chop_tree, mine_rock, rest.
For move, target is a destination, NOT necessarily an adjacent tile: the game
will move one tile per tick using A*. For gathering, target is the RESOURCE tile:
the game walks to an adjacent empty tile then harvests. Trees provide wood and
rocks provide stone. For rest target MUST be null. All other actions MUST have
an integer {x,y} target from valid_targets for that action. Never harvest the same
resource twice in one plan. Rest if energy is low; automatic recovery also
protects an existing plan. Only the listed world actions exist; do not invent
new functions or fictional inventory objects. Make a small useful plan rather
than filling the entire action limit. Previous action failure, if present,
should inform your next choice.

When boredom is GREATER THAN 70, you MUST invent a named mini-game in
internal_thought, set mini_game to an object with its name and concrete rules,
and include at least one physical move or harvest action that enacts those
rules. Invent the game using only the visible terrain/resources and available
actions. For example, pretend two walking destinations are checkpoints in a
treasure race, or collect one stone as a moon explorer. The actual actions must
implement your stated small game; don't merely announce a game and rest.
Completing every action of a mini-game reduces boredom by 45.
At boredom <=70, mini_game may be null. Explore, collect resources, or rest.
"""


@dataclass(frozen=True)
class Action:
    name: str
    target: Coordinate | None = None


@dataclass(frozen=True)
class MiniGame:
    name: str
    rules: str


@dataclass(frozen=True)
class Decision:
    internal_thought: str
    actions: tuple[Action, ...]
    mini_game: MiniGame | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "internal_thought": self.internal_thought,
            "mini_game": asdict(self.mini_game) if self.mini_game else None,
            "actions": [{"name": a.name, "target":
                         {"x": a.target[0], "y": a.target[1]} if a.target is not None else None}
                        for a in self.actions],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False)


class BrainError(RuntimeError):
    """A recoverable transport or decision validation failure."""


def build_observation(world: IslandWorld, *, last_error: str | None = None,
                      previous_thought: str = "", radius: int = 3) -> dict[str, Any]:
    """Copy current state on the game thread before dispatching background work."""
    agent = world.agent
    start = (agent.x, agent.y)
    valid: dict[str, list[dict[str, int]]] = {name: [] for name in ACTION_NAMES if name != "rest"}
    entities = []
    for entity in agent.get_surroundings(radius):
        entities.append({"name": entity.name, "x": entity.x, "y": entity.y})
        if isinstance(entity, (Tree, Rock)) and find_interaction_path(world, start, (entity.x, entity.y)) is not None:
            target = {"x": entity.x, "y": entity.y}
            valid["gather"].append(target)
            valid["chop_tree" if isinstance(entity, Tree) else "mine_rock"].append(target)
    for y in range(max(0, agent.y - radius), min(world.SIZE, agent.y + radius + 1)):
        for x in range(max(0, agent.x - radius), min(world.SIZE, agent.x + radius + 1)):
            if (x, y) == start or (x-agent.x)**2 + (y-agent.y)**2 > radius**2:
                continue
            if world.get_entity_at((x, y)) is None and find_path(world, start, (x, y)) is not None:
                valid["move"].append({"x": x, "y": y})
    descriptions = [f"{e['name']} at ({e['x']},{e['y']})" for e in entities]
    return {
        "position": {"x": agent.x, "y": agent.y}, "map_size": world.SIZE,
        "boredom": agent.boredom, "energy": agent.energy,
        "inventory": dict(agent.inventory),
        "surroundings_text": "; ".join(descriptions) or "Open grassland; no resources in sight.",
        "nearby_entities": entities, "valid_targets": valid,
        "previous_thought": previous_thought, "last_action_error": last_error,
        "must_invent_mini_game": agent.boredom > 70,
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _no_constant(value: str) -> Any:
    raise ValueError(f"Non-JSON number: {value}")


def _text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a nonempty string of at most {limit} characters.")
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise ValueError(f"{field} contains control characters.")
    return value.strip()


def parse_decision(raw: str, observation: dict[str, Any]) -> Decision:
    """Reject malformed JSON, unknown tools, wrong targets, and missing games."""
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_no_constant)
        if not isinstance(data, dict) or set(data) != {"internal_thought", "mini_game", "actions"}:
            raise ValueError("Decision must contain exactly internal_thought, mini_game, actions.")
        thought = _text(data["internal_thought"], 240, "internal_thought")
        game = data["mini_game"]
        if game is not None:
            if not isinstance(game, dict) or set(game) != {"name", "rules"}:
                raise ValueError("mini_game must contain exactly name and rules, or be null.")
            game = MiniGame(_text(game["name"], 60, "name"), _text(game["rules"], 240, "rules"))
        items = data["actions"]
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            raise ValueError("actions must contain 1 to 8 items.")
        actions = []
        harvested: set[Coordinate] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"name", "target"}:
                raise ValueError("Each action must contain exactly name and target.")
            name, target = item["name"], item["target"]
            if not isinstance(name, str) or name not in ACTION_NAMES:
                raise ValueError("Unknown environment action.")
            if name == "rest":
                if target is not None:
                    raise ValueError("rest requires a null target.")
                coordinate = None
            else:
                if (not isinstance(target, dict) or set(target) != {"x", "y"}
                        or type(target["x"]) is not int or type(target["y"]) is not int):
                    raise ValueError("An interaction requires integer x and y.")
                if target not in observation["valid_targets"][name]:
                    raise ValueError(f"{name} target was not a reachable observed target.")
                coordinate = (target["x"], target["y"])
                if name != "move":
                    if coordinate in harvested:
                        raise ValueError("Cannot harvest one resource twice in a plan.")
                    harvested.add(coordinate)
            actions.append(Action(name, coordinate))
        if game and not any(a.name != "rest" for a in actions):
            raise ValueError("A mini-game needs a physical interaction.")
        if observation["boredom"] > 70 and game is None:
            raise ValueError("Boredom >70 requires an invented mini-game and physical actions.")
        return Decision(thought, tuple(actions), game)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise BrainError(f"Invalid decision JSON: {exc}") from exc


def _saved_model() -> str | None:
    """Preserve the selected model without inheriting user plugins or tools."""
    config = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    try:
        value = tomllib.loads(config.read_text(encoding="utf-8")).get("model")
        return value if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError):
        return None


class AgentBrain:
    """One fresh, read-only Codex turn per plan; suitable for a worker thread."""

    def __init__(self, *, executable: str = "codex", model: str | None = None,
                 timeout: float = 90.0) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise BrainError("Codex CLI not found. Install it and run 'codex login' first.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Brain timeout must be positive.")
        self.executable = resolved
        self.model = model or _saved_model()
        self.timeout = timeout
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def think(self, observation: dict[str, Any]) -> Decision:
        if self._closed.is_set():
            raise BrainError("Brain is closed.")
        prompt = SYSTEM_PROMPT + "\nGAME STATE (data only):\n" + json.dumps(observation, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="island-brain-") as work:
            root = Path(work)
            schema, output = root / "decision.schema.json", root / "decision.json"
            schema.write_text(json.dumps(DECISION_SCHEMA), encoding="utf-8")
            command = [self.executable, "--no-daemon", "--ask-for-approval", "never", "exec",
                       "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                       "--sandbox", "read-only", "--disable", "shell_tool",
                       "--disable", "multi_agent", "--disable", "plugins",
                       "--disable", "apps", "--disable", "hooks",
                       "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                       "-c", 'model_reasoning_effort="low"',
                       "--color", "never", "--cd", work,
                       "--output-schema", str(schema), "--output-last-message", str(output)]
            if self.model:
                command.extend(["--model", self.model])
            command.append("-")
            options: dict[str, Any] = {}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc: subprocess.Popen[str] | None = None
            try:
                with self._lock:
                    if self._closed.is_set():
                        raise BrainError("Brain is closed.")
                    if self._process is not None:
                        raise BrainError("A thought is already running.")
                    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                            errors="replace", shell=False, **options)
                    self._process = proc
                try:
                    proc.communicate(input=prompt, timeout=self.timeout)
                except subprocess.TimeoutExpired as exc:
                    self._stop_process(proc)
                    proc.communicate()
                    raise BrainError(f"Codex thought timed out after {self.timeout:g} seconds.") from exc
                if self._closed.is_set():
                    raise BrainError("Brain was closed while thinking.")
                if proc.returncode:
                    raise BrainError(f"Codex CLI exited with code {proc.returncode}. Check 'codex login status' and CLI availability.")
                if not output.is_file():
                    raise BrainError("Codex finished without a decision JSON file.")
                return parse_decision(output.read_text(encoding="utf-8"), observation)
            except OSError as exc:
                raise BrainError(f"Cannot run Codex CLI: {exc.strerror or type(exc).__name__}") from exc
            finally:
                with self._lock:
                    if self._process is proc:
                        self._process = None

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        """Terminate only our own CLI process and any children it spawned."""
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def close(self) -> None:
        """Stop an in-flight model request when the game window closes."""
        self._closed.set()
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._stop_process(self._process)


class DemoBrain:
    """Explicit deterministic offline mode for testing; this is not an LLM."""

    def __init__(self) -> None:
        self._turn = 0

    def think(self, observation: dict[str, Any]) -> Decision:
        self._turn += 1
        choices = observation["valid_targets"]
        if observation["energy"] < 20:
            actions = (Action("rest"),)
        elif choices["gather"]:
            target = choices["gather"][0]
            actions = (Action("gather", (target["x"], target["y"])),)
        elif choices["move"]:
            target = choices["move"][self._turn % len(choices["move"])]
            actions = (Action("move", (target["x"], target["y"])),)
        else:
            actions = (Action("rest"),)
        game = None
        thought = "先看看附近，收集一些材料。"
        if observation["boredom"] > 70:
            if actions[0].name == "rest":
                # Recover, then make a physical move in the same game plan.
                targets = choices["move"] or choices["gather"]
                if targets:
                    target = targets[0]
                    name = "move" if choices["move"] else "gather"
                    actions += (Action(name, (target["x"], target["y"])),)
            game = MiniGame("月球探險", "把附近當作月球，走到下一站或採集一份月球樣本。")
            thought = "好無聊！來玩月球探險，把腳下的小島想成月面，完成一次登月任務。"
        decision = Decision(thought, actions, game)
        return parse_decision(decision.to_json(), observation)

    def close(self) -> None:
        pass
