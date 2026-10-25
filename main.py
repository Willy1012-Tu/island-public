"""Run the island: fast world ticks, slow background Codex decisions, optional UI."""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from agent_brain import AgentBrain, BrainError, Decision, DemoBrain, build_observation
from island_world import IslandWorld
from simulation import Simulation

LOGGER = logging.getLogger("island")


class Brain(Protocol):
    def think(self, observation: dict[str, Any]) -> Decision: ...
    def close(self) -> None: ...


class BrainLoop:
    """Keep at most one model request alive, without blocking ticks or drawing."""

    def __init__(self, brain: Brain, *, think_delay: float = 1.0,
                 logger: logging.Logger = LOGGER) -> None:
        self.brain = brain
        self.think_delay = think_delay
        self.logger = logger
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="island-brain")
        self.future: Future[Decision] | None = None
        self.next_thought_at = 0.0
        self.requests = 0
        self.decisions = 0
        self.failures = 0
        self._consecutive_failures = 0
        self.closed = False

    @property
    def thinking(self) -> bool:
        return self.future is not None

    def update(self, simulation: Simulation, now: float, *, paused: bool = False) -> None:
        if self.closed or paused:
            return
        if self.future is not None and self.future.done():
            future, self.future = self.future, None
            try:
                decision = future.result()
                # Boredom can change while a request is in flight (or by pressing B).
                if simulation.world.agent.boredom > 70 and decision.mini_game is None:
                    raise BrainError("Boredom rose above 70 while thinking; invent a mini-game now.")
                simulation.accept_decision(decision)
                self.logger.info("DECISION %s", decision.to_json())
                self.decisions += 1
                self._consecutive_failures = 0
                self.next_thought_at = now + self.think_delay
            except Exception as exc:
                self.failures += 1
                self._consecutive_failures += 1
                delay = min(30.0, 2.0 ** min(self._consecutive_failures, 5))
                simulation.last_error = str(exc)
                simulation.status = f"Thought failed; retry in {delay:g}s: {exc}"
                self.logger.warning("BRAIN FAILED %s (retry in %gs)", exc, delay)
                self.next_thought_at = now + delay
        if not simulation.has_goal and self.future is None and now >= self.next_thought_at:
            observation = build_observation(simulation.world, last_error=simulation.last_error,
                                            previous_thought=simulation.internal_thought)
            self.future = self.executor.submit(self.brain.think, observation)
            self.requests += 1
            simulation.status = "Codex is thinking..." if isinstance(self.brain, AgentBrain) else "Offline demo is deciding..."
            self.logger.info("THINK request=%d position=(%d,%d) boredom=%d energy=%d",
                             self.requests, simulation.world.agent.x, simulation.world.agent.y,
                             observation["boredom"], observation["energy"])

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.brain.close()
            if self.future is not None:
                self.future.cancel()
            self.executor.shutdown(wait=True, cancel_futures=True)


def _positive_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _nonnegative_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="An island agent powered by your logged-in Codex CLI.")
    parser.add_argument("--brain", choices=("codex", "demo"), default="codex",
                        help="codex: real model using CLI login (default); demo: offline rules, no LLM")
    parser.add_argument("--headless", action="store_true", help="CLI only; no Pygame window")
    parser.add_argument("--seed", type=int, default=7, help="world generation seed (default: 7)")
    parser.add_argument("--ticks", type=int, help="stop after this many game ticks")
    parser.add_argument("--tick-seconds", type=_positive_number, default=0.5,
                        help="seconds per world tick (default: 0.5)")
    parser.add_argument("--boredom", type=int, default=0, help="initial boredom, 0 to 100")
    parser.add_argument("--model", help="override the Codex model; otherwise preserve your saved selection")
    parser.add_argument("--codex-command", default="codex", help="Codex executable name or full path")
    parser.add_argument("--brain-timeout", type=_positive_number, default=90.0)
    parser.add_argument("--think-delay", type=_nonnegative_number, default=1.0,
                        help="minimum interval between plans, in seconds (default: 1)")
    parser.add_argument("--screenshot", type=Path, help="save the last window frame as a PNG")
    args = parser.parse_args(argv)
    if args.ticks is not None and args.ticks <= 0:
        parser.error("--ticks must be positive")
    if not 0 <= args.boredom <= 100:
        parser.error("--boredom must be between 0 and 100")
    if args.headless and args.screenshot:
        parser.error("--screenshot requires a window; use SDL_VIDEODRIVER=dummy for offscreen rendering")
    return args


def run(args: argparse.Namespace) -> int:
    world = IslandWorld(seed=args.seed)
    world.agent.boredom = args.boredom
    simulation = Simulation(world, LOGGER)
    simulation.internal_thought = "看看這座小島，下一步要做什麼呢？"
    brain: Brain = DemoBrain() if args.brain == "demo" else AgentBrain(
        executable=args.codex_command, model=args.model, timeout=args.brain_timeout)
    brain_loop = BrainLoop(brain, think_delay=args.think_delay)
    renderer = None
    paused = False
    running = True
    last_tick = time.monotonic()
    LOGGER.info("START brain=%s tick=%.3fs seed=%d (Space pause / B boredom +25 / Esc quit)",
                args.brain, args.tick_seconds, args.seed)
    if args.brain == "demo":
        LOGGER.info("DEMO uses deterministic offline rules; no language model is called.")
    try:
        if not args.headless:
            from renderer import IslandRenderer
            renderer = IslandRenderer(world)
        while running:
            frame_start = time.monotonic()
            if renderer is not None:
                for event in renderer.poll_events():
                    if event == "quit":
                        running = False
                    elif event == "pause":
                        paused = not paused
                        last_tick = frame_start
                        LOGGER.info("%s", "PAUSED" if paused else "RESUMED")
                    elif event == "boredom":
                        world.agent.boredom = min(100, world.agent.boredom + 25)
                        LOGGER.info("BOREDOM %d", world.agent.boredom)
            if not running:
                break
            brain_loop.update(simulation, frame_start, paused=paused)
            if not paused and frame_start - last_tick >= args.tick_seconds:
                simulation.advance_tick()
                # Schedule from now: a slow frame must not produce a burst of moves.
                last_tick = frame_start
                if not simulation.has_goal:
                    LOGGER.info("TICK %d IDLE boredom=%d energy=%d", simulation.tick_count,
                                world.agent.boredom, world.agent.energy)
                if args.ticks is not None and simulation.tick_count >= args.ticks:
                    running = False
            if renderer is not None:
                renderer.render(thought=simulation.internal_thought, path=simulation.path,
                                status=simulation.status, ticks=simulation.tick_count, paused=paused)
            # Headless mode shares the same nonblocking scheduling, at lower overhead.
            frame_budget = min(1.0 / 30, args.tick_seconds)
            time.sleep(max(0.0, frame_budget - (time.monotonic() - frame_start)))
    except KeyboardInterrupt:
        LOGGER.info("Interrupted; stopping the active Codex request.")
    finally:
        brain_loop.close()
        if renderer is not None:
            try:
                if args.screenshot:
                    renderer.save_screenshot(args.screenshot)
                    LOGGER.info("SCREENSHOT %s", args.screenshot)
            finally:
                renderer.close()
        LOGGER.info("STOP ticks=%d decisions=%d completed_plans=%d games=%d inventory=%s",
                    simulation.tick_count, brain_loop.decisions, simulation.decisions_completed,
                    simulation.games_completed, world.agent.inventory)
    return 1 if brain_loop.failures and not brain_loop.decisions else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    args = parse_args(argv)
    try:
        return run(args)
    except (BrainError, ImportError, OSError) as exc:
        LOGGER.error("STARTUP FAILED %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
