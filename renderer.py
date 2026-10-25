"""Pygame view for IslandWorld; simulation and model calls live elsewhere."""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Sequence

from island_world import Coordinate, IslandWorld, Rock, Tree, Water

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
try:
    import pygame
except ImportError as exc:
    raise ImportError(
        "The window needs pygame. Run: python -m pip install -r requirements.txt "
        "or start main.py with --headless."
    ) from exc


class IslandRenderer:
    """Render 32-pixel island tiles and a fixed, readable thought/status panel.

    Call this view from the main thread. Set SDL_VIDEODRIVER=dummy before
    constructing it to render or save screenshots without opening a window.
    """

    HUD_HEIGHT = 210
    FOOTER_HEIGHT = 40
    COLORS = {
        "background": (18, 28, 37),
        "panel": (27, 41, 52),
        "text": (232, 239, 235),
        "muted": (155, 180, 181),
        "grass": (81, 132, 85),
        "grass_alt": (85, 138, 89),
        "sand": (221, 199, 143),
        "sand_alt": (226, 205, 150),
        "water": (51, 105, 136),
        "water_light": (70, 131, 158),
        "tree": (35, 86, 57),
        "tree_light": (53, 111, 67),
        "trunk": (111, 77, 49),
        "rock": (131, 148, 151),
        "rock_light": (178, 190, 185),
        "agent": (245, 141, 104),
        "path": (250, 219, 112),
        "bar_track": (48, 65, 74),
        "calm": (121, 201, 163),
        "bored": (246, 173, 91),
    }

    def __init__(self, world: IslandWorld, tile_size: int = 32) -> None:
        if not isinstance(tile_size, int) or isinstance(tile_size, bool) or tile_size < 16:
            raise ValueError("tile_size must be an integer of at least 16 pixels.")
        self.world = world
        self.tile_size = tile_size
        self.width = world.SIZE * tile_size
        self.height = self.HUD_HEIGHT + world.SIZE * tile_size + self.FOOTER_HEIGHT
        pygame.display.init()
        pygame.font.init()
        self.surface = pygame.display.set_mode((self.width, self.height))
        pygame.display.set_caption("IslandWorld | Codex Agent")
        font_path = pygame.font.match_font(
            ("microsoftjhenghei", "microsoftyahei", "notosanscjk", "arialunicode", "dejavusans")
        )
        self.font = pygame.font.Font(font_path, 16)
        self.small_font = pygame.font.Font(font_path, 13)
        self.title_font = pygame.font.Font(font_path, 22)
        self._closed = False

    def poll_events(self) -> list[str]:
        """Return input intents; the main loop decides how to change state."""
        intents: list[str] = []
        if self._closed:
            return ["quit"]
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                intents.append("quit")
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    intents.append("quit")
                elif event.key == pygame.K_SPACE:
                    intents.append("pause")
                elif event.key == pygame.K_b:
                    intents.append("boredom")
        return intents

    def render(
        self,
        *,
        thought: str,
        path: Sequence[Coordinate],
        status: str = "",
        ticks: int = 0,
        paused: bool = False,
    ) -> None:
        """Draw one frame, including a clamped bar but the exact boredom value."""
        if self._closed:
            return
        self.surface.fill(self.COLORS["background"])
        self._draw_map()
        self._draw_path(path)
        self._draw_agent()
        self._draw_hud(thought, status, ticks, paused)
        self._draw_footer()
        if paused:
            overlay = pygame.Surface((self.width, self.world.SIZE * self.tile_size), pygame.SRCALPHA)
            overlay.fill((18, 28, 37, 90))
            self.surface.blit(overlay, (0, self.HUD_HEIGHT))
            label = self.title_font.render("PAUSED  /  Space to continue", True, self.COLORS["text"])
            label_rect = label.get_rect(center=(self.width // 2, self.HUD_HEIGHT + 38))
            pygame.draw.rect(self.surface, self.COLORS["panel"], label_rect.inflate(24, 16), border_radius=8)
            self.surface.blit(label, label_rect)
        pygame.display.flip()

    def save_screenshot(self, path: str | Path) -> None:
        """Save the current frame as PNG (or another pygame-supported format)."""
        if self._closed:
            raise RuntimeError("Cannot save a screenshot after closing the renderer.")
        pygame.image.save(self.surface, str(path))

    def close(self) -> None:
        if not self._closed:
            pygame.display.quit()
            self._closed = True

    def _tile_rect(self, x: int, y: int) -> pygame.Rect:
        return pygame.Rect(x * self.tile_size, self.HUD_HEIGHT + y * self.tile_size, self.tile_size, self.tile_size)

    def _draw_map(self) -> None:
        size = self.tile_size
        for y, row in enumerate(self.world.grid):
            for x, entity in enumerate(row):
                rect = self._tile_rect(x, y)
                alternate = (x + y) % 2
                beach = x in (1, self.world.SIZE - 2) or y in (1, self.world.SIZE - 2)
                if isinstance(entity, Water):
                    pygame.draw.rect(self.surface, self.COLORS["water"], rect)
                    pygame.draw.line(self.surface, self.COLORS["water_light"], (rect.x + 7, rect.y + size // 2), (rect.right - 5, rect.y + size // 2), 2)
                    continue
                base = "sand" if beach else "grass"
                pygame.draw.rect(self.surface, self.COLORS[base + ("_alt" if alternate else "")], rect)
                if isinstance(entity, Tree):
                    pygame.draw.rect(self.surface, self.COLORS["trunk"], (rect.x + size * 3 // 8, rect.y + size // 2, size // 4, size * 3 // 8))
                    pygame.draw.rect(self.surface, self.COLORS["tree"], rect.inflate(-size // 4, -size // 3).move(0, -size // 8), border_radius=3)
                    pygame.draw.rect(self.surface, self.COLORS["tree_light"], (rect.x + size // 4, rect.y + size // 5, size // 3, size // 4), border_radius=2)
                elif isinstance(entity, Rock):
                    rock_rect = rect.inflate(-size // 4, -size // 3).move(0, size // 10)
                    pygame.draw.rect(self.surface, self.COLORS["rock"], rock_rect, border_radius=5)
                    pygame.draw.line(self.surface, self.COLORS["rock_light"], (rock_rect.x + 5, rock_rect.y + 4), (rock_rect.right - 5, rock_rect.y + 4), 3)

    def _draw_path(self, path: Sequence[Coordinate]) -> None:
        valid_path = [point for point in path if self.world.in_bounds(point)]
        if not valid_path:
            return
        points = [self._tile_rect(self.world.agent.x, self.world.agent.y).center]
        points.extend(self._tile_rect(x, y).center for x, y in valid_path)
        pygame.draw.lines(self.surface, self.COLORS["path"], False, points, 3)
        for center in points[1:]:
            pygame.draw.circle(self.surface, self.COLORS["path"], center, 3)
        pygame.draw.rect(self.surface, self.COLORS["path"], self._tile_rect(*valid_path[-1]).inflate(-6, -6), 2, border_radius=3)

    def _draw_agent(self) -> None:
        rect = self._tile_rect(self.world.agent.x, self.world.agent.y)
        body = rect.inflate(-10, -8)
        pygame.draw.rect(self.surface, self.COLORS["background"], body.move(1, 3), border_radius=5)
        pygame.draw.rect(self.surface, self.COLORS["agent"], body, border_radius=5)
        eye_y = body.y + body.height // 3
        pygame.draw.rect(self.surface, self.COLORS["background"], (body.x + body.width // 4, eye_y, 3, 3))
        pygame.draw.rect(self.surface, self.COLORS["background"], (body.right - body.width // 4 - 3, eye_y, 3, 3))
        pygame.draw.line(self.surface, self.COLORS["background"], (body.centerx - 3, body.bottom - 6), (body.centerx + 3, body.bottom - 6), 2)

    def _draw_hud(self, thought: str, status: str, ticks: int, paused: bool) -> None:
        pygame.draw.rect(self.surface, self.COLORS["panel"], (0, 0, self.width, self.HUD_HEIGHT))
        self._text("ISLAND / 01", 16, 8, font=self.title_font)
        tick_text = f"{'PAUSED  |  ' if paused else ''}TICK {ticks:04d}"
        self._text(tick_text, self.width - self.small_font.size(tick_text)[0] - 16, 17, font=self.small_font, color="muted")
        agent = self.world.agent
        self._text(f"ENERGY {agent.energy:3d}    WOOD {agent.inventory.get('wood', 0)}    STONE {agent.inventory.get('stone', 0)}", 16, 42, font=self.small_font, color="muted")
        boredom = agent.boredom
        self._text(f"BOREDOM {boredom}", 16, 65, font=self.small_font)
        bar = pygame.Rect(142, 69, max(8, self.width - 158), 10)
        pygame.draw.rect(self.surface, self.COLORS["bar_track"], bar, border_radius=5)
        fraction = max(0.0, min(1.0, boredom / 100))
        if fraction:
            fill = bar.copy()
            fill.width = max(1, round(bar.width * fraction))
            pygame.draw.rect(self.surface, self.COLORS["bored" if boredom > 70 else "calm"], fill, border_radius=5)
        self._text("INTERNAL_THOUGHT / 角色心聲", 16, 91, font=self.small_font, color="muted")
        lines = self._wrap(thought or "等待下一個念頭…", self.font, self.width - 32, 3)
        for index, line in enumerate(lines):
            self._text(line, 16, 114 + index * 22)
        self._text(self._ellipsize(status or "Ready", self.small_font, self.width - 32), 16, 186, font=self.small_font, color="muted")

    def _draw_footer(self) -> None:
        top = self.HUD_HEIGHT + self.world.SIZE * self.tile_size
        self._text("Space pause   B boredom +25   Esc quit", 16, top + 5, font=self.small_font, color="muted")
        legend = "Grass / Beach / Tree / Rock    Yellow = path"
        self._text(self._ellipsize(legend, self.small_font, self.width - 32), 16, top + 22, font=self.small_font, color="muted")

    def _text(self, text: str, x: int, y: int, *, font: pygame.font.Font | None = None, color: str = "text") -> None:
        label = (font or self.font).render(text, True, self.COLORS[color])
        self.surface.blit(label, (x, y))

    @staticmethod
    def _ellipsize(text: str, font: pygame.font.Font, width: int) -> str:
        if font.size(text)[0] <= width:
            return text
        while text and font.size(text + "…")[0] > width:
            text = text[:-1]
        return text + "…"

    @classmethod
    def _wrap(cls, text: str, font: pygame.font.Font, width: int, max_lines: int) -> list[str]:
        # Wrapping by character works for Chinese and does not lose long English tokens.
        lines: list[str] = []
        current = ""
        text = text.replace("\r", "")
        for index, char in enumerate(text):
            if char == "\n" or (current and font.size(current + char)[0] > width):
                lines.append(current)
                current = "" if char == "\n" else char
                if len(lines) == max_lines:
                    if current or index < len(text) - 1:
                        lines[-1] = cls._ellipsize(lines[-1] + "…", font, width)
                    return lines
            else:
                current += char
        if current or not lines:
            lines.append(current)
        return lines
