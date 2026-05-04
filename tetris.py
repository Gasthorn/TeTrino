"""
Tetris - Projet BITalino
========================
Jeu Tetris fonctionnel en Pygame.
Les contrôles sont centralisés dans la classe InputHandler
pour faciliter l'intégration des capteurs BITalino.

Contrôles clavier (par défaut) :
  ← →     : Déplacer la pièce
  ↑        : Rotation
  ↓        : Descente rapide (soft drop)
  Espace   : Chute instantanée (hard drop)
  P        : Pause
  R        : Recommencer
"""

import pygame
import random
import sys
import time

# ─────────────────────────────────────────────
#  CONSTANTES
# ─────────────────────────────────────────────
CELL      = 32          # taille d'une cellule en pixels
COLS      = 10
ROWS      = 20
PREVIEW   = 5           # lignes de zone cachée en haut

PANEL_W   = 180         # largeur du panneau latéral
WIN_W     = COLS * CELL + PANEL_W
WIN_H     = ROWS * CELL

FPS       = 60

# Couleurs (palette rétro-arcade)
BLACK      = (  8,   8,  18)
DARK_GRAY  = ( 25,  25,  40)
GRAY       = ( 70,  70,  90)
WHITE      = (230, 230, 240)
CYAN       = ( 50, 220, 220)
YELLOW     = (240, 210,  30)
PURPLE     = (180,  60, 210)
GREEN      = ( 50, 200,  80)
RED        = (220,  50,  60)
BLUE       = ( 50,  90, 220)
ORANGE     = (220, 130,  30)

# Tetrominoes : forme + couleur
PIECES = {
    'I': {'shape': [[1,1,1,1]],                          'color': CYAN},
    'O': {'shape': [[1,1],[1,1]],                         'color': YELLOW},
    'T': {'shape': [[0,1,0],[1,1,1]],                     'color': PURPLE},
    'S': {'shape': [[0,1,1],[1,1,0]],                     'color': GREEN},
    'Z': {'shape': [[1,1,0],[0,1,1]],                     'color': RED},
    'J': {'shape': [[1,0,0],[1,1,1]],                     'color': BLUE},
    'L': {'shape': [[0,0,1],[1,1,1]],                     'color': ORANGE},
}

# Score par nombre de lignes effacées simultanément
LINE_SCORES = {1: 100, 2: 300, 3: 500, 4: 800}

# Vitesse de descente (ms entre chaque drop automatique) par niveau
def drop_interval(level: int) -> int:
    return max(100, 800 - (level - 1) * 70)


# ─────────────────────────────────────────────
#  UTILITAIRES
# ─────────────────────────────────────────────
def rotate_matrix(matrix):
    """Rotation 90° sens horaire."""
    return [list(row) for row in zip(*matrix[::-1])]


def draw_cell(surface, x, y, color, cell_size=CELL, alpha=255):
    """Dessine une cellule avec effet 3-D léger."""
    rect = pygame.Rect(x * cell_size, y * cell_size, cell_size - 1, cell_size - 1)
    s = pygame.Surface((cell_size - 1, cell_size - 1), pygame.SRCALPHA)
    s.fill((*color, alpha))
    # Reflet haut-gauche
    highlight = tuple(min(255, c + 60) for c in color)
    pygame.draw.line(s, (*highlight, alpha), (0, 0), (cell_size - 2, 0))
    pygame.draw.line(s, (*highlight, alpha), (0, 0), (0, cell_size - 2))
    # Ombre bas-droite
    shadow = tuple(max(0, c - 60) for c in color)
    pygame.draw.line(s, (*shadow, alpha), (cell_size - 2, 0), (cell_size - 2, cell_size - 2))
    pygame.draw.line(s, (*shadow, alpha), (0, cell_size - 2), (cell_size - 2, cell_size - 2))
    surface.blit(s, rect)


# ─────────────────────────────────────────────
#  PIECE
# ─────────────────────────────────────────────
class Piece:
    def __init__(self, name=None):
        self.name   = name or random.choice(list(PIECES.keys()))
        self.color  = PIECES[self.name]['color']
        self.shape  = [row[:] for row in PIECES[self.name]['shape']]
        self.x      = COLS // 2 - len(self.shape[0]) // 2
        self.y      = 0

    def rotated(self):
        return rotate_matrix(self.shape)

    def cells(self, shape=None, dx=0, dy=0):
        s = shape or self.shape
        return [(self.x + c + dx, self.y + r + dy)
                for r, row in enumerate(s)
                for c, v in enumerate(row) if v]


# ─────────────────────────────────────────────
#  GRILLE
# ─────────────────────────────────────────────
class Grid:
    def __init__(self):
        self.cells = [[None] * COLS for _ in range(ROWS)]

    def is_valid(self, cells):
        for x, y in cells:
            if x < 0 or x >= COLS or y >= ROWS:
                return False
            if y >= 0 and self.cells[y][x] is not None:
                return False
        return True

    def lock(self, piece):
        for x, y in piece.cells():
            if 0 <= y < ROWS and 0 <= x < COLS:
                self.cells[y][x] = piece.color

    def clear_lines(self):
        full = [i for i, row in enumerate(self.cells) if all(c is not None for c in row)]
        for i in full:
            del self.cells[i]
            self.cells.insert(0, [None] * COLS)
        return len(full)

    def draw(self, surface):
        # Fond de grille
        for r in range(ROWS):
            for c in range(COLS):
                rect = pygame.Rect(c * CELL, r * CELL, CELL - 1, CELL - 1)
                pygame.draw.rect(surface, DARK_GRAY, rect)
        # Cellules verrouillées
        for r in range(ROWS):
            for c in range(COLS):
                color = self.cells[r][c]
                if color:
                    draw_cell(surface, c, r, color)

    def is_game_over(self):
        return any(self.cells[0][c] is not None for c in range(COLS))


# ─────────────────────────────────────────────
#  INPUT HANDLER  (point d'entrée BITalino)
# ─────────────────────────────────────────────
class InputHandler:
    """
    Centralise tous les contrôles du jeu.
    Pour intégrer BITalino : remplacez / complétez les méthodes
    get_* par les lectures de vos capteurs.

    Exemple d'intégration future :
        def get_move(self):
            emg_left  = bitalino.read_channel(0)
            emg_right = bitalino.read_channel(1)
            if emg_left  > THRESHOLD: return -1
            if emg_right > THRESHOLD: return  1
            return 0
    """

    def __init__(self):
        self._keys = {}

    def update(self, events):
        """Appeler une fois par frame avec la liste des events pygame."""
        self._events = events
        self._keys   = pygame.key.get_pressed()

    # ── Actions ponctuelles (appui unique) ──────────────────────────
    def action_rotate(self) -> bool:
        return any(e.type == pygame.KEYDOWN and e.key == pygame.K_UP
                   for e in self._events)

    def action_hard_drop(self) -> bool:
        return any(e.type == pygame.KEYDOWN and e.key == pygame.K_SPACE
                   for e in self._events)

    def action_pause(self) -> bool:
        return any(e.type == pygame.KEYDOWN and e.key == pygame.K_p
                   for e in self._events)

    def action_restart(self) -> bool:
        return any(e.type == pygame.KEYDOWN and e.key == pygame.K_r
                   for e in self._events)

    # ── Actions continues (maintien touche) ──────────────────────────
    def get_move(self) -> int:
        """Retourne -1 (gauche), +1 (droite), 0 (rien)."""
        if self._keys[pygame.K_LEFT]:  return -1
        if self._keys[pygame.K_RIGHT]: return  1
        return 0

    def get_soft_drop(self) -> bool:
        """True si la descente rapide est demandée."""
        return bool(self._keys[pygame.K_DOWN])


# ─────────────────────────────────────────────
#  JEU PRINCIPAL
# ─────────────────────────────────────────────
class Tetris:
    def __init__(self, screen):
        self.screen  = screen
        self.handler = InputHandler()
        self.font_big   = pygame.font.SysFont('Courier', 28, bold=True)
        self.font_med   = pygame.font.SysFont('Courier', 18, bold=True)
        self.font_small = pygame.font.SysFont('Courier', 14)
        self.reset()

    def reset(self):
        self.grid        = Grid()
        self.current     = Piece()
        self.next_piece  = Piece()
        self.score       = 0
        self.lines       = 0
        self.level       = 1
        self.game_over   = False
        self.paused      = False

        self._drop_timer    = 0
        self._move_timer    = 0
        self._move_delay    = 150   # ms avant répétition
        self._move_repeat   = 50    # ms entre répétitions
        self._last_move_dir = 0
        self._move_held_ms  = 0

    # ── Logique ──────────────────────────────────────────────────────
    def _ghost_y(self):
        dy = 0
        while self.grid.is_valid(self.current.cells(dy=dy + 1)):
            dy += 1
        return dy

    def _lock_piece(self):
        self.grid.lock(self.current)
        cleared = self.grid.clear_lines()
        if cleared:
            self.score += LINE_SCORES.get(cleared, 0) * self.level
            self.lines += cleared
            self.level  = self.lines // 10 + 1
        self.current    = self.next_piece
        self.next_piece = Piece()
        if self.grid.is_game_over():
            self.game_over = True

    def _try_move(self, dx=0, dy=0, shape=None):
        cells = self.current.cells(shape=shape, dx=dx, dy=dy)
        if self.grid.is_valid(cells):
            if shape:
                self.current.shape = shape
            self.current.x += dx
            self.current.y += dy
            return True
        return False

    def update(self, dt_ms, events):
        self.handler.update(events)

        if self.handler.action_restart():
            self.reset(); return
        if self.handler.action_pause():
            self.paused = not self.paused
        if self.paused or self.game_over:
            return

        # Rotation
        if self.handler.action_rotate():
            self._try_move(shape=self.current.rotated())

        # Hard drop
        if self.handler.action_hard_drop():
            dy = self._ghost_y()
            self.current.y += dy
            self._lock_piece()
            return

        # Déplacement latéral avec répétition DAS
        move = self.handler.get_move()
        if move != 0:
            if move != self._last_move_dir:
                self._last_move_dir = move
                self._move_held_ms  = 0
                self._try_move(dx=move)
            else:
                self._move_held_ms += dt_ms
                delay = self._move_delay if self._move_held_ms < self._move_delay else self._move_repeat
                self._move_timer += dt_ms
                if self._move_timer >= delay:
                    self._move_timer = 0
                    self._try_move(dx=move)
        else:
            self._last_move_dir = 0
            self._move_held_ms  = 0
            self._move_timer    = 0

        # Descente automatique / soft drop
        interval = 50 if self.handler.get_soft_drop() else drop_interval(self.level)
        self._drop_timer += dt_ms
        if self._drop_timer >= interval:
            self._drop_timer = 0
            if not self._try_move(dy=1):
                self._lock_piece()

    # ── Rendu ─────────────────────────────────────────────────────────
    def draw(self):
        self.screen.fill(BLACK)

        # Surface de la grille
        grid_surf = pygame.Surface((COLS * CELL, ROWS * CELL))
        grid_surf.fill(BLACK)
        self.grid.draw(grid_surf)

        # Ghost piece
        ghost_dy = self._ghost_y()
        if ghost_dy > 0:
            for x, y in self.current.cells(dy=ghost_dy):
                if 0 <= y < ROWS:
                    draw_cell(grid_surf, x, y, self.current.color, alpha=50)

        # Pièce courante
        for x, y in self.current.cells():
            if 0 <= y < ROWS:
                draw_cell(grid_surf, x, y, self.current.color)

        self.screen.blit(grid_surf, (0, 0))

        # Bordure grille
        pygame.draw.rect(self.screen, GRAY, (0, 0, COLS * CELL, ROWS * CELL), 2)

        # Panneau latéral
        px = COLS * CELL + 10
        self._draw_panel(px)

        # Superposition pause / game over
        if self.paused:
            self._overlay("PAUSE", "P pour reprendre")
        elif self.game_over:
            self._overlay("GAME OVER", f"Score : {self.score}  |  R pour rejouer")

        pygame.display.flip()

    def _draw_panel(self, px):
        py = 10
        # Titre
        t = self.font_big.render("TETRIS", True, WHITE)
        self.screen.blit(t, (px, py)); py += 40

        # Prochaine pièce
        self._label(px, py, "SUIVANT"); py += 22
        self._draw_preview(px, py); py += 90

        # Stats
        for label, value in [("SCORE", self.score), ("LIGNES", self.lines), ("NIVEAU", self.level)]:
            self._label(px, py, label); py += 20
            v = self.font_med.render(str(value), True, CYAN)
            self.screen.blit(v, (px, py)); py += 30

        # Contrôles
        py += 10
        self._label(px, py, "CONTRÔLES"); py += 20
        controls = ["← →  Déplacer", "↑    Rotation", "↓    Soft drop",
                    "SPC  Hard drop", "P    Pause", "R    Restart"]
        for c in controls:
            t = self.font_small.render(c, True, GRAY)
            self.screen.blit(t, (px, py)); py += 16

    def _label(self, x, y, text):
        t = self.font_small.render(text, True, GRAY)
        self.screen.blit(t, (x, y))

    def _draw_preview(self, px, py):
        bg = pygame.Rect(px, py, PANEL_W - 15, 80)
        pygame.draw.rect(self.screen, DARK_GRAY, bg, border_radius=4)
        shape  = self.next_piece.shape
        color  = self.next_piece.color
        cell   = 20
        ox = px + (PANEL_W - 15 - len(shape[0]) * cell) // 2
        oy = py + (80 - len(shape) * cell) // 2
        for r, row in enumerate(shape):
            for c, v in enumerate(row):
                if v:
                    rect = pygame.Rect(ox + c * cell, oy + r * cell, cell - 1, cell - 1)
                    pygame.draw.rect(self.screen, color, rect, border_radius=2)

    def _overlay(self, title, subtitle):
        overlay = pygame.Surface((COLS * CELL, ROWS * CELL), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 160))
        self.screen.blit(overlay, (0, 0))
        t1 = self.font_big.render(title, True, WHITE)
        t2 = self.font_small.render(subtitle, True, GRAY)
        cx = COLS * CELL // 2
        self.screen.blit(t1, t1.get_rect(center=(cx, ROWS * CELL // 2 - 20)))
        self.screen.blit(t2, t2.get_rect(center=(cx, ROWS * CELL // 2 + 20)))


# ─────────────────────────────────────────────
#  POINT D'ENTRÉE
# ─────────────────────────────────────────────
def main():
    pygame.init()
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    pygame.display.set_caption("Tetris – Projet BITalino")
    clock  = pygame.time.Clock()
    game   = Tetris(screen)

    while True:
        dt     = clock.tick(FPS)
        events = pygame.event.get()

        for e in events:
            if e.type == pygame.QUIT:
                pygame.quit(); sys.exit()

        game.update(dt, events)
        game.draw()


if __name__ == '__main__':
    main()
