"""Экран-лицо Карен: зелёные эмоции на чёрном мониторе, волна как стабильное состояние.

Использование из другого потока:
    face = Face()
    face.set_emotion("angry")    # neutral | happy | angry | bored | surprised | sad
    face.set_mode("speaking")    # idle | listening | thinking | speaking
    face.set_level(0.7)          # громкость 0..1 (двигает волну)
    face.run()                   # окно; только из главного потока

Демо: python face.py  (клавиши 1-6, пробел — «говорит», Esc — выход)
"""
import math
import random
import threading
import time

import pygame

EMOTIONS = ("neutral", "happy", "angry", "bored", "surprised", "sad")
MODES = ("idle", "listening", "thinking", "speaking")

BG = (74, 92, 132)
BEZEL = (143, 160, 196)
BEZEL_DARK = (101, 118, 156)
OUTLINE = (20, 22, 30)
SCREEN = (8, 10, 9)
# слои свечения: широкий тусклый -> узкий яркий
GLOW = ((10, 55, 14), (45, 150, 45), (150, 255, 120))
GLOW_MULT = (3.2, 1.9, 1.0)


# ---------- геометрия в координатах лица (x вправо, y вниз, ~[-1..1]) ----------
def arc(cx, cy, rx, ry, a0, a1, n=40):
    """Дуга эллипса; углы в градусах, 0 = вправо, 90 = вверх."""
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        pts.append((cx + rx * math.cos(a), cy - ry * math.sin(a)))
    return pts


def ellipse(cx, cy, rx, ry, n=60):
    return arc(cx, cy, rx, ry, 0, 360, n)[:-1]


def clamp_below(pts, line):
    """Срезает фигуру линией сверху: y = max(y, line(x)) — веко или бровь."""
    return [(x, max(y, line(x))) for x, y in pts]


def line_through(p, q):
    (x0, y0), (x1, y1) = p, q
    k = (y1 - y0) / (x1 - x0)
    return lambda x: y0 + k * (x - x0)


# ---------- описания лиц: список примитивов ----------
# ("line", pts, width, closed) / ("fill", pts) / ("hole", (x, y), r)
def face_happy():
    prims = []
    for cx in (-0.5, 0.5):
        prims.append(("line", arc(cx, -0.05, 0.22, 0.24, 0, 180), 0.05, False))
    mouth = [(-0.3, 0.2)] + arc(0, 0.2, 0.3, 0.3, 180, 360)[1:-1] + [(0.3, 0.2)]
    prims.append(("fill", mouth))
    return prims


def _filled_eyes_with_brows(eye_y, brows, pupils, eye_r=(0.17, 0.2)):
    prims = []
    for (cx, py_off), (b0, b1), (px, py) in zip(((-0.45, 0), (0.45, 0)), brows, pupils):
        cut = line_through(b0, b1)
        eye = clamp_below(ellipse(cx, eye_y, *eye_r), lambda x, c=cut: c(x) + 0.06)
        prims.append(("fill", eye))
        prims.append(("hole", (px, py), 0.05))
        prims.append(("line", [b0, b1], 0.055, False))
    return prims


def face_angry():
    brows = (((-0.74, -0.27), (-0.2, -0.05)), ((0.2, -0.05), (0.74, -0.27)))
    prims = _filled_eyes_with_brows(0.04, brows, ((-0.5, 0.1), (0.4, 0.1)))
    prims.append(("line", arc(0, 0.62, 0.26, 0.17, 25, 155), 0.045, False))
    return prims


def face_bored():
    prims = []
    lid = -0.06
    for cx in (-0.45, 0.45):
        eye = clamp_below(ellipse(cx, 0.0, 0.21, 0.23), lambda x: lid)
        prims.append(("line", eye, 0.045, True))
        prims.append(("line", [(cx - 0.3, lid), (cx + 0.3, lid)], 0.05, False))
    prims.append(("line", [(-0.09, 0.45), (0.09, 0.45)], 0.035, False))
    return prims


def face_surprised():
    prims = []
    for cx in (-0.42, 0.42):
        prims.append(("line", ellipse(cx, -0.1, 0.16, 0.32), 0.045, True))
    mouth = arc(0, 0.68, 0.23, 0.27, 0, 180)
    prims.append(("fill", mouth))
    return prims


def face_sad():
    # брови домиком: внутренний конец выше
    brows = (((-0.72, -0.1), (-0.24, -0.3)), ((0.24, -0.3), (0.72, -0.1)))
    prims = _filled_eyes_with_brows(0.08, brows, ((-0.39, 0.17), (0.51, 0.17)), (0.16, 0.2))
    prims.append(("line", arc(0, 0.62, 0.17, 0.12, 25, 155), 0.04, False))
    return prims


FACES = {
    "happy": face_happy(),
    "angry": face_angry(),
    "bored": face_bored(),
    "surprised": face_surprised(),
    "sad": face_sad(),
}


class Face:
    def __init__(self, size=(640, 480), fps=30, title="Karen", fullscreen=None, bezel=None):
        """fullscreen/bezel по умолчанию берутся из KAREN_FULLSCREEN=1 и KAREN_BEZEL=0."""
        import os
        self.size, self.fps, self.title = size, fps, title
        self.fullscreen = os.environ.get("KAREN_FULLSCREEN") == "1" if fullscreen is None else fullscreen
        self.bezel = os.environ.get("KAREN_BEZEL", "1") != "0" if bezel is None else bezel
        self._lock = threading.Lock()
        self._emotion = "neutral"
        self._emotion_until = None   # когда вернуться к волне
        self._changed_at = 0.0
        self._mode = "idle"
        self._target_level = 0.0
        self._level = 0.0
        self._running = False

    # ---------- API (потокобезопасно) ----------
    def set_emotion(self, emotion, hold=None):
        """hold=None — держать, пока не сменят; hold=N — вернуться к волне через N сек."""
        if emotion not in EMOTIONS:
            emotion = "neutral"
        with self._lock:
            if emotion != self._emotion:
                self._changed_at = time.time()
            self._emotion = emotion
            self._emotion_until = None if hold is None else time.time() + hold

    def relax(self, after=3.0):
        """Вернуться к волне через `after` секунд (если эмоцию не сменят раньше)."""
        with self._lock:
            if self._emotion != "neutral":
                self._emotion_until = time.time() + after

    def set_mode(self, mode):
        with self._lock:
            self._mode = mode if mode in MODES else "idle"
            if self._mode in ("idle", "thinking"):
                self._target_level = 0.0

    def set_level(self, level):
        with self._lock:
            self._target_level = max(0.0, min(1.0, float(level)))

    @property
    def emotion(self):
        with self._lock:
            return self._emotion

    def close(self):
        self._running = False

    # ---------- отрисовка ----------
    def _layout(self, w, h):
        if not self.bezel:   # настоящий экран сам и есть монитор — рисуем лицо на всю площадь
            full = pygame.Rect(0, 0, w, h)
            return full, full
        m = int(min(w, h) * 0.06)
        bezel = pygame.Rect(m, m, w - 2 * m, h - 2 * m)
        bw = int(min(bezel.w, bezel.h) * 0.07)
        screen = bezel.inflate(-2 * bw, -2 * bw)
        return bezel, screen

    def _draw_monitor(self, surf, bezel, screen):
        surf.fill(BG)
        r = int(min(bezel.w, bezel.h) * 0.08)
        pygame.draw.rect(surf, OUTLINE, bezel.inflate(6, 6), border_radius=r + 3)
        pygame.draw.rect(surf, BEZEL_DARK, bezel, border_radius=r)
        pygame.draw.rect(surf, BEZEL, bezel.inflate(-8, -8), border_radius=r - 2)
        # заклёпки
        for x in (bezel.left + r // 2 + 4, bezel.right - r // 2 - 4):
            for y in (bezel.top + r // 2 + 4, bezel.bottom - r // 2 - 4):
                pygame.draw.circle(surf, BEZEL_DARK, (x, y), max(2, r // 7))
        pygame.draw.rect(surf, OUTLINE, screen.inflate(6, 6), border_radius=r // 2 + 2)
        pygame.draw.rect(surf, SCREEN, screen, border_radius=r // 2)

    def _to_px(self, screen):
        s = min(screen.w / 2.3, screen.h / 1.75)
        cx, cy = screen.centerx, screen.centery
        return s, (lambda p: (cx + p[0] * s, cy + p[1] * s))

    @staticmethod
    def _thick(surf, color, pts, width, closed):
        if len(pts) < 2:
            return
        w = max(1, int(width))
        if closed:
            pts = pts + [pts[0]]
        pygame.draw.lines(surf, color, False, pts, w)
        r = w / 2
        if r >= 1.5:
            for p in pts:
                pygame.draw.circle(surf, color, p, r)

    def _draw_prims(self, surf, prims, screen, bright):
        s, px = self._to_px(screen)
        colors = [tuple(int(c * bright) for c in col) for col in GLOW]
        for layer in range(3):
            col, mult = colors[layer], GLOW_MULT[layer]
            for prim in prims:
                kind = prim[0]
                if kind == "line":
                    _, pts, width, closed = prim
                    self._thick(surf, col, [px(p) for p in pts], width * s * mult, closed)
                elif kind == "fill":
                    pts = [px(p) for p in prim[1]]
                    if layer < 2:
                        self._thick(surf, col, pts, 0.04 * s * mult, True)
                    else:
                        pygame.draw.polygon(surf, col, pts)
                        self._thick(surf, col, pts, 0.03 * s, True)
        for prim in prims:
            if prim[0] == "hole":
                _, c, r = prim
                pygame.draw.circle(surf, SCREEN, px(c), r * s)

    def _wave_prims(self, t, mode, level):
        half = 1.15
        n = 140
        pts = []
        for i in range(n + 1):
            x = -half + 2 * half * i / n
            taper = math.cos(x / half * math.pi / 2) ** 1.5   # к краям волна гаснет
            if mode == "thinking":
                # бегущий импульс — «думает»
                c = math.sin(t * 2.2) * 0.8
                env = math.exp(-((x - c) ** 2) / 0.05)
                y = 0.12 * env * math.sin(28 * x - t * 18)
            else:
                amp = 0.025 + 0.42 * level
                y = amp * taper * (
                    math.sin(9 * x - t * 6)
                    + 0.45 * level * math.sin(23 * x + t * 11)
                    + 0.25 * level * math.sin(41 * x - t * 17)
                ) / (1 + 0.7 * level)
            pts.append((x, y))
        return [("line", pts, 0.03, False)]

    def _scanlines(self, size):
        surf = pygame.Surface(size, pygame.SRCALPHA)
        for y in range(0, size[1], 3):
            pygame.draw.line(surf, (0, 0, 0, 70), (0, y), (size[0], y))
        return surf

    def render(self, surf, t=None):
        t = time.time() if t is None else t
        with self._lock:
            if self._emotion_until is not None and t >= self._emotion_until:
                self._emotion, self._emotion_until = "neutral", None
                self._changed_at = t
            emotion, mode, changed = self._emotion, self._mode, self._changed_at
            self._level += (self._target_level - self._level) * 0.35
            level = self._level

        w, h = surf.get_size()
        bezel, screen = self._layout(w, h)
        if self.bezel:
            self._draw_monitor(surf, bezel, screen)
        else:
            surf.fill(SCREEN)

        # включение после смены лица + лёгкое мерцание ЭЛТ + пульс от голоса
        fade = min(1.0, (t - changed) / 0.15)
        bright = fade * (0.93 + 0.07 * math.sin(t * 50)) * (1.0 if emotion == "neutral" else 0.9 + 0.15 * level)
        bright = min(1.0, bright)

        prims = FACES.get(emotion) or self._wave_prims(t, mode, level)
        surf.set_clip(screen)
        self._draw_prims(surf, prims, screen, bright)
        if getattr(self, "_scan", None) is None or self._scan.get_size() != screen.size:
            self._scan = self._scanlines(screen.size)
        surf.blit(self._scan, screen.topleft)
        surf.set_clip(None)

    def run(self, on_key=None):
        """Главный цикл окна. Блокирует до закрытия окна или close()."""
        pygame.init()
        if self.fullscreen:
            win = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)   # родное разрешение экрана
            pygame.mouse.set_visible(False)
        else:
            win = pygame.display.set_mode(self.size, pygame.RESIZABLE)
        pygame.display.set_caption(self.title)
        clock = pygame.time.Clock()
        self._running = True
        while self._running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                    self._running = False
                elif ev.type == pygame.KEYDOWN and on_key:
                    on_key(ev)
            self.render(win)
            pygame.display.flip()
            clock.tick(self.fps)
        pygame.display.quit()


def snapshot(out_dir):
    """Сохраняет PNG каждого лица — для проверки без окна."""
    import os
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    os.makedirs(out_dir, exist_ok=True)
    surf = pygame.Surface((640, 480))
    for emo in EMOTIONS:
        f = Face()
        f.set_emotion(emo)
        f._changed_at = 0
        if emo == "neutral":
            f.set_mode("speaking")
            f._level = f._target_level = 0.6
        f.render(surf, t=10.0)
        pygame.image.save(surf, os.path.join(out_dir, f"{emo}.png"))


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "--snapshot":
        snapshot(sys.argv[2])
        sys.exit()

    face = Face()
    keys = {pygame.K_1 + i: e for i, e in enumerate(("happy", "angry", "bored", "surprised", "sad", "neutral"))}
    talking = {"on": False}

    def on_key(ev):
        if ev.key in keys:
            face.set_emotion(keys[ev.key])
        elif ev.key == pygame.K_SPACE:
            talking["on"] = not talking["on"]
            face.set_mode("speaking" if talking["on"] else "idle")
        elif ev.key == pygame.K_t:
            face.set_mode("thinking")

    def fake_voice():
        while True:
            if talking["on"]:
                face.set_level(random.random() ** 0.7)
            time.sleep(0.08)

    threading.Thread(target=fake_voice, daemon=True).start()
    print("1 радость, 2 злость, 3 скука, 4 удивление, 5 грусть, 6 волна; пробел — говорит, t — думает")
    face.run(on_key)
