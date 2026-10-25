"""Exercise the boundary between slow thinking and fast simulation."""

from __future__ import annotations

import threading

import pytest

from agent_brain import Action, Decision, DemoBrain, build_observation
from island_world import IslandWorld
from main import BrainLoop, parse_args
from simulation import Simulation


class SlowBrain:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def think(self, observation):
        self.calls += 1
        self.started.set()
        self.release.wait(3)
        return Decision("休息一下。", (Action("rest"),))

    def close(self):
        self.release.set()


def test_slow_model_does_not_block_world_or_overlap_requests():
    simulation = Simulation(IslandWorld(tree_count=0, rock_count=0, seed=1))
    brain = SlowBrain()
    loop = BrainLoop(brain)
    try:
        loop.update(simulation, 0)
        assert brain.started.wait(1)
        for tick in range(5):
            simulation.advance_tick()
            loop.update(simulation, tick + 1)
        assert simulation.tick_count == 5
        assert simulation.world.agent.boredom == 5
        assert loop.thinking
        assert brain.calls == 1
        brain.release.set()
        assert loop.future is not None
        loop.future.result(timeout=1)
        loop.update(simulation, 10)
        assert simulation.has_goal
        assert loop.decisions == 1
        loop.update(simulation, 20)
        assert brain.calls == 1  # still executing the plan
        simulation.advance_tick()
        assert simulation.decisions_completed == 1
        loop.update(simulation, 21)
        assert loop.requests == 2
    finally:
        loop.close()


def test_pause_freezes_decision_application_and_dispatch():
    simulation = Simulation(IslandWorld(tree_count=0, rock_count=0))
    loop = BrainLoop(DemoBrain())
    try:
        loop.update(simulation, 0, paused=True)
        assert loop.requests == 0
        loop.update(simulation, 1)
        assert loop.future is not None
        loop.future.result(timeout=1)
        loop.update(simulation, 2, paused=True)
        assert not simulation.has_goal
        loop.update(simulation, 3)
        assert simulation.has_goal
    finally:
        loop.close()


def test_boredom_rising_during_thought_requires_new_game():
    simulation = Simulation(IslandWorld(tree_count=0, rock_count=0))
    brain = SlowBrain()
    loop = BrainLoop(brain)
    try:
        loop.update(simulation, 0)
        assert brain.started.wait(1)
        simulation.world.agent.boredom = 80
        brain.release.set()
        loop.future.result(timeout=1)
        loop.update(simulation, 1)
        assert not simulation.has_goal
        assert loop.failures == 1
        assert "above 70" in simulation.last_error
        loop.update(simulation, 1.5)
        assert loop.requests == 1  # failure backoff
        loop.update(simulation, 4)
        assert loop.requests == 2
    finally:
        loop.close()


def test_demo_game_physically_completes_and_relieves_boredom():
    world = IslandWorld(seed=7)
    world.agent.boredom = 85
    simulation = Simulation(world)
    decision = DemoBrain().think(build_observation(world))
    assert decision.mini_game is not None
    assert any(action.name != "rest" for action in decision.actions)
    simulation.accept_decision(decision)
    for _ in range(100):
        simulation.advance_tick()
        if not simulation.has_goal:
            break
    assert simulation.games_completed == 1
    assert world.agent.boredom <= 55


@pytest.mark.parametrize("arguments", [
    ["--ticks", "0"], ["--boredom", "101"], ["--tick-seconds", "0"],
    ["--tick-seconds", "nan"], ["--brain-timeout", "inf"],
    ["--think-delay", "-1"], ["--headless", "--screenshot", "test.png"],
])
def test_invalid_cli_options_fail_early(arguments):
    with pytest.raises(SystemExit):
        parse_args(arguments)
