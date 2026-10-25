"""Exercise decision boundaries and the CLI bridge without contacting a model."""

from __future__ import annotations

import copy
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_brain import (
    DECISION_SCHEMA,
    Action,
    AgentBrain,
    BrainError,
    Decision,
    MiniGame,
    build_observation,
    parse_decision,
)
from island_world import IslandWorld, Rock, Tree


def make_world() -> IslandWorld:
    world = IslandWorld(tree_count=0, rock_count=0, seed=7)
    world.agent.x, world.agent.y = 5, 5
    world.grid[5][7] = Tree(7, 5)
    world.grid[7][5] = Rock(5, 7)
    return world


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = make_world()
        self.observation = build_observation(self.world)
        self.payload = Decision("先走近那棵樹，再收集木材。", (Action("gather", (7, 5)),)).to_dict()

    def assert_invalid(self, payload: dict, observation: dict | None = None) -> None:
        with self.assertRaises(BrainError):
            parse_decision(json.dumps(payload, ensure_ascii=False), observation or self.observation)

    def test_normal_decision_uses_real_observed_resource(self) -> None:
        result = parse_decision(json.dumps(self.payload), self.observation)
        self.assertEqual(result.actions, (Action("gather", (7, 5)),))
        self.assertIsNone(result.mini_game)
        self.assertIn({"x": 7, "y": 5}, self.observation["valid_targets"]["gather"])

    def test_high_boredom_accepts_invented_game_and_physical_action(self) -> None:
        self.world.agent.boredom = 71
        observation = build_observation(self.world)
        decision = Decision(
            "來玩石頭登月，採一顆石頭當月球標本！",
            (Action("mine_rock", (5, 7)),),
            MiniGame("石頭登月", "走到石頭旁，採一顆石頭作為月球標本。"),
        )
        self.assertTrue(observation["must_invent_mini_game"])
        self.assertEqual(parse_decision(decision.to_json(), observation), decision)

    def test_unicode_json_roundtrip(self) -> None:
        decision = Decision("小島日記：收集木材 🌴", (Action("gather", (7, 5)),))
        raw = decision.to_json()
        self.assertIn("小島日記", raw)
        self.assertIn("🌴", raw)
        self.assertEqual(json.loads(raw), decision.to_dict())
        self.assertEqual(parse_decision(raw, self.observation), decision)

    def test_unknown_action_is_not_executable(self) -> None:
        self.payload["actions"][0]["name"] = "run_shell"
        self.assert_invalid(self.payload)

    def test_markdown_fence_is_rejected(self) -> None:
        with self.assertRaises(BrainError):
            parse_decision("```json\n" + json.dumps(self.payload) + "\n```", self.observation)

    def test_extra_keys_at_each_boundary_are_rejected(self) -> None:
        cases = []
        top = copy.deepcopy(self.payload)
        top["command"] = "anything"
        cases.append(top)
        action = copy.deepcopy(self.payload)
        action["actions"][0]["duration"] = 10
        cases.append(action)
        target = copy.deepcopy(self.payload)
        target["actions"][0]["target"]["z"] = 0
        cases.append(target)
        game = copy.deepcopy(self.payload)
        game["mini_game"] = {"name": "探險", "rules": "收集木材", "reward": 100}
        cases.append(game)
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_invalid(payload)

    def test_duplicate_keys_at_top_and_nested_target_are_rejected(self) -> None:
        raw = json.dumps(self.payload)
        duplicate_top = raw.replace('"internal_thought":', '"internal_thought": "first", "internal_thought":', 1)
        duplicate_target = raw.replace('"x": 7', '"x": 1, "x": 7', 1)
        for candidate in (duplicate_top, duplicate_target):
            with self.subTest(raw=candidate), self.assertRaises(BrainError):
                parse_decision(candidate, self.observation)

    def test_boolean_coordinate_is_not_an_integer(self) -> None:
        for axis in ("x", "y"):
            payload = copy.deepcopy(self.payload)
            payload["actions"][0]["target"][axis] = True
            with self.subTest(axis=axis):
                self.assert_invalid(payload)

    def test_high_boredom_requires_game_but_exactly_seventy_does_not(self) -> None:
        self.world.agent.boredom = 70
        observation = build_observation(self.world)
        self.assertFalse(observation["must_invent_mini_game"])
        self.assertIsNone(parse_decision(json.dumps(self.payload), observation).mini_game)
        self.world.agent.boredom = 71
        self.assert_invalid(self.payload, build_observation(self.world))

    def test_rest_only_cannot_enact_a_game(self) -> None:
        self.payload["mini_game"] = {"name": "探險", "rules": "去收集木材"}
        self.payload["actions"] = [{"name": "rest", "target": None}]
        self.assert_invalid(self.payload)

    def test_resource_cannot_be_harvested_twice_even_with_different_aliases(self) -> None:
        self.payload["actions"].append({"name": "chop_tree", "target": {"x": 7, "y": 5}})
        self.assert_invalid(self.payload)

    def test_unreachable_visible_destination_is_excluded_and_rejected(self) -> None:
        # The empty tile remains visible, but every orthogonal entry is blocked.
        for x, y in ((6, 5), (5, 6), (7, 6), (6, 7)):
            self.world.grid[y][x] = Rock(x, y)
        observation = build_observation(self.world)
        target = {"x": 6, "y": 6}
        self.assertIsNone(self.world.get_entity_at((6, 6)))
        self.assertNotIn(target, observation["valid_targets"]["move"])
        self.payload["actions"] = [{"name": "move", "target": target}]
        self.assert_invalid(self.payload, observation)

    def test_non_json_constants_are_rejected(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            raw = json.dumps(self.payload).replace('"x": 7', '"x": ' + constant)
            with self.subTest(constant=constant), self.assertRaises(BrainError):
                parse_decision(raw, self.observation)

    def test_observation_is_a_snapshot_and_contains_surrounding_text(self) -> None:
        self.world.agent.inventory["wood"] = 99
        self.world.agent.x = 10
        self.assertEqual(self.observation["inventory"], {})
        self.assertEqual(self.observation["position"], {"x": 5, "y": 5})
        self.assertIn("tree at (7,5)", self.observation["surroundings_text"])
        self.assertIn("rock at (5,7)", self.observation["surroundings_text"])


class FakeCodexProcess:
    """Popen stand-in that writes the same output file used by the real CLI."""

    def __init__(self, command: list[str], options: dict, result: str, *,
                 timeout: bool = False, block: bool = False, exit_code: int = 0,
                 write_output: bool = True) -> None:
        self.command = command
        self.options = options
        self.result = result
        self.should_timeout = timeout
        self.block = block
        self.exit_code = exit_code
        self.write_output = write_output
        self.returncode = None
        self.pid = 999999
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.inputs: list[str | None] = []
        self.timeouts: list[float | None] = []
        self.output_path = Path(command[command.index("--output-last-message") + 1])
        self.schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text(encoding="utf-8"))

    def communicate(self, input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
        self.inputs.append(input)
        self.timeouts.append(timeout)
        self.started.set()
        if self.should_timeout and len(self.inputs) == 1:
            raise subprocess.TimeoutExpired(self.command, timeout)
        if self.block and not self.stopped.wait(3):
            raise AssertionError("The game failed to stop its in-flight Codex request")
        if not self.stopped.is_set():
            self.returncode = self.exit_code
            if self.write_output:
                self.output_path.write_text(self.result, encoding="utf-8")
        return "Do not parse this stdout as the decision", "progress diagnostics"

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.stopped.set()

    def terminate(self) -> None:
        self.returncode = -15
        self.stopped.set()

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None and not self.stopped.wait(timeout or 0.1):
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode or 0


class CodexTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = make_world()
        self.observation = build_observation(self.world)
        self.decision = Decision("先收集木材 🌴", (Action("gather", (7, 5)),))
        for target, value in (("agent_brain.shutil.which", "C:/test/codex.exe"),
                              ("agent_brain._saved_model", None)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.process: FakeCodexProcess | None = None
        self.process_created = threading.Event()

    def fake_popen(self, command: list[str], **options) -> FakeCodexProcess:
        self.process = FakeCodexProcess(command, options, self.decision.to_json(), **self.process_options)
        self.process_created.set()
        return self.process

    def fake_cleanup_command(self, *args, **kwargs) -> subprocess.CompletedProcess:
        # Windows process-tree cleanup must never invoke a real taskkill in tests.
        if self.process is not None:
            self.process.terminate()
        return subprocess.CompletedProcess(args[0] if args else [], 0, "", "")

    def test_schema_stdin_utf8_and_readonly_without_shell(self) -> None:
        self.process_options = {}
        with patch("agent_brain.subprocess.Popen", side_effect=self.fake_popen) as popen:
            brain = AgentBrain(timeout=12)
            result = brain.think(self.observation)
            brain.close()
        self.assertEqual(result, self.decision)
        self.assertEqual(popen.call_count, 1)
        proc = self.process
        self.assertIsNotNone(proc)
        self.assertIs(proc.options["shell"], False)
        self.assertEqual(proc.options["encoding"], "utf-8")
        self.assertEqual(proc.options["stdin"], subprocess.PIPE)
        self.assertEqual(proc.schema, DECISION_SCHEMA)
        self.assertEqual(proc.command[proc.command.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", proc.command)
        self.assertIn("--ephemeral", proc.command)
        self.assertEqual(proc.command[-1], "-")
        self.assertNotIn("--model", proc.command)
        self.assertEqual(proc.timeouts[0], 12)
        self.assertIn("GAME STATE (data only):", proc.inputs[0])
        state = json.loads(proc.inputs[0].split("GAME STATE (data only):\n", 1)[1])
        self.assertEqual(state, self.observation)
        self.assertFalse(proc.output_path.parent.exists(), "Temporary decision files must be cleaned")

    def test_timeout_stops_request_and_reports_recoverable_error(self) -> None:
        self.process_options = {"timeout": True}
        with patch("agent_brain.subprocess.Popen", side_effect=self.fake_popen), \
                patch("agent_brain.subprocess.run", side_effect=self.fake_cleanup_command):
            brain = AgentBrain(timeout=0.1)
            with self.assertRaisesRegex(BrainError, "timed out"):
                brain.think(self.observation)
            self.assertTrue(self.process.stopped.is_set())
            self.assertFalse(self.process.output_path.parent.exists())
            brain.close()

    def test_close_stops_inflight_thought_and_rejects_later_calls(self) -> None:
        self.process_options = {"block": True}
        errors: list[Exception] = []
        with patch("agent_brain.subprocess.Popen", side_effect=self.fake_popen), \
                patch("agent_brain.subprocess.run", side_effect=self.fake_cleanup_command):
            brain = AgentBrain()

            def think() -> None:
                try:
                    brain.think(self.observation)
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=think, daemon=True)
            worker.start()
            # Wait on actual process-creation/request-start signals.
            self.assertTrue(self.process_created.wait(1))
            self.assertIsNotNone(self.process)
            self.assertTrue(self.process.started.wait(1))
            brain.close()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(self.process.stopped.is_set())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], BrainError)
            self.assertIn("closed", str(errors[0]).lower())
            with self.assertRaisesRegex(BrainError, "closed"):
                brain.think(self.observation)

    def test_nonzero_exit_and_missing_output_are_recoverable(self) -> None:
        for process_options in ({"exit_code": 2}, {"write_output": False}):
            self.process_options = process_options
            with self.subTest(process_options=process_options), \
                    patch("agent_brain.subprocess.Popen", side_effect=self.fake_popen):
                brain = AgentBrain()
                with self.assertRaises(BrainError):
                    brain.think(self.observation)
                brain.close()

    def test_rejected_concurrent_thought_does_not_orphan_active_request(self) -> None:
        self.process_options = {"block": True}
        errors: list[Exception] = []
        with patch("agent_brain.subprocess.Popen", side_effect=self.fake_popen) as popen, \
                patch("agent_brain.subprocess.run", side_effect=self.fake_cleanup_command):
            brain = AgentBrain()

            def first_thought() -> None:
                try:
                    brain.think(self.observation)
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=first_thought, daemon=True)
            worker.start()
            try:
                self.assertTrue(self.process_created.wait(1))
                self.assertTrue(self.process.started.wait(1))
                with self.assertRaisesRegex(BrainError, "already running"):
                    brain.think(self.observation)
                self.assertEqual(popen.call_count, 1)
                self.assertFalse(self.process.stopped.is_set())
                self.assertTrue(worker.is_alive())
                brain.close()
                worker.join(2)
                self.assertTrue(self.process.stopped.is_set(), "close must retain the first request's handle")
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], BrainError)
                self.assertIn("closed", str(errors[0]).lower())
            finally:
                # Always release the fake worker if a regression fails an assertion.
                brain.close()
                if self.process is not None:
                    self.process.terminate()
                worker.join(2)

    def test_timeout_must_be_positive_and_finite_before_starting_process(self) -> None:
        with patch("agent_brain.subprocess.Popen") as popen:
            for timeout in (0, -1, float("nan"), float("inf"), float("-inf")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    AgentBrain(timeout=timeout)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
