"""
Calibrage accéléromètre BITalino — Interface CRT responsive
============================================================

Écran de calibration façon oscilloscope de laboratoire (CRT phosphor).

Phases :
  0. DÉTECTION  : tente d'ouvrir la carte BITalino.
                  En cas d'échec → bouton RÉESSAYER + bouton MODE DÉMO.
  1. REPOS      : tient l'accéléromètre immobile pour mesurer la baseline.
  2. G ↔ D      : axe X = port avec la plus forte variance (vs repos).
  3. H ↕ B      : axe Y = port restant avec la plus forte variance.
  4. ZONE MORTE : seuil 0..1 à régler avec un slider, radar XY temps réel.

Les 3 ports accéléromètre sont identifiés AUTOMATIQUEMENT (algorithme
moyenne+σ pendant la phase REPOS, puis variance pendant les phases G/D et H/B).
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
REST_SECONDS         = 3.0
MOVE_SECONDS         = 5.0
HR_SECONDS           = 15.0

# Palette CRT phosphor
BG_DEEP      = (  4,   8,  14)
BG_PANEL     = ( 10,  18,  26)
BG_PANEL_HI  = ( 16,  28,  38)
GRID_DIM     = ( 18,  34,  44)
GRID_HI      = ( 32,  60,  72)
PHOSPHOR     = ( 88, 255, 188)
PHOSPHOR_MID = ( 50, 180, 130)
PHOSPHOR_DIM = ( 22,  72,  56)
AMBER        = (255, 176,  64)
AMBER_DIM    = (110,  72,  20)
TEXT_HI      = (220, 240, 235)
TEXT_MID     = (140, 180, 170)
TEXT_DIM     = ( 80, 120, 110)
TEXT_FAINT   = ( 40,  68,  62)
DANGER       = (255,  68,  92)
DANGER_DIM   = (110,  20,  32)

PORT_COLORS = [
    (255, 110, 150), (255, 188,  72), (180, 255,  96),
    ( 96, 200, 255), (220, 130, 255), (255, 240, 110),
]


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
            # Simulated PPG on port 4 (index 3) at ~72 BPM (1.2 Hz)
            ppg = int(400 + 80 * math.sin(t * 0.905) + rng.uniform(-6, 6)) \
                  if self._mode == "hr" else 256
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
def _accel_candidates(samples):
    cands = []
    for i in range(6):
        col = [s[i] for s in samples]
        m = statistics.mean(col)
        sd = statistics.pstdev(col) if len(col) > 1 else 0
        if 180 < m < 840 and sd < 20:
            cands.append(i)
    return cands or list(range(6))


def _max_delta_std(rest, active, candidates):
    if not candidates:
        return None
    def sd(samples, idx):
        col = [s[idx] for s in samples]
        return statistics.pstdev(col) if len(col) > 1 else 0
    return max(candidates, key=lambda c: sd(active, c) - sd(rest, c))


def detect_x_axis(rest_samples, lr_samples):
    return _max_delta_std(rest_samples, lr_samples, _accel_candidates(rest_samples))


def detect_y_axis(rest_samples, ud_samples, exclude):
    cands = [c for c in _accel_candidates(rest_samples) if c != exclude]
    return _max_delta_std(rest_samples, ud_samples, cands)


def detect_z_axis(rest_samples, exclude):
    cands = [c for c in _accel_candidates(rest_samples) if c not in exclude]
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


def detect_ppg_port(hr_samples, exclude_ports, frequency=SAMPLING_HZ):
    """Return (port_index, bpm) for the best PPG candidate, or (None, 0)."""
    candidates = [i for i in range(6) if i not in exclude_ports]
    if not candidates:
        return None, 0
    best_port, best_bpm, best_score = None, 0, -1.0
    for port in candidates:
        col = [s[port] for s in hr_samples]
        bpm, score = _estimate_bpm_and_score(col, frequency)
        if score > best_score:
            best_score, best_port, best_bpm = score, port, bpm
    return best_port, best_bpm


# ─────────────────────────────────────────────
#  Polices
# ─────────────────────────────────────────────
class Theme:
    """Recalcule les tailles de polices proportionnellement à la fenêtre."""
    FAMILY = "consolas,couriernew,courier,monospace"

    def __init__(self, h):
        self.update(h)

    def update(self, h):
        # Ratios calibrés sur 950px de hauteur
        s = max(0.70, min(1.6, h / 950))
        self.f_huge   = pygame.font.SysFont(self.FAMILY, int(72 * s), bold=True)
        self.f_xl     = pygame.font.SysFont(self.FAMILY, int(48 * s), bold=True)
        self.f_big    = pygame.font.SysFont(self.FAMILY, int(34 * s), bold=True)
        self.f_med    = pygame.font.SysFont(self.FAMILY, int(22 * s))
        self.f_med_b  = pygame.font.SysFont(self.FAMILY, int(22 * s), bold=True)
        self.f_small  = pygame.font.SysFont(self.FAMILY, int(17 * s))
        self.f_tiny   = pygame.font.SysFont(self.FAMILY, int(13 * s))
        self.f_micro  = pygame.font.SysFont(self.FAMILY, int(11 * s))


# ─────────────────────────────────────────────
#  Pré-rendus (rebuild si la taille change)
# ─────────────────────────────────────────────
def make_scanlines(w, h, alpha=22, spacing=3):
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    for y in range(0, h, spacing):
        pygame.draw.line(s, (0, 0, 0, alpha), (0, y), (w, y))
    return s


def make_vignette(w, h):
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    layers = max(20, min(60, w // 40))
    for i in range(layers):
        a = int(4 + i * 1.4)
        pygame.draw.rect(overlay, (0, 0, 0, a),
                         pygame.Rect(i, i, w - 2 * i, h - 2 * i), 1)
    return overlay


def make_grid(w, h, step=40):
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    for x in range(0, w, step):
        c = GRID_HI if (x // step) % 5 == 0 else GRID_DIM
        pygame.draw.line(s, (*c, 110), (x, 0), (x, h))
    for y in range(0, h, step):
        c = GRID_HI if (y // step) % 5 == 0 else GRID_DIM
        pygame.draw.line(s, (*c, 110), (0, y), (w, y))
    return s


# ─────────────────────────────────────────────
#  Helpers de dessin
# ─────────────────────────────────────────────
def draw_text(surf, font, text, pos, color=TEXT_HI, glow=None):
    if glow is not None:
        g = font.render(text, True, glow)
        g.set_alpha(70)
        for off in [(-2, 0), (2, 0), (0, -2), (0, 2)]:
            surf.blit(g, (pos[0] + off[0], pos[1] + off[1]))
    t = font.render(text, True, color)
    surf.blit(t, pos)
    return t.get_rect(topleft=pos)


def draw_text_centered(surf, font, text, center, color=TEXT_HI, glow=None):
    t = font.render(text, True, color)
    rect = t.get_rect(center=center)
    if glow is not None:
        g = font.render(text, True, glow)
        g.set_alpha(60)
        for off in [(-2, 0), (2, 0), (0, -2), (0, 2)]:
            surf.blit(g, (rect.x + off[0], rect.y + off[1]))
    surf.blit(t, rect)
    return rect


def draw_corner_brackets(surf, rect, color=PHOSPHOR_DIM, length=18, width=2):
    x, y, w, h = rect
    pygame.draw.line(surf, color, (x, y), (x + length, y), width)
    pygame.draw.line(surf, color, (x, y), (x, y + length), width)
    pygame.draw.line(surf, color, (x + w, y), (x + w - length, y), width)
    pygame.draw.line(surf, color, (x + w, y), (x + w, y + length), width)
    pygame.draw.line(surf, color, (x, y + h), (x + length, y + h), width)
    pygame.draw.line(surf, color, (x, y + h), (x, y + h - length), width)
    pygame.draw.line(surf, color, (x + w, y + h), (x + w - length, y + h), width)
    pygame.draw.line(surf, color, (x + w, y + h), (x + w, y + h - length), width)


def draw_panel(surf, rect, fill=BG_PANEL, border=PHOSPHOR_DIM):
    pygame.draw.rect(surf, fill, rect)
    pygame.draw.rect(surf, border, rect, 1)
    draw_corner_brackets(surf, rect)


def draw_scope(surf, rect, buffers, highlight=None, axis_labels=None, theme=None):
    pygame.draw.rect(surf, BG_DEEP, rect)
    cell = max(20, rect.width // 24)
    for x in range(rect.left, rect.right, cell):
        pygame.draw.line(surf, GRID_DIM, (x, rect.top), (x, rect.bottom))
    for y in range(rect.top, rect.bottom, cell):
        pygame.draw.line(surf, GRID_DIM, (rect.left, y), (rect.right, y))
    cy = rect.centery
    pygame.draw.line(surf, GRID_HI, (rect.left, cy), (rect.right, cy), 1)
    pygame.draw.rect(surf, PHOSPHOR_DIM, rect, 1)
    draw_corner_brackets(surf, rect, color=PHOSPHOR_DIM, length=14)

    n = max(1, len(buffers[0]))
    step = rect.width / n
    for i, buf in enumerate(buffers):
        color = PORT_COLORS[i % len(PORT_COLORS)]
        is_hi = (highlight is None) or (i == highlight)
        if not is_hi:
            color = tuple(int(c * 0.25) for c in color)
        pts = []
        for k, v in enumerate(buf):
            x = rect.left + int(k * step)
            y = int(cy - (v - 512) * (rect.height / 2) / 512)
            y = max(rect.top + 1, min(rect.bottom - 1, y))
            pts.append((x, y))
        if len(pts) > 1:
            try:
                pygame.draw.aalines(surf, color, False, pts)
                if is_hi:
                    pygame.draw.lines(surf, color, False, pts, 2)
            except ValueError:
                pass

    if theme is not None and axis_labels:
        x = rect.right - 90
        y = rect.top + 8
        for i, lab in enumerate(axis_labels):
            color = PORT_COLORS[i % len(PORT_COLORS)]
            is_hi = (highlight is None) or (i == highlight)
            c = color if is_hi else tuple(int(c * 0.35) for c in color)
            t = theme.f_tiny.render(lab, True, c)
            surf.blit(t, (x, y))
            y += 14


def draw_radar(surf, rect, x_norm, y_norm, dead_zone, theme):
    cx, cy = rect.center
    r = min(rect.w, rect.h) // 2 - 26
    for k in (1, 2, 3, 4):
        pygame.draw.circle(surf, GRID_DIM, (cx, cy), r * k // 4, 1)
    pygame.draw.line(surf, GRID_HI, (cx - r, cy), (cx + r, cy), 1)
    pygame.draw.line(surf, GRID_HI, (cx, cy - r), (cx, cy + r), 1)

    dz_surf = pygame.Surface((2 * r + 2, 2 * r + 2), pygame.SRCALPHA)
    pygame.draw.circle(dz_surf, (*AMBER, 40), (r + 1, r + 1), int(r * dead_zone))
    pygame.draw.circle(dz_surf, (*AMBER, 200), (r + 1, r + 1), int(r * dead_zone), 2)
    surf.blit(dz_surf, (cx - r - 1, cy - r - 1))

    in_dead = math.hypot(x_norm, y_norm) <= dead_zone
    arrow_color = lambda active: PHOSPHOR if active else TEXT_DIM
    draw_text_centered(surf, theme.f_big, "←",
                       (cx - r - 26, cy),
                       arrow_color((not in_dead) and x_norm < -dead_zone))
    draw_text_centered(surf, theme.f_big, "→",
                       (cx + r + 26, cy),
                       arrow_color((not in_dead) and x_norm >  dead_zone))
    draw_text_centered(surf, theme.f_big, "↑",
                       (cx, cy - r - 26),
                       arrow_color((not in_dead) and y_norm >  dead_zone))
    draw_text_centered(surf, theme.f_big, "↓",
                       (cx, cy + r + 26),
                       arrow_color((not in_dead) and y_norm < -dead_zone))

    px = cx + int(max(-1, min(1, x_norm)) * r)
    py = cy - int(max(-1, min(1, y_norm)) * r)
    halo_color = AMBER if in_dead else PHOSPHOR
    halo = pygame.Surface((40, 40), pygame.SRCALPHA)
    pygame.draw.circle(halo, (*halo_color, 60), (20, 20), 18)
    surf.blit(halo, (px - 20, py - 20))
    pygame.draw.circle(surf, halo_color, (px, py), 7)
    pygame.draw.circle(surf, BG_DEEP, (px, py), 3)


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
        a = self.accent if self.enabled else TEXT_FAINT
        pygame.draw.rect(surf, BG_PANEL_HI if self.hover else BG_PANEL, self.rect)
        thick = 3 if self.hover else 2
        pygame.draw.rect(surf, a, self.rect, thick)
        cy = self.rect.centery
        chev = max(8, self.rect.height // 6)
        pygame.draw.line(surf, a, (self.rect.left + 12, cy),
                         (self.rect.left + 12 + chev, cy - chev), 2)
        pygame.draw.line(surf, a, (self.rect.left + 12, cy),
                         (self.rect.left + 12 + chev, cy + chev), 2)
        pygame.draw.line(surf, a, (self.rect.right - 12, cy),
                         (self.rect.right - 12 - chev, cy - chev), 2)
        pygame.draw.line(surf, a, (self.rect.right - 12, cy),
                         (self.rect.right - 12 - chev, cy + chev), 2)
        if self.hover or self._pulse > 0:
            glow = pygame.Surface(self.rect.size, pygame.SRCALPHA)
            alpha = int(40 + 60 * (breathe if self.hover else self._pulse))
            pygame.draw.rect(glow, (*a, alpha), glow.get_rect(), 6)
            surf.blit(glow, self.rect.topleft)
        t = font.render(self.label, True, a)
        surf.blit(t, t.get_rect(center=self.rect.center))


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
        track = pygame.Rect(self.rect.left, self.rect.centery - 3,
                            self.rect.width, 6)
        pygame.draw.rect(surf, BG_PANEL_HI, track, border_radius=3)
        ratio = (self.value - self.vmin) / (self.vmax - self.vmin)
        filled = pygame.Rect(track.left, track.top,
                             int(track.width * ratio), track.height)
        pygame.draw.rect(surf, self.accent, filled, border_radius=3)
        for i in range(11):
            tx = self.rect.left + int(self.rect.width * i / 10)
            h = 14 if i % 5 == 0 else 7
            pygame.draw.line(surf, TEXT_DIM,
                             (tx, self.rect.centery - h),
                             (tx, self.rect.centery + h), 1)
            if i % 5 == 0:
                lab = f"{i / 10:.1f}"
                t = font_label.render(lab, True, TEXT_DIM)
                surf.blit(t, (tx - t.get_width() // 2, self.rect.centery + 18))
        hx = self.rect.left + int(self.rect.width * ratio)
        handle = pygame.Rect(hx - 12, self.rect.centery - 22, 24, 44)
        pygame.draw.rect(surf, self.accent, handle, border_radius=3)
        pygame.draw.rect(surf, BG_DEEP, handle.inflate(-8, -16), border_radius=2)
        val_str = f"{self.value:.2f}"
        t = font_value.render(val_str, True, self.accent)
        surf.blit(t, (hx - t.get_width() // 2, self.rect.centery - 70))


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
        self.x_axis = self.y_axis = self.z_axis = None
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
        if self.state in (STATE_INTRO, STATE_REST, STATE_LR, STATE_UD):
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

    def _on_recording_done(self):
        if self.state == STATE_REST:
            self.rest_samples = self._stop_recording()
            if not self.rest_samples:
                self._log("ERR: NO SAMPLES — CHECK BITALINO LINK")
                self.btn_main.label = "[  RÉESSAYER  ]"
                return
            cands = _accel_candidates(self.rest_samples)
            self._log(f"REST OK — CANDIDATES: P{[c+1 for c in cands]}")
            self.state = STATE_LR
            self.btn_main.label = "[  OK, COMMENCER LE BALAYAGE G/D  ]"
            return
        if self.state == STATE_LR:
            self.lr_samples = self._stop_recording()
            self.x_axis = detect_x_axis(self.rest_samples, self.lr_samples)
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
                                        exclude=self.x_axis)
            ys = [s[self.y_axis] for s in self.ud_samples]
            self.y_min, self.y_max = min(ys), max(ys)
            self.z_axis = detect_z_axis(self.rest_samples,
                                        exclude=(self.x_axis, self.y_axis))
            self._log(f"Y AXIS DETECTED → PORT {self.y_axis + 1}  "
                      f"[{self.y_min}..{self.y_max}]")
            if self.z_axis is not None:
                self._log(f"Z AXIS INFERRED  → PORT {self.z_axis + 1}")
            self.state = STATE_DEADZONE
            self.btn_main.label = "[  VALIDER LA ZONE MORTE  ]"
            return

    def _restart_calibration(self):
        self.rest_samples = []
        self.lr_samples   = []
        self.ud_samples   = []
        self.x_axis = self.y_axis = self.z_axis = None
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

        self._draw_header(t)
        self._draw_title()

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
            elif self.state == STATE_DEADZONE:
                self._draw_deadzone(t)
            elif self.state == STATE_DONE:
                self._draw_done(t)

        self.screen.blit(self.scanlines, (0, 0))
        self.screen.blit(self.vignette, (0, 0))
        self._draw_noise()

    def _draw_noise(self):
        s = pygame.Surface(self.screen.get_size(), pygame.SRCALPHA)
        w, h = s.get_size()
        for _ in range(int(220 * (w * h) / (1600 * 950))):
            s.set_at((random.randint(0, w - 1), random.randint(0, h - 1)),
                     (255, 255, 255, 12))
        self.screen.blit(s, (0, 0))

    def _draw_header(self, t):
        bar = self.layout.header
        pygame.draw.rect(self.screen, BG_PANEL, bar)
        pygame.draw.line(self.screen, PHOSPHOR_DIM,
                         (0, bar.bottom), (bar.right, bar.bottom), 1)
        draw_text(self.screen, self.theme.f_small,
                  "▣  BITALINO  /  ACCELEROMETER CALIBRATION  v1.1",
                  (bar.left + self.layout.margin, bar.centery - 10),
                  color=PHOSPHOR)
        blink = "●" if int(t * 2) % 2 == 0 else "○"
        is_rec = self.recording_until > 0
        if self.state == STATE_DETECT:
            status_color = AMBER if self.detect_status == "scanning" else (
                PHOSPHOR_MID if self.detect_status == "ok" else DANGER)
            status_lab = self.detect_status.upper()
        else:
            status_color = AMBER if is_rec else PHOSPHOR_MID
            status_lab   = "REC" if is_rec else "RDY"
        right = f"{blink} {status_lab}   {time.strftime('%H:%M:%S')}   "\
                f"{self.layout.w}×{self.layout.h}"
        t_surf = self.theme.f_small.render(right, True, status_color)
        self.screen.blit(
            t_surf,
            (bar.right - t_surf.get_width() - self.layout.margin,
             bar.centery - 10))

    def _draw_title(self):
        rect = self.layout.title
        draw_text(self.screen, self.theme.f_huge, "CALIBRAGE",
                  (rect.left, rect.top), color=TEXT_HI, glow=PHOSPHOR_DIM)
        draw_text(self.screen, self.theme.f_med,
                  "// ACCÉLÉROMÈTRE 3 AXES → FLÈCHES ←→↑↓",
                  (rect.left + 4, rect.top + int(rect.height * 0.7)),
                  color=TEXT_DIM)

    def _draw_step_track(self):
        rect = self.layout.side
        steps = [
            ("01", "REPOS",          STATE_REST),
            ("02", "GAUCHE / DROITE", STATE_LR),
            ("03", "HAUT / BAS",     STATE_UD),
            ("04", "ZONE MORTE",     STATE_DEADZONE),
        ]
        order = [STATE_INTRO, STATE_REST, STATE_LR, STATE_UD,
                 STATE_DEADZONE, STATE_DONE]
        cur_idx = order.index(self.state) if self.state in order else 0
        slot_h = rect.height // 4
        for i, (num, label, st) in enumerate(steps):
            y = rect.top + i * slot_h
            done = order.index(st) < cur_idx
            active = self.state == st
            color = PHOSPHOR if active else (PHOSPHOR_MID if done else TEXT_FAINT)
            cx = rect.left + 30
            cy = y + slot_h // 2
            pygame.draw.line(self.screen, GRID_HI,
                             (cx, y + 10), (cx, y + slot_h - 10), 1)
            pygame.draw.circle(self.screen, BG_DEEP, (cx, cy), 22)
            pygame.draw.circle(self.screen, color, (cx, cy), 22, 2)
            if done:
                pygame.draw.line(self.screen, color, (cx - 8, cy + 1),
                                 (cx - 2, cy + 7), 2)
                pygame.draw.line(self.screen, color, (cx - 2, cy + 7),
                                 (cx + 9, cy - 6), 2)
            elif active:
                pygame.draw.circle(self.screen, color, (cx, cy), 8)
            draw_text(self.screen, self.theme.f_tiny, num,
                      (rect.left, cy - 18), color=color)
            draw_text(self.screen,
                      self.theme.f_med_b if active else self.theme.f_med,
                      label, (cx + 36, cy - 16), color=color)
            if active:
                draw_text(self.screen, self.theme.f_tiny, "▶ EN COURS",
                          (cx + 36, cy + 10), color=AMBER)

    def _draw_log_panel(self):
        rect = self.layout.log
        draw_panel(self.screen, rect)
        draw_text(self.screen, self.theme.f_tiny, "// CONSOLE",
                  (rect.left + 12, rect.top + 8), color=PHOSPHOR_MID)
        y = rect.top + 32
        max_lines = max(4, (rect.height - 40) // 22)
        for line in list(self.log_lines)[-max_lines:]:
            draw_text(self.screen, self.theme.f_tiny, line,
                      (rect.left + 12, y), color=TEXT_MID)
            y += 22

    # ── Écran : Détection ──────────────────────────────────────────
    def _draw_detect(self, t):
        # Panneau central plein-largeur (sauf marges)
        m = self.layout.margin
        rect = pygame.Rect(m, self.layout.title.bottom + 10,
                           self.layout.w - 2 * m,
                           self.layout.h - self.layout.title.bottom - m - 20)
        draw_panel(self.screen, rect)
        # Console à droite (mini)
        console_w = max(360, int(rect.width * 0.34))
        console = pygame.Rect(rect.right - console_w - 24, rect.top + 24,
                              console_w, rect.height - 48)
        pygame.draw.rect(self.screen, BG_PANEL_HI, console)
        pygame.draw.rect(self.screen, PHOSPHOR_DIM, console, 1)
        draw_text(self.screen, self.theme.f_tiny, "// LOG",
                  (console.left + 12, console.top + 10), color=PHOSPHOR_MID)
        y = console.top + 36
        max_lines = max(6, (console.height - 50) // 22)
        for line in list(self.log_lines)[-max_lines:]:
            draw_text(self.screen, self.theme.f_tiny, line,
                      (console.left + 12, y), color=TEXT_MID)
            y += 22

        # Zone de détection (à gauche du log)
        zone = pygame.Rect(rect.left + 24, rect.top + 24,
                           console.left - rect.left - 48,
                           rect.height - 48)
        if self.detect_status == "scanning":
            self._draw_scan_animation(zone, t)
        elif self.detect_status == "ok":
            draw_text_centered(self.screen, self.theme.f_huge, "✓ DÉTECTÉ",
                               zone.center, color=PHOSPHOR, glow=PHOSPHOR_DIM)
        elif self.detect_status == "fail":
            self._draw_detect_failure(zone, t)

    def _draw_scan_animation(self, zone, t):
        draw_text(self.screen, self.theme.f_xl, "DÉTECTION DU MATÉRIEL",
                  (zone.left, zone.top + 10), color=PHOSPHOR, glow=PHOSPHOR_DIM)
        draw_text(self.screen, self.theme.f_med,
                  f"Sondage de la liaison Bluetooth {self.address}",
                  (zone.left, zone.top + 80), color=TEXT_MID)
        # Barre de scan animée
        bar = pygame.Rect(zone.left, zone.top + 140, zone.width, 36)
        pygame.draw.rect(self.screen, BG_PANEL_HI, bar)
        pygame.draw.rect(self.screen, PHOSPHOR_DIM, bar, 1)
        # Curseur qui se déplace
        cursor_w = max(80, bar.width // 6)
        pos = (math.sin(t * 1.6) + 1) / 2  # 0..1
        cursor_x = bar.left + int((bar.width - cursor_w) * pos)
        cursor = pygame.Rect(cursor_x, bar.top, cursor_w, bar.height)
        glow = pygame.Surface(cursor.size, pygame.SRCALPHA)
        glow.fill((*PHOSPHOR, 70))
        self.screen.blit(glow, cursor.topleft)
        pygame.draw.rect(self.screen, PHOSPHOR, cursor, 2)
        # Dots
        dots = "." * (1 + int(t * 3) % 4)
        draw_text(self.screen, self.theme.f_med,
                  f"SCANNING{dots}",
                  (zone.left, zone.top + 200), color=AMBER)
        # Trame ASCII
        elapsed = time.time() - self.detect_started_at
        draw_text(self.screen, self.theme.f_tiny,
                  f"elapsed = {elapsed:5.1f}s   port_count = {len(ALL_PORTS)}   "
                  f"sample_rate = {SAMPLING_HZ} Hz",
                  (zone.left, zone.top + 250), color=TEXT_DIM)

    def _draw_detect_failure(self, zone, t):
        draw_text(self.screen, self.theme.f_xl,
                  "✗  AUCUN BITALINO DÉTECTÉ",
                  (zone.left, zone.top + 10), color=DANGER, glow=DANGER_DIM)
        draw_text(self.screen, self.theme.f_med,
                  "La carte n'a pas pu être ouverte sur la liaison Bluetooth.",
                  (zone.left, zone.top + 80), color=TEXT_HI)
        # Erreur brute
        err = self.detect_error or "(raison inconnue)"
        if len(err) > 90:
            err = err[:87] + "..."
        draw_text(self.screen, self.theme.f_small,
                  f"› {err}",
                  (zone.left, zone.top + 130), color=DANGER)
        # Suggestions
        tips = [
            "• Vérifiez que la carte est allumée et appairée en Bluetooth.",
            f"• Adresse utilisée : {self.address}",
            "• Approchez la carte de l'antenne, fermez les autres apps audio/BT.",
            "• Sinon, lancez le mode démo pour tester l'interface seule.",
        ]
        y = zone.top + 180
        for tip in tips:
            draw_text(self.screen, self.theme.f_small, tip,
                      (zone.left, y), color=TEXT_MID)
            y += 30

        # Boutons
        btn_w = max(280, zone.width // 3 - 20)
        btn_h = max(70, int(zone.height * 0.13))
        gap = 24
        total_w = btn_w * 2 + gap
        start_x = zone.left + (zone.width - total_w) // 2
        by = zone.bottom - btn_h - 30
        self.btn_retry.rect = pygame.Rect(start_x, by, btn_w, btn_h)
        self.btn_demo.rect  = pygame.Rect(start_x + btn_w + gap, by, btn_w, btn_h)
        self.btn_retry.draw(self.screen, self.theme.f_med_b, t)
        self.btn_demo.draw(self.screen, self.theme.f_med_b, t)

    # ── Écrans : étapes ────────────────────────────────────────────
    def _draw_intro(self, t):
        rect = self.layout.main
        draw_panel(self.screen, rect)
        draw_text(self.screen, self.theme.f_xl,
                  "PRÉPARATION DU CAPTEUR",
                  (rect.left + 30, rect.top + 24),
                  color=PHOSPHOR, glow=PHOSPHOR_DIM)
        lines = [
            "▸ Branchez l'accéléromètre 3 axes sur la carte BITalino.",
            "▸ Aucun choix de port n'est nécessaire :",
            "  les ports actifs sont identifiés AUTOMATIQUEMENT",
            "  pendant les phases 01–03 du calibrage.",
            "",
            "▸ Le calibrage produit le fichier  ›  calibration.json",
            "  qui sera lu par tetris.py pour mapper les flèches.",
            "",
            "  [ESC] pour quitter à tout moment.",
        ]
        y = rect.top + 110
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
        draw_panel(self.screen, rect)
        draw_text(self.screen, self.theme.f_xl, "01 // POSITION DE REPOS",
                  (rect.left + 30, rect.top + 24),
                  color=PHOSPHOR, glow=PHOSPHOR_DIM)
        draw_text(self.screen, self.theme.f_med,
                  "Prenez l'accéléromètre dans la main et tenez-le IMMOBILE.",
                  (rect.left + 30, rect.top + 90), color=TEXT_HI)
        draw_text(self.screen, self.theme.f_small,
                  "Les 6 ports sont scrutés simultanément ; les candidats",
                  (rect.left + 30, rect.top + 124), color=TEXT_DIM)
        draw_text(self.screen, self.theme.f_small,
                  "se révèlent par leur moyenne dans 200..820 et σ faible.",
                  (rect.left + 30, rect.top + 144), color=TEXT_DIM)

        scope = self._scope_rect(rect)
        with self.device.lock:
            buffers = [list(b) for b in self.device.live_buf]
        labels = [f"P{i+1}" for i in range(6)]
        draw_scope(self.screen, scope, buffers,
                   axis_labels=labels, theme=self.theme)
        self._draw_progress_or_button(t, rect, "[  OK, JE SUIS PRÊT  ]")

    def _draw_axis_step(self, t, axis):
        rect = self.layout.main
        draw_panel(self.screen, rect)
        if axis == "x":
            title = "02 // BALAYAGE  GAUCHE  ↔  DROITE"
            instr = "Bougez l'accéléromètre de GAUCHE à DROITE plusieurs fois."
            tip   = "Allez jusqu'aux amplitudes que vous utiliserez en jeu."
            highlight_idx = self.x_axis
        else:
            title = "03 // BALAYAGE  HAUT  ↕  BAS"
            instr = "Bougez l'accéléromètre de HAUT en BAS plusieurs fois."
            tip   = "Évitez de tourner sur l'axe X pendant ce balayage."
            highlight_idx = self.y_axis
        draw_text(self.screen, self.theme.f_xl, title,
                  (rect.left + 30, rect.top + 24),
                  color=PHOSPHOR, glow=PHOSPHOR_DIM)
        draw_text(self.screen, self.theme.f_med, instr,
                  (rect.left + 30, rect.top + 90), color=TEXT_HI)
        draw_text(self.screen, self.theme.f_small, tip,
                  (rect.left + 30, rect.top + 124), color=TEXT_DIM)
        if self.x_axis is not None and axis == "y":
            draw_text(self.screen, self.theme.f_small,
                      f"✓ X = PORT {self.x_axis + 1}   "
                      f"[{self.x_min}..{self.x_max}]",
                      (rect.left + 30, rect.top + 154), color=PHOSPHOR_MID)
        scope = self._scope_rect(rect)
        with self.device.lock:
            buffers = [list(b) for b in self.device.live_buf]
        labels = [f"P{i+1}" for i in range(6)]
        draw_scope(self.screen, scope, buffers,
                   highlight=highlight_idx,
                   axis_labels=labels, theme=self.theme)
        self._draw_progress_or_button(t, rect, "[  OK, COMMENCER LE BALAYAGE  ]")

    def _draw_deadzone(self, t):
        rect = self.layout.main
        draw_panel(self.screen, rect)

        # ── En-tête ───────────────────────────────────────────────
        content_top = rect.top + 24
        draw_text(self.screen, self.theme.f_xl, "04 // ZONE MORTE",
                  (rect.left + 30, content_top),
                  color=AMBER, glow=AMBER_DIM)
        draw_text(self.screen, self.theme.f_med,
                  "Plage centrale autour du repos où aucune flèche n'est déclenchée.",
                  (rect.left + 30, content_top + 60), color=TEXT_HI)
        draw_text(self.screen, self.theme.f_small,
                  "0.0 = très sensible          1.0 = pic du calibrage requis",
                  (rect.left + 30, content_top + 92), color=TEXT_DIM)

        # ── Zone de contenu sous l'en-tête ───────────────────────
        body_top = content_top + 130
        body_h   = rect.bottom - body_top - 20

        # Colonne gauche : radar (carré, prend toute la hauteur dispo)
        radar_size = min(body_h, rect.width // 2 - 50)
        radar_size = max(160, radar_size)
        radar = pygame.Rect(rect.left + 30, body_top, radar_size, radar_size)
        pygame.draw.rect(self.screen, BG_DEEP, radar)
        pygame.draw.rect(self.screen, PHOSPHOR_DIM, radar, 1)
        draw_corner_brackets(self.screen, radar, color=PHOSPHOR_DIM, length=14)

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

        # Colonne droite : infos + slider + bouton
        right_x  = radar.right + 40
        right_w  = rect.right - right_x - 20
        right_y  = body_top

        # Tableau des axes détectés
        rows = [
            ("AXE  X", f"PORT {self.x_axis + 1}",
             f"[{self.x_min} .. {self.x_max}]"),
            ("AXE  Y", f"PORT {self.y_axis + 1}",
             f"[{self.y_min} .. {self.y_max}]"),
            ("AXE  Z", (f"PORT {self.z_axis + 1}"
                        if self.z_axis is not None else "N/A"), ""),
        ]
        draw_text(self.screen, self.theme.f_tiny, "// AXES DÉTECTÉS",
                  (right_x, right_y), color=PHOSPHOR_MID)
        row_h = max(44, body_h // 10)
        for i, (a, b, c) in enumerate(rows):
            yy = right_y + 26 + i * row_h
            draw_text(self.screen, self.theme.f_med_b, a,
                      (right_x, yy), color=TEXT_HI)
            draw_text(self.screen, self.theme.f_med, b,
                      (right_x + 120, yy), color=PHOSPHOR)
            if c:
                draw_text(self.screen, self.theme.f_small, c,
                          (right_x + 120, yy + 22), color=TEXT_DIM)

        # Slider : positionné entre la table et le bouton
        btn_h   = max(60, int(body_h * 0.17))
        btn_top = rect.bottom - btn_h - 24
        # slider_cy = mi-chemin entre fin du tableau et haut du bouton
        table_bottom = right_y + 26 + len(rows) * row_h + 10
        slider_cy = (table_bottom + btn_top) // 2
        slider_margin = 16
        self.slider.rect = pygame.Rect(right_x + slider_margin,
                                       slider_cy,
                                       right_w - 2 * slider_margin, 6)
        draw_text(self.screen, self.theme.f_tiny, "DEAD ZONE",
                  (right_x + slider_margin, slider_cy - 46), color=AMBER)
        self.slider.draw(self.screen, self.theme.f_big, self.theme.f_tiny)

        # Boutons : [RECOMMENCER] à gauche, [VALIDER] à droite
        btn_gap = 10
        btn_each = (right_w - btn_gap) // 2
        self.btn_recalibrate.rect = pygame.Rect(right_x, btn_top, btn_each, btn_h)
        self.btn_recalibrate.draw(self.screen, self.theme.f_small, t)
        self.btn_main.rect = pygame.Rect(right_x + btn_each + btn_gap, btn_top,
                                         right_w - btn_each - btn_gap, btn_h)
        self.btn_main.accent = AMBER
        self.btn_main.draw(self.screen, self.theme.f_med_b, t)

    def _draw_done(self, t):
        rect = self.layout.main
        draw_panel(self.screen, rect)
        draw_text_centered(self.screen, self.theme.f_huge, "✓ TERMINÉ",
                           (rect.centerx, rect.top + 90),
                           color=PHOSPHOR, glow=PHOSPHOR_DIM)
        draw_text_centered(self.screen, self.theme.f_med,
                           "calibration.json écrit dans le dossier courant.",
                           (rect.centerx, rect.top + 180), color=TEXT_HI)
        x_rest = statistics.mean(s[self.x_axis] for s in self.rest_samples)
        y_rest = statistics.mean(s[self.y_axis] for s in self.rest_samples)
        rows = [
            ("PORTS",
             f"X = P{self.x_axis + 1}    Y = P{self.y_axis + 1}    "
             f"Z = {'P' + str(self.z_axis + 1) if self.z_axis is not None else 'N/A'}"),
            ("REPOS",  f"X={x_rest:.0f}    Y={y_rest:.0f}"),
            ("AMPL X", f"{self.x_min} .. {self.x_max}"),
            ("AMPL Y", f"{self.y_min} .. {self.y_max}"),
            ("DEAD Z", f"{self.slider.value:.2f}"),
        ]
        y = rect.top + 240
        for a, b in rows:
            draw_text(self.screen, self.theme.f_med_b, a,
                      (rect.left + 200, y), color=AMBER)
            draw_text(self.screen, self.theme.f_med, b,
                      (rect.left + 380, y), color=TEXT_HI)
            y += 42
        btn_w = max(280, rect.width // 3)
        bx = rect.left + (rect.width - btn_w) // 2
        self.btn_main.rect = pygame.Rect(bx, rect.bottom - 100, btn_w, 75)
        self.btn_main.accent = PHOSPHOR
        self.btn_main.draw(self.screen, self.theme.f_med_b, t)

    # ── Helpers de placement ───────────────────────────────────────
    def _scope_rect(self, panel):
        top    = panel.top + 200
        bottom = panel.bottom - 130
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
            total = REST_SECONDS if self.state == STATE_REST else MOVE_SECONDS
            frac = 1.0 - max(0, remaining) / total
            bar_w = rect.width - 120
            bar = pygame.Rect(rect.left + (rect.width - bar_w) // 2,
                              rect.bottom - 100, bar_w, 28)
            pygame.draw.rect(self.screen, BG_PANEL_HI, bar)
            pygame.draw.rect(self.screen, AMBER,
                             pygame.Rect(bar.left, bar.top,
                                         int(bar.width * frac), bar.height))
            pygame.draw.rect(self.screen, AMBER, bar, 2)
            txt = f"REC  ─  {remaining:0.1f} s"
            t_surf = self.theme.f_xl.render(txt, True, AMBER)
            self.screen.blit(t_surf,
                             (bar.centerx - t_surf.get_width() // 2,
                              bar.top - 70))
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
