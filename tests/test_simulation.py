"""Behavior checks for routes and tick-by-tick execution, without a model."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from island_world import Agent, IslandWorld, Rock, Tree
from pathfinding import find_interaction_path, find_path
from simulation import Simulation


def empty_world() -> IslandWorld:
    world = IslandWorld(tree_count=0, rock_count=0, seed=7)
    world.agent = Agent(world, 2, 2)
    return world


def action(name, target=None):
    return SimpleNamespace(name=name, target=target)


def decision(*actions, game=None):
    return SimpleNamespace(
        internal_thought="A short public thought.",
        actions=tuple(actions),
        mini_game=game,
    )


def test_astar_shortest_route_avoids_obstacles_and_is_deterministic():
    world = empty_world()
    world.grid[2][3] = Rock(3, 2)
    path = find_path(world, (2, 2), (4, 2))
    assert path is not None
    assert len(path) == 4
    assert path[-1] == (4, 2)
    assert (2, 2) not in path and (3, 2) not in path
    previous = (2, 2)
    for step in path:
        assert abs(step[0] - previous[0]) + abs(step[1] - previous[1]) == 1
        assert world.get_entity_at(step) is None
        previous = step
    assert find_path(world, (2, 2), (4, 2)) == path


def test_astar_handles_same_position_water_and_unreachable_destination():
    world = empty_world()
    assert find_path(world, (2, 2), (2, 2)) == []
    assert find_path(world, (2, 2), (0, 2)) is None
    for x, y in ((3, 4), (4, 3), (5, 4), (4, 5)):
        world.grid[y][x] = Rock(x, y)
    assert find_path(world, (2, 2), (4, 4)) is None


def test_interaction_path_uses_reachable_adjacent_cell_not_resource():
    world = empty_world()
    world.grid[2][5] = Tree(5, 2)
    path = find_interaction_path(world, (2, 2), (5, 2))
    assert path == [(3, 2), (4, 2)]
    assert find_interaction_path(world, (4, 2), (5, 2)) == []


def test_gather_walks_one_step_per_tick_then_harvests_separately():
    world = empty_world()
    world.grid[2][5] = Tree(5, 2)
    simulation = Simulation(world)
    simulation.accept_decision(decision(action("gather", (5, 2))))
    simulation.advance_tick()
    assert (world.agent.x, world.agent.y) == (3, 2)
    assert world.agent.inventory == {}
    assert simulation.path == [(4, 2)]
    simulation.advance_tick()
    assert (world.agent.x, world.agent.y) == (4, 2)
    assert world.agent.inventory == {} and simulation.has_goal
    simulation.advance_tick()
    assert world.agent.inventory == {"wood": 1}
    assert world.get_entity_at((5, 2)) is None
    assert not simulation.has_goal
    assert simulation.decisions_completed == 1
    assert world.agent.boredom == simulation.tick_count == 3
    assert world.agent.energy == 93


def test_adjacent_harvest_does_not_move_onto_tree():
    world = empty_world()
    world.grid[2][3] = Tree(3, 2)
    simulation = Simulation(world)
    simulation.accept_decision(decision(action("chop_tree", (3, 2))))
    simulation.advance_tick()
    assert (world.agent.x, world.agent.y) == (2, 2)
    assert world.agent.inventory == {"wood": 1}
    assert not simulation.has_goal


def test_rests_automatically_without_discarding_gather_goal():
    world = empty_world()
    world.agent.energy = 0
    world.grid[2][3] = Rock(3, 2)
    simulation = Simulation(world)
    simulation.accept_decision(decision(action("gather", (3, 2))))
    simulation.advance_tick()
    assert world.agent.energy == 10
    assert world.agent.inventory == {} and simulation.has_goal
    simulation.advance_tick()
    assert world.agent.energy == 2
    assert world.agent.inventory == {"stone": 1}
    assert not simulation.has_goal


def test_move_recovery_and_multiple_actions_still_use_one_primitive_per_tick():
    world = empty_world()
    world.agent.energy = 0
    simulation = Simulation(world)
    simulation.accept_decision(decision(action("move", (3, 2)), action("rest")))
    simulation.advance_tick()
    assert (world.agent.x, world.agent.y) == (2, 2)
    assert world.agent.energy == 10
    simulation.advance_tick()
    assert (world.agent.x, world.agent.y) == (3, 2)
    assert world.agent.energy == 9 and simulation.has_goal
    simulation.advance_tick()
    assert world.agent.energy == 19 and not simulation.has_goal


def test_game_completes_only_after_the_full_action_sequence():
    world = empty_world()
    world.agent.boredom = 80
    original_boredom = world.agent.boredom
    finish = Mock(side_effect=lambda: setattr(world.agent, "boredom", world.agent.boredom - 45))
    world.agent.finish_mini_game = finish
    simulation = Simulation(world)
    game = SimpleNamespace(name="Hop and return", rules="Visit a tile, then return.")
    simulation.accept_decision(
        decision(action("move", (3, 2)), action("move", (2, 2)), game=game)
    )
    simulation.advance_tick()
    finish.assert_not_called()
    assert simulation.has_goal and simulation.games_completed == 0
    simulation.advance_tick()
    finish.assert_called_once_with()
    assert world.agent.boredom == original_boredom + 2 - 45
    assert simulation.games_completed == simulation.decisions_completed == 1
    assert not simulation.has_goal


def test_invalid_plan_clears_goal_and_preserves_error_for_next_prompt():
    world = empty_world()
    simulation = Simulation(world)
    simulation.accept_decision(decision(action("move", (0, 0))))
    simulation.advance_tick()
    assert not simulation.has_goal
    assert simulation.last_error and "No reachable route" in simulation.last_error
    assert simulation.decisions_completed == 0
    assert world.agent.energy == 100
    assert world.agent.boredom == 1


def test_rejects_replacing_active_goal_and_accepts_next_after_completion():
    world = empty_world()
    simulation = Simulation(world)
    simulation.accept_decision(decision(action("move", (3, 2))))
    with pytest.raises(ValueError, match="active goal"):
        simulation.accept_decision(decision(action("rest")))
    simulation.advance_tick()
    simulation.accept_decision(decision(action("rest")))
    assert simulation.has_goal


def test_tick_without_goal_advances_world_without_changing_energy():
    world = empty_world()
    simulation = Simulation(world)
    simulation.advance_tick()
    assert simulation.tick_count == world.agent.boredom == 1
    assert world.agent.energy == 100


def test_new_obstacle_replans_the_remaining_route():
    world = empty_world()
    simulation = Simulation(world)
    simulation.accept_decision(decision(action("move", (5, 2))))
    simulation.advance_tick()
    assert (world.agent.x, world.agent.y) == (3, 2)
    world.grid[2][4] = Rock(4, 2)
    for _ in range(8):
        simulation.advance_tick()
        if not simulation.has_goal:
            break
    assert (world.agent.x, world.agent.y) == (5, 2)
    assert simulation.last_error is None
