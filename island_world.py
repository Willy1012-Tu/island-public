"""A small grid-based island world with a single agent."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TypeAlias

Coordinate: TypeAlias = tuple[int, int]


@dataclass
class Entity:
    x: int
    y: int
    name: str


class Tree(Entity):
    def __init__(self, x: int, y: int) -> None:
        super().__init__(x, y, "tree")


class Rock(Entity):
    def __init__(self, x: int, y: int) -> None:
        super().__init__(x, y, "rock")


class Water(Entity):
    def __init__(self, x: int, y: int) -> None:
        super().__init__(x, y, "water")


class IslandWorld:
    """A 20-by-20 island whose perimeter is surrounded by water."""

    SIZE = 20

    def __init__(
        self,
        tree_count: int = 35,
        rock_count: int = 20,
        seed: int | None = None,
    ) -> None:
        if tree_count < 0 or rock_count < 0:
            raise ValueError("Resource counts cannot be negative.")
        if tree_count + rock_count >= (self.SIZE - 2) ** 2:
            raise ValueError("Resources must leave at least one interior cell for the agent.")

        self._random = random.Random(seed)
        self.grid: list[list[Entity | None]] = [
            [None for _ in range(self.SIZE)] for _ in range(self.SIZE)
        ]
        self._place_water_boundary()

        available = [
            (x, y)
            for y in range(1, self.SIZE - 1)
            for x in range(1, self.SIZE - 1)
        ]
        self._random.shuffle(available)
        positions = iter(available)
        for _ in range(tree_count):
            x, y = next(positions)
            self.grid[y][x] = Tree(x, y)
        for _ in range(rock_count):
            x, y = next(positions)
            self.grid[y][x] = Rock(x, y)

        x, y = next((x, y) for x, y in available if self.grid[y][x] is None)
        self.agent = Agent(self, x, y)

    def _place_water_boundary(self) -> None:
        for x in range(self.SIZE):
            self.grid[0][x] = Water(x, 0)
            self.grid[self.SIZE - 1][x] = Water(x, self.SIZE - 1)
        for y in range(1, self.SIZE - 1):
            self.grid[y][0] = Water(0, y)
            self.grid[y][self.SIZE - 1] = Water(self.SIZE - 1, y)

    def get_entity_at(self, coordinate: Coordinate) -> Entity | None:
        x, y = coordinate
        if not self.in_bounds(coordinate):
            return None
        return self.grid[y][x]

    def in_bounds(self, coordinate: Coordinate) -> bool:
        x, y = coordinate
        return 0 <= x < self.SIZE and 0 <= y < self.SIZE

    def tick(self) -> None:
        """Advance the world by one tick."""
        self.agent.tick()


class Agent:
    """The player character, with a position, inventory, boredom, and energy."""

    MAX_ENERGY = 100
    ACTION_COSTS = {"move": 1, "chop_tree": 5, "mine_rock": 8}

    def __init__(self, world: IslandWorld, x: int, y: int) -> None:
        if not world.in_bounds((x, y)) or world.get_entity_at((x, y)) is not None:
            raise ValueError("The agent must start on an empty interior cell.")
        self.world = world
        self.x = x
        self.y = y
        self.inventory: dict[str, int] = {}
        self.boredom = 0
        self.energy = self.MAX_ENERGY

    def tick(self) -> None:
        self.boredom = min(100, self.boredom + 1)

    def finish_mini_game(self) -> None:
        """Reward a completed sequence of physical game actions."""
        self.boredom = max(0, self.boredom - 45)

    def get_surroundings(self, radius: int = 3) -> list[Entity]:
        """Return all map entities within Euclidean distance ``radius``."""
        if radius < 0:
            raise ValueError("Radius cannot be negative.")

        nearby: list[Entity] = []
        for row in self.world.grid:
            for entity in row:
                if entity is None:
                    continue
                if (entity.x - self.x) ** 2 + (entity.y - self.y) ** 2 <= radius**2:
                    nearby.append(entity)
        return nearby

    def execute_action(
        self, action_name: str, target: Coordinate | None
    ) -> None:
        """Perform a move, harvest, or rest action; invalid actions raise ValueError."""
        if action_name == "rest":
            if target is not None:
                raise ValueError("Rest does not take a target.")
            self.energy = min(self.MAX_ENERGY, self.energy + 10)
            return

        if action_name == "move":
            coordinate = self._require_coordinate(target)
            if abs(coordinate[0] - self.x) + abs(coordinate[1] - self.y) != 1:
                raise ValueError("Movement target must be one orthogonal step away.")
            if not self.world.in_bounds(coordinate):
                raise ValueError("Movement target is outside the island.")
            entity = self.world.get_entity_at(coordinate)
            if entity is not None:
                raise ValueError(f"Cannot move onto {entity.name}.")
            self._spend_energy("move")
            self.x, self.y = coordinate
            return

        if action_name in ("gather", "chop_tree", "mine_rock"):
            coordinate = self._require_coordinate(target)
            if abs(coordinate[0] - self.x) + abs(coordinate[1] - self.y) != 1:
                raise ValueError("Harvest target must be one orthogonal step away.")
            entity = self.world.get_entity_at(coordinate)
            if action_name == "gather":
                if isinstance(entity, Tree):
                    action_name = "chop_tree"
                elif isinstance(entity, Rock):
                    action_name = "mine_rock"
                else:
                    raise ValueError("There is no harvestable resource at the target.")

            expected_type = Tree if action_name == "chop_tree" else Rock
            if not isinstance(entity, expected_type):
                resource = "tree" if action_name == "chop_tree" else "rock"
                raise ValueError(f"There is no {resource} at the target.")

            self._spend_energy(action_name)
            resource_name = "wood" if action_name == "chop_tree" else "stone"
            self.inventory[resource_name] = self.inventory.get(resource_name, 0) + 1
            self.world.grid[coordinate[1]][coordinate[0]] = None
            return

        raise ValueError(f"Unknown action: {action_name}")

    @staticmethod
    def _require_coordinate(target: Coordinate | None) -> Coordinate:
        if (
            not isinstance(target, tuple)
            or len(target) != 2
            or not all(type(value) is int for value in target)
        ):
            raise ValueError("This action requires a target coordinate (x, y).")
        return target

    def _spend_energy(self, action_name: str) -> None:
        cost = self.ACTION_COSTS[action_name]
        if self.energy < cost:
            raise ValueError("Not enough energy to perform this action.")
        self.energy -= cost
