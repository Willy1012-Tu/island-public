"""Exercise the real SDL dummy display without opening a desktop window."""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pytest

pygame = pytest.importorskip("pygame")

from island_world import IslandWorld
from renderer import IslandRenderer


def test_draw_chinese_thought_path_and_save_real_png(tmp_path):
    world = IslandWorld(tree_count=0, rock_count=0, seed=3)
    world.agent.x, world.agent.y = 3, 3
    world.agent.boredom = 135
    view = IslandRenderer(world)
    try:
        view.render(
            thought="我發明了一個沙灘尋寶接力遊戲：走向下一棵樹，再收集一塊木頭。" * 4,
            path=[(4, 3), (5, 3), (5, 4)],
            status="Playing: 沙灘尋寶接力",
            ticks=42,
        )
        screenshot = tmp_path / "island.png"
        view.save_screenshot(screenshot)
        image = pygame.image.load(str(screenshot))
        assert image.get_size() == (640, 890)
        assert screenshot.read_bytes().startswith(b"\x89PNG")
        assert tuple(image.get_at((2, view.HUD_HEIGHT + 2)))[:3] == view.COLORS["water"]
        assert tuple(image.get_at((4 * 32 + 16, view.HUD_HEIGHT + 3 * 32 + 16)))[:3] == view.COLORS["path"]
    finally:
        view.close()


def test_keyboard_and_window_events_are_intents_only():
    world = IslandWorld(seed=1)
    view = IslandRenderer(world)
    try:
        pygame.event.clear()
        for key in (pygame.K_SPACE, pygame.K_b, pygame.K_ESCAPE):
            pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=key))
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        assert view.poll_events() == ["pause", "boredom", "quit", "quit"]
        assert world.agent.boredom == 0
    finally:
        view.close()
    assert view.poll_events() == ["quit"]


def test_thought_wrap_never_exceeds_panel_or_three_lines():
    view = IslandRenderer(IslandWorld(seed=1))
    try:
        lines = view._wrap("沿著沙灘走一圈，看看樹木和石頭。" * 30, view.font, 608, 3)
        assert len(lines) == 3
        assert lines[-1].endswith("…")
        assert all(view.font.size(line)[0] <= 608 for line in lines)
    finally:
        view.close()
