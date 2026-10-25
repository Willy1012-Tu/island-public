"""Make one REAL Codex decision, execute it, and save evidence of a mini-game.

Run from the repository root: python tools/smoke_codex.py
This consumes one normal Codex account request; it needs an existing CLI login.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_brain import AgentBrain, build_observation
from island_world import IslandWorld
from simulation import Simulation


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    world = IslandWorld(seed=7)
    world.agent.boredom = 85
    observation = build_observation(world)
    brain = AgentBrain(timeout=90)
    try:
        decision = brain.think(observation)
    finally:
        brain.close()
    assert decision.mini_game is not None
    simulation = Simulation(world)
    simulation.accept_decision(decision)
    for _ in range(300):
        simulation.advance_tick()
        if not simulation.has_goal:
            break
    assert simulation.last_error is None, simulation.last_error
    assert simulation.games_completed == 1
    assert simulation.decisions_completed == 1
    assert world.agent.boredom <= 55
    result = {
        "provider": "codex-cli", "model": brain.model,
        "boredom_before": observation["boredom"], "boredom_after": world.agent.boredom,
        "decision": decision.to_dict(), "ticks_to_complete": simulation.tick_count,
        "games_completed": simulation.games_completed, "inventory": world.agent.inventory,
        "position_after": {"x": world.agent.x, "y": world.agent.y},
    }
    destination = Path(__file__).resolve().parents[1] / "artifacts"
    destination.mkdir(exist_ok=True)
    (destination / "codex_smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
