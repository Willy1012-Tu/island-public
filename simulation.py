"""The fast game loop: execute one safe primitive per world tick."""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from island_world import Coordinate, IslandWorld, Rock, Tree
from pathfinding import find_interaction_path, find_path

if TYPE_CHECKING:
    from agent_brain import Action, Decision, MiniGame


class Simulation:
    """Execute a structured plan independently of the model's slower thinking.

    ``pending_actions[0]`` is the current high-level action. A distant move or
    harvest remains current until its A* route and final interaction finish.
    """

    def __init__(
        self, world: IslandWorld, logger: logging.Logger | None = None
    ) -> None:
        self.world = world
        self.logger = logger or logging.getLogger(__name__)
        self.tick_count = 0
        self.pending_actions: deque[Action] = deque()
        self.path: list[Coordinate] = []
        self.internal_thought = "Waiting for the agent's first thought."
        self.status = "Idle"
        self.last_error: str | None = None
        self.decisions_completed = 0
        self.games_completed = 0
        self._mini_game: MiniGame | None = None
        self._prepared = False

    @property
    def has_goal(self) -> bool:
        return bool(self.pending_actions)

    def accept_decision(self, decision: Decision) -> None:
        """Start a plan only when the preceding plan has finished or failed."""
        if self.has_goal:
            raise ValueError("Cannot replace an active goal.")
        if not decision.actions:
            raise ValueError("A decision must contain at least one action.")
        self.pending_actions.extend(decision.actions)
        self.internal_thought = decision.internal_thought
        self._mini_game = decision.mini_game
        self.last_error = None
        self._prepared = False
        self.path = []
        self.status = f"Goal: {self.pending_actions[0].name}"
        self.logger.info("THOUGHT %s", self.internal_thought)
        if self._mini_game is not None:
            self.logger.info("GAME %s: %s", self._mini_game.name, self._mini_game.rules)

    def advance_tick(self) -> None:
        """Advance boredom and perform at most one movement/harvest/rest."""
        self.world.tick()
        self.tick_count += 1
        if not self.has_goal:
            return
        try:
            self._execute_current()
        except ValueError as error:
            self._fail_plan(str(error))

    def _execute_current(self) -> None:
        action = self.pending_actions[0]
        agent = self.world.agent

        if action.name == "rest":
            if action.target is not None:
                raise ValueError("Rest does not take a target.")
            agent.execute_action("rest", None)
            self.status = f"Resting; energy {agent.energy}"
            self.logger.info("TICK %d REST energy=%d", self.tick_count, agent.energy)
            self._complete_action()
            return

        if action.name not in ("move", "gather", "chop_tree", "mine_rock"):
            raise ValueError(f"Unknown action: {action.name}")
        target = self._coordinate(action.target)
        actual_action = action.name
        if action.name != "move":
            actual_action = self._harvest_action(action.name, target)

        if not self._prepared:
            self._prepare_route(action.name, target)
        elif self.path:
            next_step = self.path[0]
            current = (agent.x, agent.y)
            if (
                abs(current[0] - next_step[0]) + abs(current[1] - next_step[1]) != 1
                or self.world.get_entity_at(next_step) is not None
            ):
                self._prepare_route(action.name, target)

        if self.path:
            if not self._has_energy("move"):
                return
            step = self.path[0]
            agent.execute_action("move", step)
            self.path.pop(0)
            self.status = f"Walking to {target}; {len(self.path)} steps left"
            self.logger.info(
                "TICK %d MOVE (%d,%d) energy=%d boredom=%d",
                self.tick_count, agent.x, agent.y, agent.energy, agent.boredom,
            )
            if action.name == "move" and not self.path:
                self._complete_action()
            return

        if action.name == "move":
            # A destination equal to the current position is a harmless no-op.
            self._complete_action()
            return
        if not self._has_energy(actual_action):
            return
        agent.execute_action(action.name, target)
        self.status = f"Completed {action.name} at {target}"
        self.logger.info(
            "TICK %d %s target=%s inventory=%s energy=%d",
            self.tick_count, action.name.upper(), target, agent.inventory, agent.energy,
        )
        self._complete_action()

    def _prepare_route(self, action_name: str, target: Coordinate) -> None:
        agent = self.world.agent
        start = (agent.x, agent.y)
        if action_name == "move":
            route = find_path(self.world, start, target)
        else:
            route = find_interaction_path(self.world, start, target)
        if route is None:
            raise ValueError(f"No reachable route for {action_name} at {target}.")
        self.path = route
        self._prepared = True

    def _harvest_action(self, action_name: str, target: Coordinate) -> str:
        entity = self.world.get_entity_at(target)
        if action_name == "gather":
            if isinstance(entity, Tree):
                return "chop_tree"
            if isinstance(entity, Rock):
                return "mine_rock"
            raise ValueError(f"No harvestable resource at {target}.")
        expected = Tree if action_name == "chop_tree" else Rock
        if not isinstance(entity, expected):
            raise ValueError(f"Wrong or missing resource for {action_name} at {target}.")
        return action_name

    def _has_energy(self, primitive: str) -> bool:
        agent = self.world.agent
        if agent.energy >= agent.ACTION_COSTS[primitive]:
            return True
        agent.execute_action("rest", None)
        self.status = f"Recovering energy for {primitive}; goal preserved"
        self.logger.info(
            "TICK %d AUTO-REST energy=%d before %s",
            self.tick_count, agent.energy, primitive,
        )
        return False

    def _complete_action(self) -> None:
        self.pending_actions.popleft()
        self.path = []
        self._prepared = False
        if self.has_goal:
            return
        self.decisions_completed += 1
        if self._mini_game is not None:
            self.world.agent.finish_mini_game()
            self.games_completed += 1
            self.status = f"Game completed: {self._mini_game.name}"
            self.logger.info(
                "GAME COMPLETE %s boredom=%d",
                self._mini_game.name, self.world.agent.boredom,
            )
        else:
            self.status = "Goal completed; ready to think"
        self._mini_game = None

    def _fail_plan(self, message: str) -> None:
        self.last_error = message
        self.status = f"Goal failed: {message}"
        self.pending_actions.clear()
        self.path = []
        self._prepared = False
        self._mini_game = None
        self.logger.warning("TICK %d GOAL FAILED %s", self.tick_count, message)

    @staticmethod
    def _coordinate(target: Coordinate | None) -> Coordinate:
        if (
            not isinstance(target, tuple)
            or len(target) != 2
            or any(type(value) is not int for value in target)
        ):
            raise ValueError("An action requires an integer target coordinate (x, y).")
        return target
