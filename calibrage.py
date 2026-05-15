"""
Calibrage accéléromètre BITalino — Interface CRT responsive
============================================================

Écran de calibration façon oscilloscope de laboratoire (CRT phosphor).

Le capteur cardiaque (PPG, oreille) est branché DÈS LE DÉBUT et reste
branché. Pas de phase « rythme cardiaque » dédiée.

Phases :
  0. DÉTECTION  : tente d'ouvrir la carte BITalino.
                  En cas d'échec → bouton RÉESSAYER + bouton MODE DÉMO.
  1. REPOS      : accéléromètre tenu immobile. Lignes accéléro = plates ;
                  la seule courbe qui bouge encore = le pouls → port PPG
                  isolé automatiquement (périodicité), + baseline accéléro.
  2. G ↔ D      : axe X = port avec la plus forte variance (vs repos).
  3. H ↕ B      : axe Y = port restant avec la plus forte variance.
  4. CŒUR       : calibration dédiée du BPM de repos sur le port PPG
                  déjà isolé (fenêtre longue, immobile, retry si instable ;
                  détection de repli si REPOS n'a pas isolé le pouls).
  5. ZONE MORTE : seuil 0..1 (slider) + radar XY + BPM live affiché.

Les 3 ports accéléromètre sont identifiés AUTOMATIQUEMENT (moyenne+σ
pendant REPOS, en excluant le port PPG, puis variance pendant G/D et H/B).
Sortie : calibration.json.

Fenêtre redimensionnable (drag du coin) — l'interface se réorganise.

Lancement :
    python calibrage.py [adresse]
    python calibrage.py --demo            (sans BITalino, données simulées)
"""

import json
import math
import platform
import random
import statistics
import sys
import threading
import time
from collections import deque

import pygame

# ─────────────────────────────────────────────
#  Configuration de l'API PLUX
# ─────────────────────────────────────────────
osDic = {
    "Darwin": f"MacOS/Intel{''.join(platform.python_version().split('.')[:2])}",
    "Linux": "Linux64",
    "Windows": f"Win{platform.architecture()[0][:2]}_{''.join(platform.python_version().split('.')[:2])}",
}
if platform.mac_ver()[0] != "":
    import subprocess
    from os import linesep
    p = subprocess.Popen("sw_vers", stdout=subprocess.PIPE)
    result = p.communicate()[0].decode("utf-8").split(str("\t"))[2].split(linesep)[0]
    if result.startswith("12."):
        osDic["Darwin"] = "MacOS/Intel310"

sys.path.append(f"PLUX-API-Python3/{osDic.get(platform.system(), '')}")

try:
    import plux
    PLUX_AVAILABLE = True
except Exception:
    PLUX_AVAILABLE = False


# ─────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────
INITIAL_W, INITIAL_H = 1280, 720
MIN_W,     MIN_H     = 1100, 720
FPS                  = 60
DEFAULT_ADDR         = "BTH98:D3:51:FE:87:0E"
SAMPLING_HZ          = 1000
ALL_PORTS            = [1, 2, 3, 4, 5, 6]
REST_SECONDS         = 8.0   # assez long pour isoler le pouls (≥ ~8 battements)
MOVE_SECONDS         = 5.0
HR_SECONDS           = 15.0  # calibration cardiaque dédiée : BPM de repos fiable
PPG_MIN_BPM          = 40
PPG_MAX_BPM          = 180
PPG_MIN_SCORE        = 1.0    # score mini de périodicité pour valider un PPG

# ─────────────────────────────────────────────
#  Palette  —  TETRIS MODERN
#  Matrice indigo profond, tuiles néon biseautées.
# ─────────────────────────────────────────────
BG_DEEP      = (  7,   8,  20)   # fond matrice (presque noir indigo)
BG_PANEL     = ( 15,  16,  34)   # puits / panneau
BG_PANEL_HI  = ( 26,  27,  54)   # surface surélevée / hover
GRID_DIM     = ( 22,  23,  46)   # lignes de matrice faibles
GRID_HI      = ( 42,  44,  84)   # lignes de matrice fortes
PHOSPHOR     = (  0, 238, 255)   # I — cyan, accent primaire
PHOSPHOR_MID = (  0, 230, 130)   # S — vert, succès / terminé
PHOSPHOR_DIM = ( 24,  70,  96)   # bord cyan atténué
AMBER        = (255, 178,  36)   # L — orange, enregistrement / actif
AMBER_DIM    = (104,  70,  16)
TEXT_HI      = (238, 241, 255)
TEXT_MID     = (152, 160, 205)
TEXT_DIM     = ( 99, 106, 154)
TEXT_FAINT   = ( 56,  60,  98)
DANGER       = (255,  58,  96)   # Z — rouge, pouls / erreur
DANGER_DIM   = (110,  22,  42)

# 7 couleurs de tétrominos. 6 premières = mapping des 6 ports BITalino.
TETRO = {
    "I": (  0, 238, 255), "O": (255, 209,  38), "T": (180,  86, 255),
    "S": (  0, 230, 130), "Z": (255,  58,  96), "J": ( 46, 122, 255),
    "L": (255, 148,  28),
}
PORT_COLORS = [TETRO["I"], TETRO["O"], TETRO["T"],
               TETRO["S"], TETRO["Z"], TETRO["J"]]

# Formes de tétrominos (offsets de cellules) — décor de fond.
TETRO_SHAPES = {
    "I": [(0, 0), (1, 0), (2, 0), (3, 0)],
    "O": [(0, 0), (1, 0), (0, 1), (1, 1)],
    "T": [(0, 0), (1, 0), (2, 0), (1, 1)],
    "S": [(1, 0), (2, 0), (0, 1), (1, 1)],
    "Z": [(0, 0), (1, 0), (1, 1), (2, 1)],
    "J": [(0, 0), (0, 1), (1, 1), (2, 1)],
    "L": [(2, 0), (0, 1), (1, 1), (2, 1)],
}


def _lighten(c, f):
    return tuple(min(255, int(v + (255 - v) * f)) for v in c[:3])


def _darken(c, f):
    return tuple(max(0, int(v * (1 - f))) for v in c[:3])


# ─────────────────────────────────────────────
#  Devices
# ─────────────────────────────────────────────
if PLUX_AVAILABLE:
    class CalibrationDevice(plux.SignalsDev):
        def __init__(self, address):
            plux.SignalsDev.__init__(address)
            self.frequency  = SAMPLING_HZ
            self.lock       = threading.Lock()
            self.stop_flag  = False
            self.latest     = (0,) * 6
            self.recording  = False
            self.recorded   = []
            self.live_buf   = [deque([512] * 720, maxlen=720) for _ in ALL_PORTS]
            self._tick      = 0

        def onRawFrame(self, nSeq, data):
            sample = tuple(int(v) for v in data[:6])
            with self.lock:
                self.latest = sample
                if self.recording:
                    self.recorded.append(sample)
                self._tick += 1
                if self._tick % 8 == 0:
                    for i, v in enumerate(sample):
                        self.live_buf[i].append(v)
            return self.stop_flag


class SimulatedDevice:
    def __init__(self):
        self.frequency = SAMPLING_HZ
        self.lock      = threading.Lock()
        self.stop_flag = False
        self.latest    = (512,) * 6
        self.recording = False
        self.recorded  = []
        self.live_buf  = [deque([512] * 720, maxlen=720) for _ in ALL_PORTS]
        self._t        = 0
        self._mode     = "rest"

    def loop(self):
        rng = random.Random(7)
        while not self.stop_flag:
            self._t += 1
            t = self._t / 120.0
            x = 512 + int(2 * math.sin(t * 13) + rng.uniform(-2, 2))
            y = 510 + int(2 * math.cos(t * 11) + rng.uniform(-2, 2))
            z = 780 + int(1.5 * math.sin(t *  7) + rng.uniform(-2, 2))
            if self._mode == "lr":
                x = int(512 + 220 * math.sin(t * 3) + rng.uniform(-3, 3))
            elif self._mode == "ud":
                y = int(510 + 200 * math.sin(t * 2.4) + rng.uniform(-3, 3))
            elif self._mode == "radar":
                x = int(512 + 180 * math.sin(t * 1.7))
                y = int(510 + 140 * math.cos(t * 1.3))
            # PPG simulé sur le port 4 (index 3), ~72 BPM (1.2 Hz).
            # Toujours actif : il "bouge encore" même accéléromètre immobile.
            ppg = int(400 + 80 * math.sin(t * 0.905) + rng.uniform(-6, 6))
            sample = (x, y, z, ppg, 64, 900)
            with self.lock:
                self.latest = sample
                if self.recording:
                    self.recorded.append(sample)
                if self._t % 8 == 0:
                    for i, v in enumerate(sample):
                        self.live_buf[i].append(v)
            time.sleep(1.0 / self.frequency)

    def start(self, *_a, **_kw):
        pass

    def stop(self):
        self.stop_flag = True

    def close(self):
        pass

    def set_mode(self, mode):
        self._mode = mode


# ─────────────────────────────────────────────
#  Détection automatique des ports
# ─────────────────────────────────────────────
def _accel_candidates(samples, exclude=()):
    """Ports plausibles pour un axe accéléromètre : moyenne dans 180..840 et
    σ faible (immobile = courbe plate). `exclude` retire le port PPG déjà
    identifié — le pouls ne doit jamais être pris pour un axe."""
    cands = []
    for i in range(6):
        if i in exclude:
            continue
        col = [s[i] for s in samples]
        m = statistics.mean(col)
        sd = statistics.pstdev(col) if len(col) > 1 else 0
        if 180 < m < 840 and sd < 20:
            cands.append(i)
    return cands or [i for i in range(6) if i not in exclude]


def _max_delta_std(rest, active, candidates):
    if not candidates:
        return None
    def sd(samples, idx):
        col = [s[idx] for s in samples]
        return statistics.pstdev(col) if len(col) > 1 else 0
    return max(candidates, key=lambda c: sd(active, c) - sd(rest, c))


def detect_x_axis(rest_samples, lr_samples, exclude=()):
    return _max_delta_std(rest_samples, lr_samples,
                          _accel_candidates(rest_samples, exclude))


def detect_y_axis(rest_samples, ud_samples, exclude=()):
    return _max_delta_std(rest_samples, ud_samples,
                          _accel_candidates(rest_samples, exclude))


def detect_z_axis(rest_samples, exclude=()):
    cands = _accel_candidates(rest_samples, exclude)
    if not cands:
        return None
    means = {c: statistics.mean(s[c] for s in rest_samples) for c in cands}
    return max(cands, key=lambda c: abs(means[c] - 512))


# ─────────────────────────────────────────────
#  Détection PPG / rythme cardiaque
# ─────────────────────────────────────────────
def _smooth(data, window=50):
    half = window // 2
    n = len(data)
    result = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        result.append(sum(data[lo:hi]) / (hi - lo))
    return result


def _estimate_bpm_and_score(col, frequency):
    """Peak-detection BPM from a raw PPG column. Returns (bpm, quality_score)."""
    if len(col) < frequency:
        return 0, 0.0
    smoothed = _smooth(col, window=max(10, frequency // 20))
    mean_v = statistics.mean(smoothed)
    std_v  = statistics.pstdev(smoothed) if len(smoothed) > 1 else 0
    if std_v < 5:
        return 0, 0.0
    threshold = mean_v + 0.35 * std_v
    min_gap = int(frequency * 60 / 180)   # fastest plausible: 180 BPM
    max_gap = int(frequency * 60 / 30)    # slowest plausible:  30 BPM
    peaks = []
    i = 1
    while i < len(smoothed) - 1:
        if (smoothed[i] >= threshold
                and smoothed[i] >= smoothed[i - 1]
                and smoothed[i] >= smoothed[i + 1]):
            if not peaks or (i - peaks[-1]) >= min_gap:
                peaks.append(i)
        i += 1
    if len(peaks) < 3:
        return 0, 0.0
    intervals = [peaks[k + 1] - peaks[k] for k in range(len(peaks) - 1)]
    valid = [iv for iv in intervals if min_gap <= iv <= max_gap]
    if len(valid) < 2:
        return 0, 0.0
    mean_iv = statistics.mean(valid)
    bpm = int(round(60 * frequency / mean_iv))
    cv = statistics.pstdev(valid) / mean_iv if mean_iv > 0 else 1
    score = len(valid) / (1 + cv * 8)
    return bpm, score


def detect_ppg_from_rest(rest_samples, frequency=SAMPLING_HZ):
    """Le capteur cardiaque (oreille) est branché dès le départ. Quand
    l'accéléromètre est tenu immobile, c'est le SEUL signal qui « bouge
    encore » : on cherche donc le port le plus périodique (meilleur score
    de détection de pic) parmi les 6. Retourne (port, bpm) ou (None, 0)."""
    best_port, best_bpm, best_score = None, 0, PPG_MIN_SCORE
    for port in range(6):
        col = [s[port] for s in rest_samples]
        bpm, score = _estimate_bpm_and_score(col, frequency)
        if score > best_score and PPG_MIN_BPM <= bpm <= PPG_MAX_BPM:
            best_score, best_port, best_bpm = score, port, bpm
    return best_port, best_bpm


def detect_ppg_port(hr_samples, exclude_ports, frequency=SAMPLING_HZ):
    """Repli pour la phase cardiaque dédiée : meilleur port PPG parmi les
    ports non-axes. Retourne (port, bpm) ou (None, 0)."""
    candidates = [i for i in range(6) if i not in exclude_ports]
    best_port, best_bpm, best_score = None, 0, PPG_MIN_SCORE
    for port in candidates:
        col = [s[port] for s in hr_samples]
        bpm, score = _estimate_bpm_and_score(col, frequency)
        if score > best_score and PPG_MIN_BPM <= bpm <= PPG_MAX_BPM:
            best_score, best_port, best_bpm = score, port, bpm
    return best_port, best_bpm


# ─────────────────────────────────────────────
#  Polices
# ─────────────────────────────────────────────
class Theme:
    """Polices proportionnelles à la fenêtre.
    DISPLAY = sans condensé géométrique (titres, façon HUD Tetris moderne).
    MONO    = chasse fixe (données, logs, valeurs)."""
    DISPLAY = "bahnschrift,segoeuisemibold,franklingothicmedium,impact,arialblack"
    MONO    = "cascadiamono,consolas,couriernew,monospace"

    def __init__(self, h):
        self.update(h)

    def update(self, h):
        # Ratios calibrés sur 950px de hauteur
        s = max(0.70, min(1.6, h / 950))
        D = lambda px: pygame.font.SysFont(self.DISPLAY, int(px * s), bold=True)
        M = lambda px, b=False: pygame.font.SysFont(self.MONO, int(px * s),
                                                    bold=b)
        self.f_huge   = D(78)
        self.f_xl     = D(46)
        self.f_big    = D(34)
        self.f_med    = M(21)
        self.f_med_b  = D(22)
        self.f_small  = M(17)
        self.f_tiny   = M(13)
        self.f_micro  = M(11)


# ─────────────────────────────────────────────
#  Tuile tétromino — motif central du design
# ─────────────────────────────────────────────
def draw_block(surf, rect, color, alpha=255, gloss=True, inset=0):
    """Tuile Tetris moderne : face mate, biseau clair haut/gauche,
    biseau sombre bas/droite, reflet en haut. Brique de toute l'UI."""
    r = pygame.Rect(rect)
    if inset:
        r = r.inflate(-2 * inset, -2 * inset)
    if r.w <= 2 or r.h <= 2:
        return
    b = max(2, min(r.w, r.h) // 9)
    light = _lighten(color, 0.50)
    dark  = _darken(color, 0.45)
    face  = _darken(color, 0.10)

    if alpha >= 255:
        pygame.draw.rect(surf, face, r)
        pygame.draw.polygon(surf, light, [
            r.topleft, r.topright, (r.right - b, r.top + b),
            (r.left + b, r.top + b)])
        pygame.draw.polygon(surf, light, [
            r.topleft, (r.left + b, r.top + b),
            (r.left + b, r.bottom - b), r.bottomleft])
        pygame.draw.polygon(surf, dark, [
            r.bottomleft, (r.left + b, r.bottom - b),
            (r.right - b, r.bottom - b), r.bottomright])
        pygame.draw.polygon(surf, dark, [
            r.topright, (r.right - b, r.top + b),
            (r.right - b, r.bottom - b), r.bottomright])
        if gloss:
            gl = pygame.Surface((r.w, r.h), pygame.SRCALPHA)
            pygame.draw.rect(gl, (255, 255, 255, 28),
                             pygame.Rect(b, b, r.w - 2 * b,
                                         max(1, (r.h - 2 * b) // 3)))
            surf.blit(gl, r.topleft)
        pygame.draw.rect(surf, _darken(color, 0.62), r, 1)
    else:
        tmp = pygame.Surface((r.w, r.h), pygame.SRCALPHA)
        lr = pygame.Rect(0, 0, r.w, r.h)
        pygame.draw.rect(tmp, (*face, alpha), lr)
        pygame.draw.rect(tmp, (*light, alpha), lr, b)
        pygame.draw.rect(tmp, (*_darken(color, 0.62), alpha), lr, 1)
        surf.blit(tmp, r.topleft)


def draw_tetromino(surf, x, y, cell, shape, color, alpha=255, gap=2):
    for (cx, cy) in TETRO_SHAPES[shape]:
        draw_block(surf,
                   pygame.Rect(x + cx * cell, y + cy * cell,
                               cell - gap, cell - gap),
                   color, alpha=alpha)


# ─────────────────────────────────────────────
#  Pré-rendus (rebuild si la taille change)
# ─────────────────────────────────────────────
def make_grid(w, h, step=40):
    """Fond opaque : dégradé indigo vertical + matrice + tétrominos
    fantômes géants en filigrane."""
    s = pygame.Surface((w, h)).convert()
    top, bot = (10, 11, 26), (5, 5, 15)
    for yy in range(h):
        f = yy / max(1, h - 1)
        s.fill((int(top[0] + (bot[0] - top[0]) * f),
                int(top[1] + (bot[1] - top[1]) * f),
                int(top[2] + (bot[2] - top[2]) * f)),
               pygame.Rect(0, yy, w, 1))
    rng = random.Random(42)
    ghosts = pygame.Surface((w, h), pygame.SRCALPHA)
    for _ in range(max(4, w // 360)):
        shp = rng.choice(list(TETRO_SHAPES))
        cell = rng.randint(46, 96)
        gx = rng.randint(-cell, w)
        gy = rng.randint(-cell, h)
        for (cx, cy) in TETRO_SHAPES[shp]:
            pygame.draw.rect(ghosts, (*TETRO[shp], 14),
                             pygame.Rect(gx + cx * cell, gy + cy * cell,
                                         cell - 4, cell - 4), 2)
    s.blit(ghosts, (0, 0))
    grid = pygame.Surface((w, h), pygame.SRCALPHA)
    for x in range(0, w, step):
        c = GRID_HI if (x // step) % 4 == 0 else GRID_DIM
        pygame.draw.line(grid, (*c, 90), (x, 0), (x, h))
    for y in range(0, h, step):
        c = GRID_HI if (y // step) % 4 == 0 else GRID_DIM
        pygame.draw.line(grid, (*c, 90), (0, y), (w, y))
    s.blit(grid, (0, 0))
    return s


def make_scanlines(w, h, alpha=22, spacing=3):
    """Bloom doux cyan en haut (remplace les scanlines CRT)."""
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    glow_h = h // 3
    for yy in range(glow_h):
        a = int(26 * (1 - yy / glow_h))
        if a > 0:
            pygame.draw.line(s, (*PHOSPHOR, a), (0, yy), (w, yy))
    return s


def make_vignette(w, h):
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    layers = max(24, min(70, w // 36))
    for i in range(layers):
        a = int(3 + i * 1.5)
        pygame.draw.rect(overlay, (0, 0, 6, a),
                         pygame.Rect(i, i, w - 2 * i, h - 2 * i), 1)
    return overlay


# ─────────────────────────────────────────────
#  Helpers de dessin
# ─────────────────────────────────────────────
def draw_text(surf, font, text, pos, color=TEXT_HI, glow=None):
    if glow is not None:
        g = font.render(text, True, glow)
        g.set_alpha(90)
        for off in [(-2, 0), (2, 0), (0, -2), (0, 2), (-3, 0), (3, 0)]:
            surf.blit(g, (pos[0] + off[0], pos[1] + off[1]))
    t = font.render(text, True, color)
    surf.blit(t, pos)
    return t.get_rect(topleft=pos)


def draw_text_centered(surf, font, text, center, color=TEXT_HI, glow=None):
    t = font.render(text, True, color)
    rect = t.get_rect(center=center)
    if glow is not None:
        g = font.render(text, True, glow)
        g.set_alpha(80)
        for off in [(-2, 0), (2, 0), (0, -2), (0, 2), (-3, 0), (3, 0)]:
            surf.blit(g, (rect.x + off[0], rect.y + off[1]))
    surf.blit(t, rect)
    return rect


def draw_corner_brackets(surf, rect, color=PHOSPHOR_DIM, length=18, width=3):
    """Coins en équerre façon pièce de Tetris (encoche carrée)."""
    x, y, w, h = rect
    L = length
    for (cx, cy, dx, dy) in (
        (x, y, 1, 1), (x + w, y, -1, 1),
        (x, y + h, 1, -1), (x + w, y + h, -1, -1),
    ):
        pygame.draw.line(surf, color, (cx, cy), (cx + dx * L, cy), width)
        pygame.draw.line(surf, color, (cx, cy), (cx, cy + dy * L), width)


def draw_panel(surf, rect, fill=BG_PANEL, border=PHOSPHOR_DIM, accent=None):
    """Puits sombre : liseré, barre d'accent supérieure, coins encochés."""
    r = pygame.Rect(rect)
    pygame.draw.rect(surf, fill, r, border_radius=3)
    pygame.draw.rect(surf, border, r, 1, border_radius=3)
    if accent is not None:
        bar = pygame.Surface((r.w - 4, 3), pygame.SRCALPHA)
        bar.fill((*accent, 150))
        surf.blit(bar, (r.x + 2, r.y + 2))
    draw_corner_brackets(surf, r, color=border, length=16, width=3)


def draw_scope(surf, rect, buffers, highlight=None, axis_labels=None, theme=None):
    """Oscilloscope sur matrice Tetris : signaux en couleurs de pièces,
    lueur sur la courbe mise en avant."""
    pygame.draw.rect(surf, (10, 11, 24), rect, border_radius=3)
    cell = max(18, rect.width // 28)
    for x in range(rect.left, rect.right, cell):
        pygame.draw.line(surf, GRID_DIM, (x, rect.top), (x, rect.bottom))
    for y in range(rect.top, rect.bottom, cell):
        pygame.draw.line(surf, GRID_DIM, (rect.left, y), (rect.right, y))
    cy = rect.centery
    pygame.draw.line(surf, GRID_HI, (rect.left, cy), (rect.right, cy), 1)
    pygame.draw.rect(surf, PHOSPHOR_DIM, rect, 1, border_radius=3)
    draw_corner_brackets(surf, rect, color=PHOSPHOR_DIM, length=14, width=3)

    n = max(1, len(buffers[0]))
    step = rect.width / n
    for i, buf in enumerate(buffers):
        color = PORT_COLORS[i % len(PORT_COLORS)]
        is_hi = (highlight is None) or (i == highlight)
        if not is_hi:
            color = _darken(color, 0.74)
        pts = []
        for k, v in enumerate(buf):
            x = rect.left + int(k * step)
            y = int(cy - (v - 512) * (rect.height / 2) / 512)
            y = max(rect.top + 1, min(rect.bottom - 1, y))
            pts.append((x, y))
        if len(pts) > 1:
            try:
                if is_hi and highlight is not None:
                    glow = pygame.Surface(rect.size, pygame.SRCALPHA)
                    gp = [(px - rect.left, py - rect.top) for px, py in pts]
                    pygame.draw.lines(glow, (*color, 60), False, gp, 7)
                    surf.blit(glow, rect.topleft)
                pygame.draw.aalines(surf, color, False, pts)
                if is_hi:
                    pygame.draw.lines(surf, color, False, pts, 2)
            except ValueError:
                pass

    if theme is not None and axis_labels:
        chip = max(12, cell // 2)
        x = rect.right - 96
        y = rect.top + 10
        for i, lab in enumerate(axis_labels):
            color = PORT_COLORS[i % len(PORT_COLORS)]
            is_hi = (highlight is None) or (i == highlight)
            c = color if is_hi else _darken(color, 0.62)
            draw_block(surf, pygame.Rect(x, y, chip, chip), c)
            t = theme.f_tiny.render(lab, True, c if is_hi else TEXT_DIM)
            surf.blit(t, (x + chip + 6, y + (chip - t.get_height()) // 2))
            y += chip + 5


def draw_radar(surf, rect, x_norm, y_norm, dead_zone, theme):
    """Matrice carrée centrée : zone morte = carré néon (bloc),
    pièce-curseur biseautée, flèches actives en surbrillance."""
    cx, cy = rect.center
    r = min(rect.w, rect.h) // 2 - 30
    field = pygame.Rect(cx - r, cy - r, 2 * r, 2 * r)
    pygame.draw.rect(surf, (10, 11, 24), field, border_radius=2)
    divs = 8
    for k in range(divs + 1):
        gx = field.left + k * (2 * r) // divs
        gy = field.top + k * (2 * r) // divs
        pygame.draw.line(surf, GRID_DIM, (gx, field.top), (gx, field.bottom))
        pygame.draw.line(surf, GRID_DIM, (field.left, gy), (field.right, gy))
    pygame.draw.line(surf, GRID_HI, (cx - r, cy), (cx + r, cy), 1)
    pygame.draw.line(surf, GRID_HI, (cx, cy - r), (cx, cy + r), 1)
    pygame.draw.rect(surf, PHOSPHOR_DIM, field, 1, border_radius=2)
    draw_corner_brackets(surf, field, color=PHOSPHOR_DIM, length=14, width=3)

    dz = max(4, int(r * dead_zone))
    dz_surf = pygame.Surface((2 * dz, 2 * dz), pygame.SRCALPHA)
    pygame.draw.rect(dz_surf, (*AMBER, 34), dz_surf.get_rect(), border_radius=4)
    pygame.draw.rect(dz_surf, (*AMBER, 210), dz_surf.get_rect(), 2,
                     border_radius=4)
    surf.blit(dz_surf, (cx - dz, cy - dz))

    in_dead = math.hypot(x_norm, y_norm) <= dead_zone
    hot = lambda active: PHOSPHOR if active else TEXT_FAINT
    draw_text_centered(surf, theme.f_big, "◄", (cx - r - 30, cy),
                       hot((not in_dead) and x_norm < -dead_zone))
    draw_text_centered(surf, theme.f_big, "►", (cx + r + 30, cy),
                       hot((not in_dead) and x_norm > dead_zone))
    draw_text_centered(surf, theme.f_big, "▲", (cx, cy - r - 30),
                       hot((not in_dead) and y_norm > dead_zone))
    draw_text_centered(surf, theme.f_big, "▼", (cx, cy + r + 30),
                       hot((not in_dead) and y_norm < -dead_zone))

    px = cx + int(max(-1, min(1, x_norm)) * r)
    py = cy - int(max(-1, min(1, y_norm)) * r)
    pc = AMBER if in_dead else PHOSPHOR
    halo = pygame.Surface((52, 52), pygame.SRCALPHA)
    pygame.draw.circle(halo, (*pc, 55), (26, 26), 24)
    surf.blit(halo, (px - 26, py - 26))
    bs = max(12, r // 9)
    draw_block(surf, pygame.Rect(px - bs // 2, py - bs // 2, bs, bs), pc)


# ─────────────────────────────────────────────
#  Composants
# ─────────────────────────────────────────────
class Button:
    def __init__(self, label, accent=PHOSPHOR, hot_key=None):
        self.label = label
        self.accent = accent
        self.hot_key = hot_key
        self.rect = pygame.Rect(0, 0, 100, 40)
        self.hover = False
        self.pressed = False
        self.enabled = True
        self._pulse = 0.0

    def update(self, mouse_pos, events):
        self.hover = self.enabled and self.rect.collidepoint(mouse_pos)
        clicked = False
        for e in events:
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.hover:
                self.pressed = True
            if e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                if self.pressed and self.hover and self.enabled:
                    clicked = True
                    self._pulse = 1.0
                self.pressed = False
            if e.type == pygame.KEYDOWN and self.enabled:
                if self.hot_key is not None and e.key == self.hot_key:
                    clicked = True
                    self._pulse = 1.0
        self._pulse = max(0.0, self._pulse - 0.04)
        return clicked

    def draw(self, surf, font, t_anim):
        breathe = 0.5 + 0.5 * math.sin(t_anim * 2.4)
        a = self.accent
        r = pygame.Rect(self.rect)
        if not self.enabled:
            # Tuile éteinte : plate, terne.
            pygame.draw.rect(surf, BG_PANEL, r, border_radius=3)
            pygame.draw.rect(surf, TEXT_FAINT, r, 1, border_radius=3)
            t = font.render(self.label, True, TEXT_FAINT)
            surf.blit(t, t.get_rect(center=r.center))
            return
        # Halo extérieur (hover / impulsion clic)
        if self.hover or self._pulse > 0:
            inten = breathe if self.hover else self._pulse
            glow = pygame.Surface((r.w + 24, r.h + 24), pygame.SRCALPHA)
            pygame.draw.rect(glow, (*a, int(70 * inten)),
                             glow.get_rect(), border_radius=8)
            surf.blit(glow, (r.x - 12, r.y - 12))
        # Effet d'enfoncement au clic
        if self.pressed:
            r = r.move(0, 2)
            tile = _darken(a, 0.18)
        elif self.hover:
            tile = _lighten(a, 0.12)
        else:
            tile = a
        draw_block(surf, r, tile)
        txt_c = _darken(a, 0.78)
        t = font.render(self.label, True, txt_c)
        surf.blit(t, t.get_rect(center=r.center))


class Slider:
    def __init__(self, value=0.3, vmin=0.0, vmax=1.0, accent=AMBER):
        self.value = value
        self.vmin = vmin
        self.vmax = vmax
        self.accent = accent
        self.rect = pygame.Rect(0, 0, 100, 6)
        self.drag = False

    def update(self, mouse_pos, events):
        for e in events:
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                if self.rect.inflate(0, 30).collidepoint(mouse_pos):
                    self.drag = True
            if e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                self.drag = False
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_LEFT:
                    self.value = max(self.vmin, self.value - 0.01)
                elif e.key == pygame.K_RIGHT:
                    self.value = min(self.vmax, self.value + 0.01)
        if self.drag and self.rect.width > 0:
            x = max(self.rect.left, min(self.rect.right, mouse_pos[0]))
            self.value = self.vmin + (self.vmax - self.vmin) * \
                         (x - self.rect.left) / self.rect.width

    def draw(self, surf, font_value, font_label):
        cy = self.rect.centery
        ratio = (self.value - self.vmin) / (self.vmax - self.vmin)
        # Rail creux
        groove = pygame.Rect(self.rect.left, cy - 7, self.rect.width, 14)
        pygame.draw.rect(surf, (8, 9, 20), groove, border_radius=4)
        pygame.draw.rect(surf, PHOSPHOR_DIM, groove, 1, border_radius=4)
        # Remplissage en briques (clear de ligne Tetris)
        seg = max(10, self.rect.width // 24)
        filled_w = int(self.rect.width * ratio)
        bx = self.rect.left
        while bx < self.rect.left + filled_w - 2:
            draw_block(surf,
                       pygame.Rect(bx + 1, cy - 6,
                                   min(seg - 2, self.rect.left + filled_w - bx),
                                   12),
                       self.accent)
            bx += seg
        # Graduations 0..1
        for i in range(11):
            tx = self.rect.left + int(self.rect.width * i / 10)
            h = 11 if i % 5 == 0 else 5
            pygame.draw.line(surf, TEXT_DIM, (tx, cy - h), (tx, cy + h), 1)
            if i % 5 == 0:
                lab = f"{i / 10:.1f}"
                tl = font_label.render(lab, True, TEXT_DIM)
                surf.blit(tl, (tx - tl.get_width() // 2, cy + 16))
        # Poignée = tuile
        hx = self.rect.left + filled_w
        draw_block(surf, pygame.Rect(hx - 12, cy - 20, 24, 40),
                   _lighten(self.accent, 0.10))
        # Valeur (néon)
        val_str = f"{self.value:.2f}"
        t = font_value.render(val_str, True, self.accent)
        gx = hx - t.get_width() // 2
        gy = cy - 24 - t.get_height()
        g = font_value.render(val_str, True, self.accent)
        g.set_alpha(90)
        for off in [(-2, 0), (2, 0), (0, -2), (0, 2)]:
            surf.blit(g, (gx + off[0], gy + off[1]))
        surf.blit(t, (gx, gy))


# ─────────────────────────────────────────────
#  Layout responsive
# ─────────────────────────────────────────────
class Layout:
    def __init__(self, w, h):
        self.compute(w, h)

    def compute(self, w, h):
        self.w = w
        self.h = h
        self.margin   = max(20, int(w * 0.022))
        self.header_h = max(54, int(h * 0.062))
        self.title_h  = max(120, int(h * 0.16))
        self.side_w   = max(330, int(w * 0.235))
        self.log_h    = max(170, int(h * 0.21))

        self.header = pygame.Rect(0, 0, w, self.header_h)
        self.title  = pygame.Rect(self.margin,
                                  self.header_h + max(20, int(h * 0.025)),
                                  w - 2 * self.margin,
                                  self.title_h)
        self.side   = pygame.Rect(self.margin,
                                  self.title.bottom + 10,
                                  self.side_w,
                                  h - self.title.bottom - self.log_h - self.margin - 20)
        self.log    = pygame.Rect(self.margin,
                                  h - self.log_h - self.margin,
                                  self.side_w,
                                  self.log_h)
        main_x = self.side.right + self.margin
        self.main   = pygame.Rect(main_x,
                                  self.title.bottom + 10,
                                  w - main_x - self.margin,
                                  h - self.title.bottom - self.margin - 20)


# ─────────────────────────────────────────────
#  États
# ─────────────────────────────────────────────
STATE_DETECT   = "detect"
STATE_INTRO    = "intro"
STATE_REST     = "rest"
STATE_LR       = "lr"
STATE_UD       = "ud"
STATE_HR       = "hr"
STATE_DEADZONE = "dead"
STATE_DONE     = "done"


# ─────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────
class App:
    def __init__(self, screen, address):
        self.screen   = screen
        self.address  = address
        w, h = screen.get_size()
        self.layout   = Layout(w, h)
        self.theme    = Theme(h)
        self.clock    = pygame.time.Clock()
        self._cached_size = (w, h)
        self._rebuild_overlays()
        self.t0       = time.time()

        self.state    = STATE_DETECT
        self.recording_until = 0.0

        # Détection
        self.detect_status = "idle"   # idle / scanning / ok / fail
        self.detect_error  = ""
        self.detect_started_at = 0.0
        self.device   = None
        self.acq_thread = None
        self._demo    = False

        # Données
        self.rest_samples = []
        self.lr_samples   = []
        self.ud_samples   = []
        self.hr_samples   = []
        self.x_axis = self.y_axis = self.z_axis = None
        self.ppg_port = None
        self.bpm_rest = 0
        self._bpm_live   = 0
        self._bpm_live_t = 0.0
        self.x_min  = self.x_max  = 0
        self.y_min  = self.y_max  = 0

        # Composants
        self.btn_main        = Button("[  OK  ]", accent=PHOSPHOR, hot_key=pygame.K_RETURN)
        self.btn_retry       = Button("[  RÉESSAYER  ]", accent=AMBER, hot_key=pygame.K_r)
        self.btn_demo        = Button("[  MODE DÉMO  ]", accent=PHOSPHOR_MID, hot_key=pygame.K_d)
        self.btn_recalibrate = Button("[  RECOMMENCER LE CALIBRAGE  ]", accent=DANGER)
        self.slider          = Slider(value=0.3, accent=AMBER)

        self.log_lines = deque(maxlen=10)
        self._log("SYSTEM BOOT ............................. OK")
        self._log("AWAITING DEVICE DETECTION...")
        self._start_detection()

    # ── Logs ───────────────────────────────────────────────────────
    def _log(self, msg):
        self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    # ── Rebuild des surfaces selon la taille ───────────────────────
    def _rebuild_overlays(self):
        w, h = self.screen.get_size()
        self.bg_grid   = make_grid(w, h)
        self.scanlines = make_scanlines(w, h, alpha=22, spacing=3)
        self.vignette  = make_vignette(w, h)
        self._cached_size = (w, h)

    def _maybe_resize(self):
        if self.screen.get_size() != self._cached_size:
            w, h = self.screen.get_size()
            self.layout.compute(w, h)
            self.theme.update(h)
            self._rebuild_overlays()

    # ── Détection BITalino ─────────────────────────────────────────
    def _start_detection(self, force_demo=False):
        # Reset éventuelle connexion précédente
        if self.device is not None:
            try:
                self.device.stop_flag = True
                self.device.stop()
                self.device.close()
            except Exception:
                pass
            self.device = None

        self._demo = force_demo
        self.detect_status = "scanning"
        self.detect_error  = ""
        self.detect_started_at = time.time()
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        # Petit délai mini pour que l'utilisateur voie la phase de scan
        target_min = self.detect_started_at + 1.4
        if self._demo:
            self._log("DEMO MODE ENABLED — using simulated sensor data")
            d = SimulatedDevice()
            self.acq_thread = threading.Thread(target=d.loop, daemon=True)
            self.acq_thread.start()
            self.device = d
            time.sleep(max(0, target_min - time.time()))
            self.detect_status = "ok"
            self._log(f"DEVICE READY  → SIMULATED  @ {SAMPLING_HZ} Hz")
            return
        if not PLUX_AVAILABLE:
            time.sleep(max(0, target_min - time.time()))
            self.detect_status = "fail"
            self.detect_error  = "Module 'plux' introuvable (PLUX-API-Python3)"
            self._log("DETECTION FAILED — plux module not found")
            return
        try:
            self._log(f"PROBING {self.address} ...")
            d = CalibrationDevice(self.address)
            d.frequency = SAMPLING_HZ
            d.start(d.frequency, ALL_PORTS, 16)
            self.acq_thread = threading.Thread(target=d.loop, daemon=True)
            self.acq_thread.start()
            self.device = d
            time.sleep(max(0, target_min - time.time()))
            self.detect_status = "ok"
            self._log(f"DEVICE READY  → BITALINO @ {SAMPLING_HZ} Hz")
        except Exception as exc:
            time.sleep(max(0, target_min - time.time()))
            self.detect_status = "fail"
            self.detect_error  = str(exc)
            self._log(f"DETECTION FAILED — {exc}")

    # ── Acquisition control ────────────────────────────────────────
    def _start_recording(self, seconds):
        with self.device.lock:
            self.device.recorded = []
            self.device.recording = True
        self.recording_until = time.time() + seconds

    def _stop_recording(self):
        with self.device.lock:
            self.device.recording = False
            return list(self.device.recorded)

    # ── Boucle ─────────────────────────────────────────────────────
    def run(self):
        running = True
        while running:
            self.clock.tick(FPS)
            t_now = time.time() - self.t0
            mouse = pygame.mouse.get_pos()
            events = pygame.event.get()
            for e in events:
                if e.type == pygame.QUIT:
                    running = False
                elif e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                    running = False
                elif e.type == pygame.VIDEORESIZE:
                    new_w = max(MIN_W, e.w)
                    new_h = max(MIN_H, e.h)
                    self.screen = pygame.display.set_mode(
                        (new_w, new_h), pygame.RESIZABLE | pygame.DOUBLEBUF
                    )
            self._maybe_resize()
            self._update(t_now, mouse, events)
            self._draw(t_now)
            pygame.display.flip()
        return self.state == STATE_DONE

    # ── Update ─────────────────────────────────────────────────────
    def _update(self, t, mouse, events):
        if self.state == STATE_DETECT:
            if self.detect_status == "ok":
                self.state = STATE_INTRO
                return
            if self.detect_status == "fail":
                if self.btn_retry.update(mouse, events):
                    self._log("RETRYING DETECTION...")
                    self._start_detection(force_demo=False)
                if self.btn_demo.update(mouse, events):
                    self._start_detection(force_demo=True)
            return

        recording = self.recording_until > 0 and time.time() < self.recording_until
        if self.state in (STATE_INTRO, STATE_REST, STATE_LR, STATE_UD, STATE_HR):
            self.btn_main.enabled = not recording
            if self.btn_main.update(mouse, events):
                self._on_main_button()

        if self.state == STATE_DEADZONE:
            self.slider.update(mouse, events)
            self.btn_main.enabled = True
            if self.btn_main.update(mouse, events):
                self._save_and_finish()
            if self.btn_recalibrate.update(mouse, events):
                self._restart_calibration()

        if self.state == STATE_DONE:
            if self.btn_main.update(mouse, events):
                pygame.event.post(pygame.event.Event(pygame.QUIT))

        if self.recording_until > 0 and time.time() >= self.recording_until:
            self.recording_until = 0
            self._on_recording_done()

        if isinstance(self.device, SimulatedDevice):
            mode = {
                STATE_INTRO: "rest", STATE_REST: "rest",
                STATE_LR: "lr",      STATE_UD: "ud",
                STATE_HR: "rest",
                STATE_DEADZONE: "radar", STATE_DONE: "radar",
            }.get(self.state, "rest")
            self.device.set_mode(mode)

    def _on_main_button(self):
        if self.state == STATE_INTRO:
            self.state = STATE_REST
            self._log("STEP 01/04 — REST POSITION CALIBRATION")
            return
        if self.state == STATE_REST and self.recording_until == 0:
            self._log(f"RECORDING REST BASELINE ({REST_SECONDS:.1f} s)...")
            self._start_recording(REST_SECONDS)
            self.btn_main.label = "[  ENREGISTREMENT...  ]"
            return
        if self.state == STATE_LR and self.recording_until == 0:
            self._log(f"RECORDING LEFT/RIGHT MOTION ({MOVE_SECONDS:.1f} s)...")
            self._start_recording(MOVE_SECONDS)
            self.btn_main.label = "[  ENREGISTREMENT...  ]"
            return
        if self.state == STATE_UD and self.recording_until == 0:
            self._log(f"RECORDING UP/DOWN MOTION ({MOVE_SECONDS:.1f} s)...")
            self._start_recording(MOVE_SECONDS)
            self.btn_main.label = "[  ENREGISTREMENT...  ]"
            return
        if self.state == STATE_HR and self.recording_until == 0:
            self._log(f"RECORDING HEART RATE AT REST ({HR_SECONDS:.1f} s)...")
            self._start_recording(HR_SECONDS)
            self.btn_main.label = "[  ENREGISTREMENT...  ]"
            return

    def _on_recording_done(self):
        if self.state == STATE_REST:
            self.rest_samples = self._stop_recording()
            if not self.rest_samples:
                self._log("ERR: NO SAMPLES — CHECK BITALINO LINK")
                self.btn_main.label = "[  RÉESSAYER  ]"
                return
            # Accéléromètre immobile → seul le pouls (oreille) « bouge encore ».
            # On isole donc le port PPG par sa périodicité, avant les axes.
            freq = self.device.frequency if self.device is not None else SAMPLING_HZ
            port, bpm = detect_ppg_from_rest(self.rest_samples, frequency=freq)
            self.ppg_port = port
            self.bpm_rest = bpm
            if port is not None:
                self._log(f"PPG (CŒUR) → PORT {port + 1}   "
                          f"BPM REPOS = {bpm}")
            else:
                self._log("PPG NON DÉTECTÉ — VÉRIFIER LE CAPTEUR D'OREILLE")
            cands = _accel_candidates(self.rest_samples, self._ppg_excl())
            self._log(f"REPOS OK — CANDIDATS ACCEL: P{[c+1 for c in cands]}")
            self.state = STATE_LR
            self.btn_main.label = "[  OK, COMMENCER LE BALAYAGE G/D  ]"
            return
        if self.state == STATE_LR:
            self.lr_samples = self._stop_recording()
            self.x_axis = detect_x_axis(self.rest_samples, self.lr_samples,
                                        self._ppg_excl())
            xs = [s[self.x_axis] for s in self.lr_samples]
            self.x_min, self.x_max = min(xs), max(xs)
            self._log(f"X AXIS DETECTED → PORT {self.x_axis + 1}  "
                      f"[{self.x_min}..{self.x_max}]")
            self.state = STATE_UD
            self.btn_main.label = "[  OK, COMMENCER LE BALAYAGE H/B  ]"
            return
        if self.state == STATE_UD:
            self.ud_samples = self._stop_recording()
            self.y_axis = detect_y_axis(self.rest_samples, self.ud_samples,
                                        self._ppg_excl(self.x_axis))
            ys = [s[self.y_axis] for s in self.ud_samples]
            self.y_min, self.y_max = min(ys), max(ys)
            self.z_axis = detect_z_axis(self.rest_samples,
                                        self._ppg_excl(self.x_axis, self.y_axis))
            self._log(f"Y AXIS DETECTED → PORT {self.y_axis + 1}  "
                      f"[{self.y_min}..{self.y_max}]")
            if self.z_axis is not None:
                self._log(f"Z AXIS INFERRED  → PORT {self.z_axis + 1}")
            self.state = STATE_HR
            self.btn_main.label = "[  OK, CALIBRER LE POULS DE REPOS  ]"
            return
        if self.state == STATE_HR:
            self.hr_samples = self._stop_recording()
            if not self.hr_samples:
                self._log("ERR: NO SAMPLES — CHECK BITALINO LINK")
                self.btn_main.label = "[  RÉESSAYER L'ENREGISTREMENT  ]"
                return
            freq = self.device.frequency if self.device is not None else SAMPLING_HZ
            if self.ppg_port is not None:
                # Port PPG déjà isolé en phase REPOS : on raffine le BPM repos
                # sur une fenêtre plus longue (mesure dédiée, immobile).
                col = [s[self.ppg_port] for s in self.hr_samples]
                bpm, score = _estimate_bpm_and_score(col, freq)
                ok = (score > PPG_MIN_SCORE
                      and PPG_MIN_BPM <= bpm <= PPG_MAX_BPM)
                if not ok:
                    self._log("ERR: POULS INSTABLE — RESTEZ IMMOBILE, RÉESSAYEZ")
                    self.btn_main.label = "[  RÉESSAYER L'ENREGISTREMENT  ]"
                    return
            else:
                # Repli : REPOS n'a pas isolé le pouls → détection ici parmi
                # les ports non-axes.
                axis_excl = self._ppg_excl(self.x_axis, self.y_axis,
                                           self.z_axis)
                port, bpm = detect_ppg_port(self.hr_samples, axis_excl,
                                            frequency=freq)
                if port is None or bpm <= 0:
                    self._log("ERR: PPG NON DÉTECTÉ — VÉRIFIER CAPTEUR OREILLE")
                    self.btn_main.label = "[  RÉESSAYER L'ENREGISTREMENT  ]"
                    return
                self.ppg_port = port
            self.bpm_rest = bpm
            self._bpm_live = bpm
            self._log(f"CŒUR CALIBRÉ → PORT {self.ppg_port + 1}   "
                      f"BPM REPOS = {bpm}")
            self.state = STATE_DEADZONE
            self.btn_main.label = "[  VALIDER LA ZONE MORTE  ]"
            return

    def _ppg_excl(self, *extra):
        """Ports à exclure des axes : le port PPG + axes déjà trouvés."""
        return tuple(p for p in (self.ppg_port, *extra) if p is not None)

    def _restart_calibration(self):
        self.rest_samples = []
        self.lr_samples   = []
        self.ud_samples   = []
        self.hr_samples   = []
        self.x_axis = self.y_axis = self.z_axis = None
        self.ppg_port = None
        self.bpm_rest = 0
        self._bpm_live   = 0
        self._bpm_live_t = 0.0
        self.x_min  = self.x_max  = 0
        self.y_min  = self.y_max  = 0
        self.recording_until = 0.0
        self.slider.value = 0.3
        if self.device is not None:
            with self.device.lock:
                self.device.recorded  = []
                self.device.recording = False
        self.btn_main.label  = "[  OK, JE SUIS PRÊT  ]"
        self.btn_main.accent = PHOSPHOR
        self.state = STATE_REST
        self._log("RECALIBRATION REQUESTED — RESTARTING FROM STEP 01")

    def _save_and_finish(self):
        x_rest = statistics.mean(s[self.x_axis] for s in self.rest_samples)
        y_rest = statistics.mean(s[self.y_axis] for s in self.rest_samples)
        z_rest = (statistics.mean(s[self.z_axis] for s in self.rest_samples)
                  if self.z_axis is not None else 512)
        calib = {
            "address":   self.address if not self._demo else "SIMULATED",
            "frequency": SAMPLING_HZ,
            "ports": {
                "x": int(self.x_axis + 1),
                "y": int(self.y_axis + 1),
                "z": int(self.z_axis + 1) if self.z_axis is not None else None,
            },
            "rest":  {"x": x_rest, "y": y_rest, "z": z_rest},
            "range": {"x_min": int(self.x_min), "x_max": int(self.x_max),
                      "y_min": int(self.y_min), "y_max": int(self.y_max)},
            "dead_zone": round(self.slider.value, 3),
            "ppg": {
                "port": int(self.ppg_port + 1) if self.ppg_port is not None else None,
                "bpm_rest": int(self.bpm_rest),
            },
            "mapping": {
                "left":  "x < x_rest - dead_zone * (x_rest - x_min)",
                "right": "x > x_rest + dead_zone * (x_max - x_rest)",
                "up":    "y > y_rest + dead_zone * (y_max - y_rest)",
                "down":  "y < y_rest - dead_zone * (y_rest - y_min)",
            },
        }
        with open("calibration.json", "w", encoding="utf-8") as f:
            json.dump(calib, f, indent=2, ensure_ascii=False)
        self._log("CALIBRATION WRITTEN → calibration.json")
        self.state = STATE_DONE
        self.btn_main.label = "[  QUITTER  ]"

    # ── Rendu ──────────────────────────────────────────────────────
    def _draw(self, t):
        self.screen.fill(BG_DEEP)
        self.screen.blit(self.bg_grid, (0, 0))
        self._draw_drift(t)

        self._draw_header(t)
        self._draw_title(t)

        if self.state == STATE_DETECT:
            self._draw_detect(t)
        else:
            self._draw_step_track()
            self._draw_log_panel()
            if self.state == STATE_INTRO:
                self._draw_intro(t)
            elif self.state == STATE_REST:
                self._draw_rest(t)
            elif self.state in (STATE_LR, STATE_UD):
                self._draw_axis_step(t, "x" if self.state == STATE_LR else "y")
            elif self.state == STATE_HR:
                self._draw_hr(t)
            elif self.state == STATE_DEADZONE:
                self._draw_deadzone(t)
            elif self.state == STATE_DONE:
                self._draw_done(t)

        self.screen.blit(self.scanlines, (0, 0))
        self.screen.blit(self.vignette, (0, 0))

    def _draw_drift(self, t):
        """Tétrominos fantômes qui descendent lentement — ambiance matrice."""
        w, h = self.screen.get_size()
        if getattr(self, "_drift_sz", None) != (w, h):
            rng = random.Random(1991)
            keys = list(TETRO_SHAPES)
            self._drift = []
            for _ in range(max(7, w // 220)):
                k = rng.choice(keys)
                self._drift.append((
                    k, rng.randint(0, w), rng.uniform(7, 22),
                    rng.uniform(0, 1000), rng.randint(20, 40)))
            self._drift_sz = (w, h)
        layer = pygame.Surface((w, h), pygame.SRCALPHA)
        for k, x, spd, ph, cell in self._drift:
            shp = TETRO_SHAPES[k]
            span = (max(c[1] for c in shp) + 2) * cell
            y = int((ph + t * spd) % (h + span)) - span
            rot = int(t * 0.15 + ph) % 2
            for (cx, cy) in shp:
                bx = x + (cy if rot else cx) * cell
                by = y + (cx if rot else cy) * cell
                pygame.draw.rect(layer, (*TETRO[k], 16),
                                 pygame.Rect(bx, by, cell - 3, cell - 3), 2)
        self.screen.blit(layer, (0, 0))

    def _status(self):
        is_rec = self.recording_until > 0
        if self.state == STATE_DETECT:
            c = (AMBER if self.detect_status == "scanning" else
                 PHOSPHOR_MID if self.detect_status == "ok" else DANGER)
            return c, self.detect_status.upper()
        if is_rec:
            return DANGER, "REC"
        return PHOSPHOR_MID, "READY"

    def _draw_header(self, t):
        bar = self.layout.header
        pygame.draw.rect(self.screen, (12, 13, 28), bar)
        pygame.draw.line(self.screen, PHOSPHOR_DIM,
                         (0, bar.bottom), (bar.right, bar.bottom), 2)
        # Logo : mini pièce I
        m = self.layout.margin
        bs = max(7, bar.height // 6)
        for j in range(4):
            draw_block(self.screen,
                       pygame.Rect(bar.left + m + j * (bs + 2),
                                   bar.centery - bs // 2, bs, bs),
                       PHOSPHOR)
        draw_text(self.screen, self.theme.f_small,
                  "BITALINO  ·  CALIBRAGE ACCÉL + POULS  ·  v2.0",
                  (bar.left + m + 4 * (bs + 2) + 16, bar.centery - 9),
                  color=TEXT_MID)
        # Statut : chip bloc + libellé + horloge
        sc, sl = self._status()
        blink = sc if int(t * 2) % 2 == 0 else _darken(sc, 0.45)
        clock = time.strftime("%H:%M:%S")
        ts = self.theme.f_small.render(
            f"{sl}   {clock}   {self.layout.w}×{self.layout.h}", True, TEXT_MID)
        chip = max(10, bar.height // 5)
        cxr = bar.right - m - ts.get_width()
        draw_block(self.screen,
                   pygame.Rect(cxr - chip - 12, bar.centery - chip // 2,
                               chip, chip), blink)
        self.screen.blit(ts, (cxr, bar.centery - 9))

    def _draw_title(self, t):
        rect = self.layout.title
        draw_text(self.screen, self.theme.f_huge, "CALIBRAGE",
                  (rect.left, rect.top - 4), color=TEXT_HI, glow=PHOSPHOR)
        draw_text(self.screen, self.theme.f_small,
                  "ACCÉLÉROMÈTRE 3 AXES → ◄ ► ▲ ▼      CAPTEUR CARDIAQUE → BPM",
                  (rect.left + 4, rect.top + int(rect.height * 0.74)),
                  color=TEXT_DIM)
        # Frise de tuiles décorative en haut-droite
        order = ["I", "O", "T", "S", "Z", "J", "L"]
        bs = max(12, rect.height // 6)
        bx = rect.right - len(order) * (bs + 4)
        by = rect.top + 2
        for i, k in enumerate(order):
            off = int(3 * math.sin(t * 2 + i))
            draw_block(self.screen,
                       pygame.Rect(bx + i * (bs + 4), by + off, bs, bs),
                       TETRO[k])

    def _draw_step_track(self):
        rect = self.layout.side
        steps = [
            ("01", "REPOS + POULS",    STATE_REST,     "I"),
            ("02", "GAUCHE / DROITE",  STATE_LR,       "J"),
            ("03", "HAUT / BAS",       STATE_UD,       "L"),
            ("04", "RYTHME CARDIAQUE", STATE_HR,       "Z"),
            ("05", "ZONE MORTE",       STATE_DEADZONE, "T"),
        ]
        order = [STATE_INTRO, STATE_REST, STATE_LR, STATE_UD,
                 STATE_HR, STATE_DEADZONE, STATE_DONE]
        cur_idx = order.index(self.state) if self.state in order else 0
        draw_text(self.screen, self.theme.f_tiny, "// FILE D'ATTENTE",
                  (rect.left + 4, rect.top - 22), color=PHOSPHOR_MID)
        slot_h = rect.height // len(steps)
        pad = max(6, slot_h // 10)
        for i, (num, label, st, shp) in enumerate(steps):
            done = order.index(st) < cur_idx
            active = self.state == st
            slot = pygame.Rect(rect.left, rect.top + i * slot_h + pad,
                               rect.width, slot_h - 2 * pad)
            base = (PHOSPHOR if active else
                    PHOSPHOR_MID if done else TEXT_FAINT)
            fill = BG_PANEL_HI if active else BG_PANEL
            pygame.draw.rect(self.screen, fill, slot, border_radius=4)
            pygame.draw.rect(self.screen, base, slot, 2 if active else 1,
                             border_radius=4)
            if active:
                draw_corner_brackets(self.screen, slot, color=PHOSPHOR,
                                     length=12, width=3)
            gs = min(slot.height - 12, slot.width // 6)
            gx = slot.left + 14
            gy = slot.centery - gs // 2
            tcol = TETRO[shp]
            if active:
                draw_block(self.screen, pygame.Rect(gx, gy, gs, gs), tcol)
            elif done:
                draw_block(self.screen, pygame.Rect(gx, gy, gs, gs),
                           _darken(PHOSPHOR_MID, 0.25))
                pygame.draw.lines(self.screen, BG_DEEP, False,
                                  [(gx + gs * 0.22, gy + gs * 0.52),
                                   (gx + gs * 0.42, gy + gs * 0.72),
                                   (gx + gs * 0.80, gy + gs * 0.28)], 3)
            else:
                pygame.draw.rect(self.screen, _darken(tcol, 0.55),
                                 pygame.Rect(gx, gy, gs, gs), 2)
            tx = gx + gs + 16
            draw_text(self.screen, self.theme.f_tiny, f"ÉTAPE {num}",
                      (tx, slot.centery - 20),
                      color=base if not done else PHOSPHOR_MID)
            draw_text(self.screen,
                      self.theme.f_med_b if active else self.theme.f_small,
                      label, (tx, slot.centery - 2),
                      color=TEXT_HI if active else base,
                      glow=PHOSPHOR if active else None)

    def _draw_log_panel(self):
        rect = self.layout.log
        draw_panel(self.screen, rect, accent=PHOSPHOR)
        draw_text(self.screen, self.theme.f_tiny, "// CONSOLE",
                  (rect.left + 14, rect.top + 10), color=PHOSPHOR_MID)
        y = rect.top + 34
        lines = list(self.log_lines)
        max_lines = max(4, (rect.height - 44) // 20)
        shown = lines[-max_lines:]
        for idx, line in enumerate(shown):
            newest = idx == len(shown) - 1
            draw_text(self.screen, self.theme.f_tiny, f"› {line}",
                      (rect.left + 14, y),
                      color=PHOSPHOR if newest else TEXT_DIM)
            y += 20

    # ── Écran : Détection ──────────────────────────────────────────
    def _draw_detect(self, t):
        m = self.layout.margin
        rect = pygame.Rect(m, self.layout.title.bottom + 10,
                           self.layout.w - 2 * m,
                           self.layout.h - self.layout.title.bottom - m - 20)
        draw_panel(self.screen, rect, accent=PHOSPHOR)
        console_w = max(360, int(rect.width * 0.34))
        console = pygame.Rect(rect.right - console_w - 24, rect.top + 28,
                              console_w, rect.height - 56)
        pygame.draw.rect(self.screen, (10, 11, 24), console, border_radius=3)
        pygame.draw.rect(self.screen, PHOSPHOR_DIM, console, 1, border_radius=3)
        draw_text(self.screen, self.theme.f_tiny, "// JOURNAL",
                  (console.left + 14, console.top + 12), color=PHOSPHOR_MID)
        y = console.top + 38
        max_lines = max(6, (console.height - 54) // 20)
        shown = list(self.log_lines)[-max_lines:]
        for idx, line in enumerate(shown):
            newest = idx == len(shown) - 1
            draw_text(self.screen, self.theme.f_tiny, f"› {line}",
                      (console.left + 14, y),
                      color=PHOSPHOR if newest else TEXT_DIM)
            y += 20

        zone = pygame.Rect(rect.left + 30, rect.top + 30,
                           console.left - rect.left - 56,
                           rect.height - 60)
        if self.detect_status == "scanning":
            self._draw_scan_animation(zone, t)
        elif self.detect_status == "ok":
            for i in range(3):
                draw_block(self.screen,
                           pygame.Rect(zone.centerx - 60 + i * 44,
                                       zone.centery - 60, 38, 38),
                           PHOSPHOR_MID)
            draw_text_centered(self.screen, self.theme.f_huge, "DÉTECTÉ",
                               (zone.centerx, zone.centery + 10),
                               color=PHOSPHOR_MID, glow=PHOSPHOR_MID)
        elif self.detect_status == "fail":
            self._draw_detect_failure(zone, t)

    def _draw_scan_animation(self, zone, t):
        draw_text(self.screen, self.theme.f_xl, "DÉTECTION DU MATÉRIEL",
                  (zone.left, zone.top + 6), color=PHOSPHOR, glow=PHOSPHOR)
        draw_text(self.screen, self.theme.f_small,
                  f"Sondage de la liaison Bluetooth  ·  {self.address}",
                  (zone.left, zone.top + 70), color=TEXT_MID)
        # Ligne de tuiles qui se "remplit" puis se vide (clear de ligne)
        bs = max(22, zone.width // 16)
        cells = zone.width // (bs + 6)
        bar_y = zone.top + 130
        phase = (math.sin(t * 1.6) + 1) / 2
        lit = int(phase * cells)
        order = list(TETRO)
        for i in range(cells):
            bx = zone.left + i * (bs + 6)
            if i <= lit:
                draw_block(self.screen, pygame.Rect(bx, bar_y, bs, bs),
                           TETRO[order[i % len(order)]])
            else:
                pygame.draw.rect(self.screen, GRID_HI,
                                 pygame.Rect(bx, bar_y, bs, bs), 1,
                                 border_radius=3)
        dots = "." * (1 + int(t * 3) % 4)
        draw_text(self.screen, self.theme.f_med, f"SCAN EN COURS{dots}",
                  (zone.left, bar_y + bs + 24), color=AMBER)
        elapsed = time.time() - self.detect_started_at
        draw_text(self.screen, self.theme.f_tiny,
                  f"elapsed={elapsed:5.1f}s   ports={len(ALL_PORTS)}   "
                  f"fs={SAMPLING_HZ}Hz",
                  (zone.left, bar_y + bs + 60), color=TEXT_DIM)

    def _draw_detect_failure(self, zone, t):
        draw_block(self.screen, pygame.Rect(zone.left, zone.top + 6, 34, 34),
                   DANGER)
        draw_text(self.screen, self.theme.f_xl, "AUCUN BITALINO",
                  (zone.left + 48, zone.top + 8), color=DANGER, glow=DANGER_DIM)
        draw_text(self.screen, self.theme.f_small,
                  "La carte n'a pas pu être ouverte sur la liaison Bluetooth.",
                  (zone.left, zone.top + 74), color=TEXT_HI)
        err = self.detect_error or "(raison inconnue)"
        if len(err) > 90:
            err = err[:87] + "..."
        draw_text(self.screen, self.theme.f_small, f"› {err}",
                  (zone.left, zone.top + 112), color=DANGER)
        tips = [
            "• Carte allumée et appairée en Bluetooth.",
            f"• Adresse utilisée : {self.address}",
            "• Rapprochez la carte, fermez les autres apps audio/BT.",
            "• Ou lancez le MODE DÉMO pour tester l'interface seule.",
        ]
        y = zone.top + 158
        for tip in tips:
            draw_text(self.screen, self.theme.f_small, tip,
                      (zone.left, y), color=TEXT_MID)
            y += 30

        btn_w = max(260, zone.width // 3 - 20)
        btn_h = max(64, int(zone.height * 0.13))
        gap = 22
        total_w = btn_w * 2 + gap
        start_x = zone.left + (zone.width - total_w) // 2
        by = zone.bottom - btn_h - 24
        self.btn_retry.rect = pygame.Rect(start_x, by, btn_w, btn_h)
        self.btn_demo.rect  = pygame.Rect(start_x + btn_w + gap, by,
                                          btn_w, btn_h)
        self.btn_retry.draw(self.screen, self.theme.f_med_b, t)
        self.btn_demo.draw(self.screen, self.theme.f_med_b, t)

    # ── Écrans : étapes ────────────────────────────────────────────
    def _panel_header(self, rect, badge, title, accent, shape="T",
                       subtitle=None, sub2=None):
        """En-tête de panneau commun : tuile badge + titre néon + sous-titres.
        Renvoie le Y sous l'en-tête."""
        draw_panel(self.screen, rect, accent=accent)
        bs = self.theme.f_xl.get_height()
        bx, by = rect.left + 28, rect.top + 24
        draw_block(self.screen, pygame.Rect(bx, by, bs, bs), TETRO[shape])
        bt = self.theme.f_big.render(badge, True, _darken(TETRO[shape], 0.78))
        self.screen.blit(bt, bt.get_rect(center=(bx + bs // 2,
                                                 by + bs // 2)))
        tx = bx + bs + 20
        draw_text(self.screen, self.theme.f_xl, title, (tx, by - 2),
                  color=TEXT_HI, glow=accent)
        yy = by + bs + 14
        if subtitle:
            draw_text(self.screen, self.theme.f_med, subtitle,
                      (rect.left + 28, yy), color=TEXT_HI)
            yy += self.theme.f_med.get_height() + 6
        if sub2:
            draw_text(self.screen, self.theme.f_small, sub2,
                      (rect.left + 28, yy), color=TEXT_DIM)
            yy += self.theme.f_small.get_height() + 4
        self._hdr_bottom = yy
        return yy

    def _draw_intro(self, t):
        rect = self.layout.main
        self._panel_header(rect, "00", "PRÉPARATION", PHOSPHOR, shape="I")
        lines = [
            "▸ Branchez l'accéléromètre 3 axes sur la carte BITalino.",
            "▸ Branchez AUSSI le capteur cardiaque (oreille) maintenant :",
            "  il reste branché pendant tout le calibrage.",
            "▸ Aucun choix de port n'est nécessaire :",
            "  à la phase REPOS l'accéléromètre est immobile, donc le",
            "  seul signal qui bouge encore est le pouls → le port PPG",
            "  est isolé automatiquement, les 3 axes ensuite.",
            "▸ L'étape 04 calibre votre BPM de repos (capteur cardiaque).",
            "",
            "▸ Le calibrage produit le fichier  ›  calibration.json",
            "  (mapping des flèches + votre BPM de repos).",
            "",
            "  [ESC] pour quitter à tout moment.",
        ]
        y = self._hdr_bottom + 16
        for l in lines:
            draw_text(self.screen, self.theme.f_med, l,
                      (rect.left + 30, y), color=TEXT_MID)
            y += 32
        if self.btn_main.label == "[  OK  ]":
            self.btn_main.label = "[  COMMENCER LE CALIBRAGE  ]"
        self._place_main_button(rect)
        self.btn_main.accent = PHOSPHOR
        self.btn_main.draw(self.screen, self.theme.f_med_b, t)

    def _draw_rest(self, t):
        rect = self.layout.main
        self._panel_header(
            rect, "01", "REPOS + POULS", PHOSPHOR, shape="I",
            subtitle="Prenez l'accéléromètre en main et tenez-le IMMOBILE.",
            sub2="Lignes plates = accéléro. La courbe qui bouge encore = "
                 "le pouls (oreille) → port PPG isolé tout seul.")
        scope = self._scope_rect(rect)
        with self.device.lock:
            buffers = [list(b) for b in self.device.live_buf]
        labels = [f"P{i+1}" for i in range(6)]
        draw_scope(self.screen, scope, buffers,
                   axis_labels=labels, theme=self.theme)
        self._draw_progress_or_button(t, rect, "[  OK, JE SUIS PRÊT  ]")

    def _draw_axis_step(self, t, axis):
        rect = self.layout.main
        if axis == "x":
            badge, shape = "02", "J"
            title = "GAUCHE  ◄ ►  DROITE"
            instr = "Bougez l'accéléromètre de GAUCHE à DROITE plusieurs fois."
            tip   = "Allez jusqu'aux amplitudes que vous utiliserez en jeu."
            highlight_idx = self.x_axis
        else:
            badge, shape = "03", "L"
            title = "HAUT  ▲ ▼  BAS"
            instr = "Bougez l'accéléromètre de HAUT en BAS plusieurs fois."
            tip   = "Évitez de tourner sur l'axe X pendant ce balayage."
            highlight_idx = self.y_axis
        self._panel_header(rect, badge, title, PHOSPHOR, shape=shape,
                           subtitle=instr, sub2=tip)
        if self.x_axis is not None and axis == "y":
            draw_text(self.screen, self.theme.f_small,
                      f"✓ X = PORT {self.x_axis + 1}   "
                      f"[{self.x_min}..{self.x_max}]",
                      (rect.left + 30, self._hdr_bottom + 8),
                      color=PHOSPHOR_MID)
        scope = self._scope_rect(rect)
        with self.device.lock:
            buffers = [list(b) for b in self.device.live_buf]
        labels = [f"P{i+1}" for i in range(6)]
        draw_scope(self.screen, scope, buffers,
                   highlight=highlight_idx,
                   axis_labels=labels, theme=self.theme)
        self._draw_progress_or_button(t, rect, "[  OK, COMMENCER LE BALAYAGE  ]")

    def _draw_hr(self, t):
        rect = self.layout.main
        if self.ppg_port is not None:
            sub = (f"Port pouls isolé au REPOS → P{self.ppg_port + 1}. "
                   f"Calibration du BPM de repos ({HR_SECONDS:.0f} s).")
        else:
            sub = (f"Pouls non isolé : détection ici sur "
                   f"{HR_SECONDS:.0f} s, respirez calmement.")
        self._panel_header(
            rect, "04", "RYTHME CARDIAQUE", DANGER, shape="Z",
            subtitle="Gardez le capteur d'oreille en place, restez IMMOBILE.",
            sub2=sub)

        bpm_now = self._live_bpm()
        if self.ppg_port is not None and bpm_now > 0:
            phase = (t * bpm_now / 60.0) % 1.0
            beat  = phase < 0.16 or 0.32 < phase < 0.46
            hcol  = DANGER if beat else _darken(DANGER, 0.35)
            hx = rect.left + 30
            hy = self._hdr_bottom + 6
            bs = self.theme.f_big.get_height()
            draw_block(self.screen, pygame.Rect(hx, hy, bs, bs), hcol)
            draw_text(self.screen, self.theme.f_big,
                      f"{bpm_now} BPM",
                      (hx + bs + 14, hy - 2),
                      color=DANGER, glow=DANGER_DIM)

        axis_excl = self._ppg_excl(self.x_axis, self.y_axis, self.z_axis)
        scope = self._scope_rect(rect)
        with self.device.lock:
            buffers = [list(b) for b in self.device.live_buf]
        labels = [f"P{i+1}" + ("  (acc)" if i in axis_excl else
                               ("  ♥" if i == self.ppg_port else ""))
                  for i in range(6)]
        if self.ppg_port is not None:
            highlight = self.ppg_port
        else:
            # Met en évidence le meilleur candidat PPG en direct.
            base = self.device.frequency if self.device.frequency else SAMPLING_HZ
            freq = max(1, int(round(base / 8)))
            highlight, best = None, PPG_MIN_SCORE
            for cdt in (i for i in range(6) if i not in axis_excl):
                _, sc = _estimate_bpm_and_score(list(buffers[cdt]), freq)
                if sc > best:
                    best, highlight = sc, cdt
        draw_scope(self.screen, scope, buffers,
                   highlight=highlight, axis_labels=labels, theme=self.theme)
        self._draw_progress_or_button(
            t, rect, "[  OK, CALIBRER LE POULS DE REPOS  ]")

    def _live_bpm(self):
        """BPM live calculé sur le buffer du port PPG (recalcul ~1 Hz)."""
        if self.ppg_port is None or self.device is None:
            return self.bpm_rest
        now = time.time()
        if now - self._bpm_live_t < 1.0:
            return self._bpm_live
        self._bpm_live_t = now
        with self.device.lock:
            col = list(self.device.live_buf[self.ppg_port])
        base = self.device.frequency if self.device.frequency else SAMPLING_HZ
        freq = max(1, int(round(base / 8)))   # live_buf décimé 1/8
        bpm, score = _estimate_bpm_and_score(col, freq)
        if score > PPG_MIN_SCORE and PPG_MIN_BPM <= bpm <= PPG_MAX_BPM:
            self._bpm_live = bpm
        elif self._bpm_live == 0:
            self._bpm_live = self.bpm_rest
        return self._bpm_live

    def _draw_deadzone(self, t):
        rect = self.layout.main
        draw_panel(self.screen, rect, accent=AMBER)
        th  = self.theme
        pad = rect.left + 30

        # ── En-tête : empilé d'après la hauteur réelle des polices ──
        y = rect.top + 24
        bs = th.f_xl.get_height()
        draw_block(self.screen, pygame.Rect(pad, y, bs, bs), TETRO["T"])
        _bt = th.f_big.render("05", True, _darken(TETRO["T"], 0.78))
        self.screen.blit(_bt, _bt.get_rect(center=(pad + bs // 2,
                                                   y + bs // 2)))
        draw_text(self.screen, th.f_xl, "ZONE MORTE",
                  (pad + bs + 20, y - 2), color=TEXT_HI, glow=AMBER)
        y += th.f_xl.get_height() + 10
        draw_text(self.screen, th.f_med,
                  "Plage morte autour du repos : 0.0 = très sensible, "
                  "1.0 = pic du calibrage requis.",
                  (pad, y), color=TEXT_HI)
        y += th.f_med.get_height() + 16

        body_top = y
        body_h   = rect.bottom - body_top - 20

        # ── Colonne gauche : radar carré ───────────────────────────
        radar_size = max(160, min(body_h, rect.width // 2 - 50))
        radar = pygame.Rect(rect.left + 30, body_top, radar_size, radar_size)
        pygame.draw.rect(self.screen, (10, 11, 24), radar, border_radius=4)
        pygame.draw.rect(self.screen, PHOSPHOR_DIM, radar, 1, border_radius=4)

        with self.device.lock:
            sample = self.device.latest
        if self.x_axis is not None and self.y_axis is not None:
            xn = _normalize(sample[self.x_axis],
                            statistics.mean(s[self.x_axis] for s in self.rest_samples),
                            self.x_min, self.x_max)
            yn = _normalize(sample[self.y_axis],
                            statistics.mean(s[self.y_axis] for s in self.rest_samples),
                            self.y_min, self.y_max)
        else:
            xn, yn = 0.0, 0.0
        draw_radar(self.screen, radar, xn, yn, self.slider.value, self.theme)

        # ── Colonne droite : table → slider → boutons ──────────────
        #    Budget vertical déterministe (pile sans chevauchement) :
        #    boutons ancrés en bas, slider réservé au-dessus, table = reste.
        right_x = radar.right + 40
        right_w = rect.right - right_x - 20
        right_y = body_top
        gap     = 12
        tiny_h  = th.f_tiny.get_height()
        big_h   = th.f_big.get_height()
        med_h   = th.f_med.get_height()

        # 1. Boutons en bas
        btn_h   = max(48, min(80, int(body_h * 0.15)))
        btn_top = rect.bottom - btn_h - 18

        # 2. Bloc slider : hauteur réelle réservée
        #    haut = label "DEAD ZONE" + texte valeur ; bas = graduations.
        top_pad   = 24 + big_h + 6 + tiny_h + 4  # label + valeur au-dessus
        bot_pad   = 14 + tiny_h + 6              # sous centery
        slider_cy = btn_top - gap - bot_pad

        # 3. Table : occupe l'espace restant, hauteur de ligne adaptée
        table_top = right_y + tiny_h + 10
        table_avail = (slider_cy - top_pad - gap) - table_top

        bpm_now = self._live_bpm()
        if self.ppg_port is not None:
            ppg_loc = f"PORT {self.ppg_port + 1}"
            bpm_val = f"{bpm_now} BPM" if bpm_now > 0 else "-- BPM"
        else:
            ppg_loc, bpm_val = "N/A", "--"
        rows = [
            ("AXE  X", f"P{self.x_axis + 1}",
             f"[{self.x_min} .. {self.x_max}]"),
            ("AXE  Y", f"P{self.y_axis + 1}",
             f"[{self.y_min} .. {self.y_max}]"),
            ("AXE  Z", (f"P{self.z_axis + 1}"
                        if self.z_axis is not None else "N/A"), ""),
            ("♥ POULS", bpm_val, ppg_loc),
        ]
        row_h = max(med_h + 4, min(70, table_avail // len(rows)))

        draw_text(self.screen, th.f_tiny, "// CAPTEURS DÉTECTÉS",
                  (right_x, right_y), color=PHOSPHOR_MID)
        c_dy = max(0, (med_h - th.f_small.get_height()) // 2)
        row_cols = [
            PORT_COLORS[self.x_axis % 6],
            PORT_COLORS[self.y_axis % 6],
            PORT_COLORS[self.z_axis % 6] if self.z_axis is not None
            else TEXT_FAINT,
            DANGER,
        ]
        chip = max(10, med_h - 4)
        for i, (a, b, c) in enumerate(rows):
            yy = table_top + i * row_h
            heart = a.startswith("♥")
            if heart and bpm_now > 0:
                phase = (t * bpm_now / 60.0) % 1.0
                rc = DANGER if (phase < 0.16 or 0.32 < phase < 0.46) \
                    else _darken(DANGER, 0.4)
            else:
                rc = row_cols[i]
            # Pastille couleur de pièce dans la gouttière (sans décaler le texte)
            draw_block(self.screen,
                       pygame.Rect(right_x - 18, yy + (med_h - chip) // 2,
                                   chip, chip), rc)
            draw_text(self.screen, th.f_med_b, a, (right_x, yy),
                      color=rc if heart else TEXT_HI)
            draw_text(self.screen, th.f_med_b if heart else th.f_med, b,
                      (right_x + 118, yy), color=rc,
                      glow=DANGER_DIM if heart else None)
            if c:
                cw = th.f_small.size(c)[0]
                draw_text(self.screen, th.f_small, c,
                          (right_x + right_w - cw, yy + c_dy),
                          color=TEXT_DIM)

        # 4. Slider
        slider_margin = 16
        self.slider.rect = pygame.Rect(right_x + slider_margin, slider_cy,
                                       right_w - 2 * slider_margin, 6)
        draw_text(self.screen, th.f_tiny, "DEAD ZONE",
                  (right_x + slider_margin,
                   slider_cy - 24 - big_h - 4 - tiny_h),
                  color=AMBER)
        self.slider.draw(self.screen, th.f_big, th.f_tiny)

        # 5. Boutons : [RECOMMENCER] | [VALIDER]
        btn_gap  = 10
        btn_each = (right_w - btn_gap) // 2
        self.btn_recalibrate.rect = pygame.Rect(right_x, btn_top,
                                                btn_each, btn_h)
        self.btn_recalibrate.draw(self.screen, th.f_small, t)
        self.btn_main.rect = pygame.Rect(right_x + btn_each + btn_gap, btn_top,
                                         right_w - btn_each - btn_gap, btn_h)
        self.btn_main.accent = AMBER
        self.btn_main.draw(self.screen, th.f_med_b, t)

    def _draw_done(self, t):
        rect = self.layout.main
        draw_panel(self.screen, rect, accent=PHOSPHOR_MID)
        # Bandeau de tuiles "ligne complétée"
        order = ["I", "J", "L", "O", "S", "T", "Z"]
        bs = max(20, rect.width // 26)
        total = len(order) * (bs + 6) - 6
        bxs = rect.centerx - total // 2
        for i, k in enumerate(order):
            off = int(5 * math.sin(t * 3 + i * 0.6))
            draw_block(self.screen,
                       pygame.Rect(bxs + i * (bs + 6), rect.top + 40 + off,
                                   bs, bs), TETRO[k])
        draw_text_centered(self.screen, self.theme.f_huge, "CALIBRAGE OK",
                           (rect.centerx, rect.top + 40 + bs + 64),
                           color=PHOSPHOR_MID, glow=PHOSPHOR_MID)
        draw_text_centered(self.screen, self.theme.f_small,
                           "› calibration.json écrit dans le dossier courant",
                           (rect.centerx, rect.top + 40 + bs + 118),
                           color=TEXT_MID)
        x_rest = statistics.mean(s[self.x_axis] for s in self.rest_samples)
        y_rest = statistics.mean(s[self.y_axis] for s in self.rest_samples)
        cards = [
            ("PORTS", f"X·P{self.x_axis+1}  Y·P{self.y_axis+1}  "
             f"Z·{'P'+str(self.z_axis+1) if self.z_axis is not None else '—'}",
             PHOSPHOR),
            ("REPOS", f"X={x_rest:.0f}  Y={y_rest:.0f}", TETRO["J"]),
            ("AMPLITUDE X", f"{self.x_min} .. {self.x_max}", TETRO["L"]),
            ("AMPLITUDE Y", f"{self.y_min} .. {self.y_max}", TETRO["O"]),
            ("ZONE MORTE", f"{self.slider.value:.2f}", AMBER),
            ("POULS", (f"P{self.ppg_port+1}  ·  {self.bpm_rest} BPM"
                       if self.ppg_port is not None else "N/A"), DANGER),
        ]
        gx = rect.left + 60
        gw = rect.width - 120
        cw = (gw - 24) // 2
        ch = max(54, int(rect.height * 0.085))
        gy = rect.top + 40 + bs + 150
        for i, (lab, val, col) in enumerate(cards):
            cx = gx + (i % 2) * (cw + 24)
            cy = gy + (i // 2) * (ch + 14)
            card = pygame.Rect(cx, cy, cw, ch)
            pygame.draw.rect(self.screen, BG_PANEL, card, border_radius=4)
            pygame.draw.rect(self.screen, _darken(col, 0.4), card, 1,
                             border_radius=4)
            pygame.draw.rect(self.screen, col,
                             pygame.Rect(card.x, card.y + 4, 4,
                                         card.h - 8))
            draw_text(self.screen, self.theme.f_tiny, lab,
                      (card.x + 18, card.y + 10), color=col)
            draw_text(self.screen, self.theme.f_med_b, val,
                      (card.x + 18, card.y + 10 +
                       self.theme.f_tiny.get_height() + 4), color=TEXT_HI)
        btn_w = max(280, rect.width // 3)
        bx = rect.left + (rect.width - btn_w) // 2
        self.btn_main.rect = pygame.Rect(bx, rect.bottom - 96, btn_w, 70)
        self.btn_main.accent = PHOSPHOR_MID
        self.btn_main.draw(self.screen, self.theme.f_med_b, t)

    # ── Helpers de placement ───────────────────────────────────────
    def _scope_rect(self, panel):
        # Sous l'en-tête réel (hauteur des polices variable) + 1 ligne info.
        hdr = getattr(self, "_hdr_bottom", panel.top + 150)
        top    = max(panel.top + 196, hdr + 56)
        bottom = panel.bottom - 128
        return pygame.Rect(panel.left + 30, top,
                           panel.width - 60, max(140, bottom - top))

    def _place_main_button(self, panel):
        btn_w = max(360, panel.width // 2)
        bx = panel.left + (panel.width - btn_w) // 2
        self.btn_main.rect = pygame.Rect(bx, panel.bottom - 110, btn_w, 80)

    def _draw_progress_or_button(self, t, rect, prompt_label):
        rec = self.recording_until > 0 and time.time() < self.recording_until
        if rec:
            remaining = self.recording_until - time.time()
            total = {STATE_REST: REST_SECONDS,
                     STATE_HR:   HR_SECONDS}.get(self.state, MOVE_SECONDS)
            frac = max(0.0, min(1.0, 1.0 - max(0, remaining) / total))
            # Pile de tuiles qui se remplit (clear de ligne en cours)
            cols = 20
            seg = (rect.width - 120) // cols
            bar_w = seg * cols
            bx0 = rect.left + (rect.width - bar_w) // 2
            by = rect.bottom - 78
            lit = int(frac * cols + 0.001)
            order = list(TETRO)
            for i in range(cols):
                cell = pygame.Rect(bx0 + i * seg, by, seg - 4, 30)
                if i < lit:
                    draw_block(self.screen, cell,
                               TETRO[order[i % len(order)]])
                else:
                    pygame.draw.rect(self.screen, GRID_HI, cell, 1,
                                     border_radius=3)
            pulse = AMBER if int(t * 4) % 2 == 0 else _darken(AMBER, 0.4)
            cs = self.theme.f_xl.get_height()
            txt = f"{remaining:0.1f}s"
            ts = self.theme.f_xl.render(txt, True, AMBER)
            tx = rect.centerx - (cs + 14 + ts.get_width()) // 2
            ty = by - cs - 26
            draw_block(self.screen, pygame.Rect(tx, ty, cs, cs), pulse)
            rl = self.theme.f_tiny.render("REC", True, _darken(AMBER, 0.78))
            self.screen.blit(rl, rl.get_rect(center=(tx + cs // 2,
                                                     ty + cs // 2)))
            for off in [(-2, 0), (2, 0), (0, -2), (0, 2)]:
                g = self.theme.f_xl.render(txt, True, AMBER)
                g.set_alpha(80)
                self.screen.blit(g, (tx + cs + 14 + off[0], ty + off[1]))
            self.screen.blit(ts, (tx + cs + 14, ty))
        else:
            self.btn_main.label = prompt_label
            self.btn_main.accent = PHOSPHOR
            self._place_main_button(rect)
            self.btn_main.draw(self.screen, self.theme.f_med_b, t)


def _normalize(v, rest, vmin, vmax):
    if v >= rest:
        return (v - rest) / max(1, (vmax - rest))
    return (v - rest) / max(1, (rest - vmin))


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
def main(argv):
    address = DEFAULT_ADDR
    for a in argv:
        if not a.startswith("-"):
            address = a

    pygame.init()
    pygame.display.set_caption("BITalino — Calibrage Accéléromètre")
    flags = pygame.RESIZABLE | pygame.DOUBLEBUF
    screen = pygame.display.set_mode((INITIAL_W, INITIAL_H), flags)

    app = App(screen, address)
    try:
        app.run()
    finally:
        if app.device is not None:
            try:
                app.device.stop_flag = True
                if app.acq_thread is not None:
                    app.acq_thread.join(timeout=2)
                app.device.stop()
                app.device.close()
            except Exception:
                pass
        pygame.quit()


if __name__ == "__main__":
    main(sys.argv[1:])
