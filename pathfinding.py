"""Deterministic, orthogonal A* routes through the island's empty cells."""

from __future__ import annotations

from heapq import heappop, heappush
from itertools import count

from island_world import Coordinate, IslandWorld


def _distance(first: Coordinate, second: Coordinate) -> int:
    return abs(first[0] - second[0]) + abs(first[1] - second[1])


def _neighbors(coordinate: Coordinate) -> tuple[Coordinate, ...]:
    x, y = coordinate
    return ((x, y - 1), (x - 1, y), (x + 1, y), (x, y + 1))


def _passable(world: IslandWorld, coordinate: Coordinate) -> bool:
    return world.in_bounds(coordinate) and world.get_entity_at(coordinate) is None


def find_path(
    world: IslandWorld, start: Coordinate, goal: Coordinate
) -> list[Coordinate] | None:
    """Return a shortest route excluding ``start`` and including ``goal``.

    Water, trees, and rocks are obstacles. ``None`` means no route exists;
    ``[]`` means the agent is already at the destination.
    """
    if not _passable(world, start) or not _passable(world, goal):
        return None
    if start == goal:
        return []

    sequence = count()
    frontier: list[tuple[int, int, int, Coordinate]] = []
    heappush(frontier, (_distance(start, goal), 0, next(sequence), start))
    previous: dict[Coordinate, Coordinate] = {}
    costs = {start: 0}

    while frontier:
        _, cost, _, current = heappop(frontier)
        if cost != costs[current]:
            continue
        if current == goal:
            route = []
            while current != start:
                route.append(current)
                current = previous[current]
            route.reverse()
            return route
        for neighbor in _neighbors(current):
            if not _passable(world, neighbor):
                continue
            next_cost = cost + 1
            if next_cost >= costs.get(neighbor, float("inf")):
                continue
            costs[neighbor] = next_cost
            previous[neighbor] = current
            heappush(
                frontier,
                (next_cost + _distance(neighbor, goal), next_cost, next(sequence), neighbor),
            )
    return None


def find_interaction_path(
    world: IslandWorld, start: Coordinate, target: Coordinate
) -> list[Coordinate] | None:
    """Route to the best reachable orthogonal neighbor of a resource.

    The target itself need not be passable. Choosing an adjacent destination
    keeps harvesting separate from moving onto a resource's occupied tile.
    """
    if not world.in_bounds(target):
        return None
    candidates: list[tuple[int, Coordinate, list[Coordinate]]] = []
    for neighbor in _neighbors(target):
        route = find_path(world, start, neighbor)
        if route is not None:
            candidates.append((len(route), neighbor, route))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]
