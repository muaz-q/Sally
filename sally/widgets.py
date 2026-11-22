"""Sally's live desktop widgets.

Each widget is a small frameless, see-through window pinned to the bottom of the window
stack, so it sits on the wallpaper like part of it and every normal window covers it.

    Stage      the cover and its moving neon rings (ignores the mouse entirely)
    Player     shuffle / previous / play-pause / next / repeat and the seek bar
    Header     the "Sally" logo, settings, hide and quit
    Lyrics     the previous, current and next lyric line
    TopTracks  your top tracks on a slowly turning spiral; click one to play it
"""
import bisect
import ctypes
import math
import os
import random
import time
from collections import OrderedDict

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetricsF, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtQml import QQmlImageProviderBase
from PySide6.QtQuick import QQuickImageProvider, QQuickView, QQuickWindow
from PySide6.QtWidgets import QWidget

from . import layout as L

ICON_FONT = "Segoe Fluent Icons"
GLYPH = {
    "shuffle": "", "prev": "", "next": "", "play": "", "pause": "",
    "repeat": "", "repeat_one": "", "settings": "", "minimize": "",
    "close": "", "up": "", "down": "", "right": "",
}
FRAME = 16          # ms between animation frames (~60 fps)
IDLE_FRAME = 250    # ms between ticks while a full-screen window hides the desktop
WHITE = (255, 255, 255)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def qcolor(rgb, alpha=255):
    return QColor(rgb[0], rgb[1], rgb[2], max(0, min(255, round(alpha))))


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def smoothstep(edge0, edge1, x):
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


def font(px, weight=QFont.Weight.Normal, family="Segoe UI"):
    f = QFont(family)
    f.setPixelSize(max(1, round(px)))
    f.setWeight(weight)
    return f


def icon_font(px):
    f = QFont(ICON_FONT)
    f.setPixelSize(max(1, round(px)))
    return f


def pil_to_qimage(img):
    """Pillow image -> QImage that owns its pixels. Safe to call off the GUI thread."""
    rgba = img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    return QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()


def square_pixmap(image, side, dpr):
    """Crop to a square and scale once, so painting each frame is a plain copy."""
    px = max(1, round(side * dpr))
    pix = QPixmap.fromImage(image.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                         Qt.TransformationMode.SmoothTransformation))
    if pix.width() != px or pix.height() != px:
        pix = pix.copy((pix.width() - px) // 2, (pix.height() - px) // 2, px, px)
    pix.setDevicePixelRatio(dpr)
    return pix


def glyph(p, rect, name, px, color):
    p.setFont(icon_font(px))
    p.setPen(color)
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, GLYPH[name])


def wave_icon(p, cx, cy, height, u, color):
    """The little equaliser mark next to "Sally" and "Lyrics"."""
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    bar, gap = 2.2 * u, 2.0 * u
    heights = (0.35, 0.7, 1.0, 0.55, 0.85, 0.4)
    x = cx - (len(heights) * (bar + gap) - gap) / 2
    for h in heights:
        bh = height * h
        p.drawRoundedRect(QRectF(x, cy - bh / 2, bar, bh), bar / 2, bar / 2)
        x += bar + gap
    p.restore()


def glass(p, rect, radius, u, pal, border=1.0):
    """Dark tinted panel with a thin glowing edge in the song's colours. The wallpaper behind
    every panel is already blurred, so a translucent fill reads as frosted glass."""
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.fillPath(path, QColor(20, 16, 40, 150))
    for width, alpha in ((4.0 * u, 40 * border), (1.2 * u, 190 * border)):
        g = QLinearGradient(rect.topLeft(), rect.bottomRight())
        g.setColorAt(0.0, qcolor(pal[2], alpha))
        g.setColorAt(0.5, qcolor(pal[0], alpha * 0.7))
        g.setColorAt(1.0, qcolor(pal[1], alpha))
        p.setPen(QPen(QBrush(g), width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


def spark(p, centre, radius, rgb, alpha=1.0):
    g = QRadialGradient(centre, radius)
    g.setColorAt(0.0, qcolor(WHITE, 255 * alpha))
    g.setColorAt(0.22, qcolor(mix(WHITE, rgb, 0.4), 210 * alpha))
    g.setColorAt(0.55, qcolor(rgb, 70 * alpha))
    g.setColorAt(1.0, qcolor(rgb, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(centre, radius, radius)


def fmt_time(ms):
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


def precise_timer(parent, interval, slot):
    """Windows' default timers can fire up to ~15 ms late, which reads as stutter."""
    timer = QTimer(parent)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(slot)
    timer.start(interval)
    return timer


def blank(width, height, dpr):
    img = QImage(max(1, math.ceil(width * dpr)), max(1, math.ceil(height * dpr)),
                 QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.GlobalColor.transparent)
    return img


# ─────────────────────────────────────────────
# WINDOWS DESKTOP PLUMBING
# ─────────────────────────────────────────────
user32 = ctypes.windll.user32
_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.FindWindowExW.restype = ctypes.c_void_p
user32.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.FindWindowW.restype = ctypes.c_void_p
user32.GetWindow.restype = ctypes.c_void_p
user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
GWLP_HWNDPARENT = -8


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def desktop_host():
    """The top-level window holding the desktop icons (Progman, or a WorkerW on some builds)."""
    found = []

    def check(hwnd, _):
        if user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            found.append(hwnd)
            return False
        return True
    user32.EnumWindows(_WNDENUMPROC(check), None)
    return found[0] if found else user32.FindWindowW("Progman", None)


def pin_to_desktop(hwnd, host):
    """Make the desktop the window's owner. Windows always keeps owned windows above their
    owner, so "show desktop" (Win+D, the three-finger swipe) can no longer bury the widget
    under the desktop, and nothing can push it below the wallpaper."""
    if host and user32.GetWindowLongPtrW(hwnd, GWLP_HWNDPARENT) != host:
        user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, host)


def _cloaked(hwnd):
    value = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 14, ctypes.byref(value), 4)
    return value.value != 0


def window_hidden(hwnd):
    """True when a single other window completely covers this one, so animating it only
    burns CPU. Minimised, cloaked (other virtual desktop) and click-through overlay windows
    don't count."""
    if not hwnd:
        return False
    mine = RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(mine))
    h = user32.GetWindow(hwnd, 3)  # GW_HWNDPREV: the next window above
    while h:
        if user32.IsWindowVisible(ctypes.c_void_p(h)) and not user32.IsIconic(ctypes.c_void_p(h)) \
                and not (user32.GetWindowLongW(ctypes.c_void_p(h), -20) & 0x20) and not _cloaked(h):
            r = RECT()
            user32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))
            if r.left <= mine.left and r.top <= mine.top and r.right >= mine.right and r.bottom >= mine.bottom:
                return True
        h = user32.GetWindow(h, 3)
    return False


def wallpaper_layer():
    """The window the wallpaper is painted in, which sits *under* the desktop icons. Asking
    Progman for message 0x052C makes Explorer create it (live-wallpaper apps use the same
    trick). Windows 11 24H2+ keeps it as a child of Progman; older builds as a separate
    top-level WorkerW right behind the icons."""
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW(ctypes.c_void_p(progman), 0x052C, 0xD, 0x1, 0, 1000, ctypes.byref(result))
    layer = user32.FindWindowExW(progman, None, "WorkerW", None)
    if layer:
        return layer
    host = desktop_host()
    return user32.FindWindowExW(None, host, "WorkerW", None) if host else None


# ─────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────
class Theme(QObject):
    """The current song's colours. The Stage eases it from one song to the next."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.palette = list(L.DEFAULT_PALETTE)

    def set(self, palette):
        self.palette = list(palette)
        self.changed.emit()


class Clock:
    """Song position between polls: Spotify is asked every few seconds, the bar moves smoothly."""

    def __init__(self):
        self.track = None
        self.base = 0
        self.at = time.monotonic()

    def set(self, track):
        self.track = track
        self.base = track["progress_ms"] if track else 0
        self.at = time.monotonic()

    def seek(self, ms):
        self.base, self.at = ms, time.monotonic()

    def set_playing(self, playing):
        if self.track:
            self.base, self.at = self.progress(), time.monotonic()
            self.track = {**self.track, "is_playing": playing}

    def progress(self):
        t = self.track
        if not t:
            return 0
        ms = self.base + ((time.monotonic() - self.at) * 1000 if t["is_playing"] else 0)
        return min(ms, t["duration_ms"]) if t["duration_ms"] else ms


# ─────────────────────────────────────────────
# BASE WINDOW
# ─────────────────────────────────────────────
class DesktopWidget(QWidget):
    def __init__(self, theme, clickable=True):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowStaysOnBottomHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        if not clickable:
            flags |= Qt.WindowType.WindowTransparentForInput
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.theme = theme
        self.u = 1.0
        self.hover = None
        theme.changed.connect(self.update)

    def place(self, rect, u):
        self.u = u
        self.setGeometry(rect.toAlignedRect())
        self.update()

    def send_to_bottom(self):
        """Just above the desktop: as the desktop's owned window it can't sink any lower."""
        hwnd = int(self.winId())
        pin_to_desktop(hwnd, desktop_host())
        # HWND_BOTTOM, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)

    def showEvent(self, event):
        super().showEvent(event)
        self.send_to_bottom()
        QTimer.singleShot(0, self.send_to_bottom)

    def mouseReleaseEvent(self, event):
        self.send_to_bottom()

    def leaveEvent(self, event):
        self.hover = None
        self.update()

    def hit(self, pos):
        """Name of the button under pos. Subclasses list (name, centre, radius) in buttons()."""
        for name, centre, radius in self.buttons():
            if (pos.x() - centre.x()) ** 2 + (pos.y() - centre.y()) ** 2 <= radius ** 2:
                return name
        return None

    def buttons(self):
        return []

    def mouseMoveEvent(self, event):
        hover = self.hit(event.position())
        if hover != self.hover:
            self.hover = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor)
            self.update()


# ─────────────────────────────────────────────
# STAGE: cover + moving rings + the song-change "injection"
# ─────────────────────────────────────────────
class Ring:
    def __init__(self, a, b, angle, spin, phase, dot_speed, strength=1.0):
        self.a, self.b, self.angle, self.spin = a, b, angle, spin
        self.phase, self.dot_speed, self.strength = phase, dot_speed, strength


RINGS = [
    Ring(300, 92, 35, 4.0, 0.0, 0.55),
    Ring(295, 100, -33, -3.2, 1.7, -0.45),
    Ring(232, 82, 112, 2.6, 3.1, 0.7),
    Ring(262, 66, 78, -4.8, 4.4, -0.6, 0.65),
    Ring(205, 58, 4, 6.0, 5.2, 0.8, 0.5),
]
STRANDS = [(7, 5, 1.6, 0.35), (-6, -4, -1.3, 0.3)]   # extra thin copies: da, db, angle, alpha

INJECT_PALETTE = 1.2     # seconds for the rings to take the new song's colours
INJECT_PARTICLES = 1.25  # particles keep spawning this long
REVEAL = (0.65, 2.3)     # the new cover swirls in between these times
INJECT_DONE = 2.6        # then the wallpaper is allowed to change
FADE_FROM, FADE_TO = 100, L.STAGE_BOTTOM - 2  # rings and covers fade out over this band below the cover centre


def ring_sprite(ring, pal, u, dpr):
    """One ring with its glow and thin side strands, drawn once per colour scheme. The
    graphics card then rotates and squashes this picture every frame for free."""
    a, b = ring.a * u, ring.b * u
    pad = 14 * u
    w, h = 2 * (a + pad), 2 * (b + 8 * u + pad)
    img = blank(w, h, dpr)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
    p.translate(w / 2, h / 2)
    s = ring.strength

    def pen(alpha, width):
        g = QLinearGradient(-a, 0, a, 0)  # dim tail on one side, bright head on the other
        g.setColorAt(0.0, qcolor(pal[1], 25 * alpha))
        g.setColorAt(0.5, qcolor(pal[0], 140 * alpha))
        g.setColorAt(1.0, qcolor(pal[2], 255 * alpha))
        return QPen(QBrush(g), width)

    p.setBrush(Qt.BrushStyle.NoBrush)
    for width, alpha in ((10, 0.10), (4.5, 0.24), (1.7, 0.95)):
        p.setPen(pen(alpha * s, width * u))
        p.drawEllipse(QPointF(0, 0), a, b)
    for da, db, dangle, alpha in STRANDS:
        p.rotate(dangle)
        p.setPen(pen(alpha * s, 0.9 * u))
        p.drawEllipse(QPointF(0, 0), a + da * u, b + db * u)
        p.rotate(-dangle)
    p.end()
    return img, w, h


SPARK_PX = 64


def spark_sprite(rgb, dpr):
    img = blank(SPARK_PX, SPARK_PX, dpr)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    spark(p, QPointF(SPARK_PX / 2, SPARK_PX / 2), SPARK_PX / 2, rgb)
    p.end()
    return img


def rounded_cover(image, side, radius, dpr):
    """The cover with its rounded corners already cut out (transparent)."""
    square = square_pixmap(image, side, dpr)
    img = blank(side, side, dpr)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, side, side), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(QPointF(0, 0), square)
    p.end()
    return img


class Pictures(QQuickImageProvider):
    """Hands Python-drawn pictures to the QML scene as image://sally/<id>."""

    def __init__(self):
        super().__init__(QQmlImageProviderBase.ImageType.Image)
        self.store = {}

    def requestImage(self, name, size, requested):
        img = self.store.get(name) or QImage()
        if not img.isNull():
            size.setWidth(img.width())
            size.setHeight(img.height())
        return img


class Stage(QQuickView):
    """The cover and its rings, rendered on the graphics card (stage.qml). Drawing the same
    rotating rings with QPainter cost well over a full CPU core at 60 fps; this costs a
    small fraction, so the rings can keep moving all the time."""
    finished = Signal()  # a song-change animation ended; the new wallpaper may go up now

    def __init__(self, theme):
        QQuickWindow.setDefaultAlphaBuffer(True)
        super().__init__()
        self.theme = theme
        self.u = 1.0
        self.setColor(QColor(0, 0, 0, 0))
        self.setFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                      | Qt.WindowType.WindowStaysOnBottomHint | Qt.WindowType.WindowDoesNotAcceptFocus
                      | Qt.WindowType.WindowTransparentForInput)
        self.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        self.pictures = Pictures()
        self.engine().addImageProvider("sally", self.pictures)
        self.setSource(QUrl.fromLocalFile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage.qml")))
        self.root = self.rootObject()
        if self.root is None:
            raise RuntimeError("stage.qml failed to load: " + "; ".join(e.toString() for e in self.errors()))

        self.serial = 0
        self.old_image = self.new_image = None
        self.pal_from = self.pal_to = list(L.DEFAULT_PALETTE)
        self.sets = OrderedDict()            # palette -> {"rings": [ids], "sparks": [ids]}
        self.inject_at = None
        self.particles = []
        self.timer = precise_timer(self, FRAME, self.tick)
        self.timer.stop()                    # only runs during a song change

    # ── pictures ─────────────────────────────
    def put(self, prefix, image):
        self.serial += 1
        name = f"{prefix}-{self.serial}"
        self.pictures.store[name] = image
        return "image://sally/" + name

    def drop(self, urls):
        for url in urls:
            self.pictures.store.pop(url.rsplit("/", 1)[-1], None)

    def sprite_set(self, pal):
        key = tuple(tuple(c) for c in pal)
        if key not in self.sets:
            dpr = self.devicePixelRatio()
            self.sets[key] = {
                "rings": [self.put("ring", ring_sprite(r, pal, self.u, dpr)[0]) for r in RINGS],
                "sparks": [self.put("spark", spark_sprite(c, dpr)) for c in pal],
            }
            while len(self.sets) > 3:
                _, old = self.sets.popitem(last=False)
                self.drop(old["rings"] + old["sparks"])
        else:
            self.sets.move_to_end(key)
        return self.sets[key]

    def show_palette(self, pal, slot):
        """slot "A" is what's on screen; "B" is faded in over it during a song change."""
        s = self.sprite_set(pal)
        self.root.setProperty("src" + slot, s["rings"])
        self.root.setProperty("spark" + slot, s["sparks"][2])

    def cover_side(self):
        return L.COVER * self.u + 2  # 1px over on each side hides the wallpaper's own cover edge

    def show_cover(self, image):
        if image is None:
            self.root.setProperty("cover", "")
            return
        old = self.root.property("cover")
        side = self.cover_side()
        self.root.setProperty("cover", self.put("cover", rounded_cover(
            image, side, L.COVER_RADIUS * self.u + 1, self.devicePixelRatio())))
        if old:
            self.drop([old])

    # ── window ───────────────────────────────
    def place(self, rect, u):
        self.u = u
        self.setGeometry(rect.toAlignedRect())
        for s in self.sets.values():
            self.drop(s["rings"] + s["sparks"])
        self.sets.clear()
        r = self.root
        r.setProperty("u", u)
        r.setProperty("coverSide", self.cover_side())
        r.setProperty("rings", [{
            "a": g.a * u, "b": g.b * u, "angle": g.angle, "spin": g.spin, "phase": g.phase,
            "dot": g.dot_speed, "strength": g.strength,
            "w": 2 * (g.a + 14) * u, "h": 2 * (g.b + 8 + 14) * u,
        } for g in RINGS])
        height = rect.height() or 1
        r.setProperty("fadeFrom", min(1.0, (L.STAGE_HALF + FADE_FROM) * u / height))
        r.setProperty("fadeTo", min(1.0, (L.STAGE_HALF + FADE_TO) * u / height))
        self.show_palette(self.theme.palette, "A")
        r.setProperty("haloColor", qcolor(self.theme.palette[1]))
        self.show_cover(self.old_image)

    def send_to_bottom(self):
        hwnd = int(self.winId())
        pin_to_desktop(hwnd, desktop_host())
        user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)

    def showEvent(self, event):
        super().showEvent(event)
        self.send_to_bottom()
        QTimer.singleShot(0, self.send_to_bottom)

    def set_running(self, running):
        pass  # the graphics card animation is cheap enough to never pause

    # ── song change ──────────────────────────
    @property
    def busy(self):
        return self.inject_at is not None

    def inject(self, image, palette):
        """A new song's cover arrived: recolour the rings, then swirl the cover in."""
        if self.busy:
            self._finish(emit=False)  # jump the half-done change to its end, then start this one
        self.new_image = image
        self.pal_from, self.pal_to = list(self.theme.palette), list(palette)
        if not self.isVisible():
            self._finish()
            return
        new = self.sprite_set(self.pal_to)
        self.show_palette(self.pal_to, "B")
        self.root.setProperty("sparkTo", new["sparks"])
        self.root.setProperty("mix", 0.0)
        u = self.u
        self.particles = [{
            "born": random.uniform(0, INJECT_PARTICLES),
            "life": random.uniform(0.75, 1.15),
            "r0": random.uniform(190, 330) * u,
            "th0": random.uniform(0, 2 * math.pi),
            "turn": random.choice((-1, 1)) * random.uniform(0.9, 1.6) * math.pi,
            "colour": random.choice((0, 1, 2, 2)),
            "size": random.uniform(4.0, 7.5) * u,
        } for _ in range(70)]
        self.root.setProperty("particles", self.particles)
        dpr = self.devicePixelRatio()
        side = self.cover_side()
        radius = L.COVER_RADIUS * u + 1
        self.cover_old = rounded_cover(self.old_image, side, radius, dpr) if self.old_image else None
        self.cover_new = rounded_cover(image, side, radius, dpr)
        self.inject_at = time.monotonic()
        self.root.setProperty("trans", 0.0)
        self.timer.start(FRAME)

    def _finish(self, emit=True):
        self.timer.stop()
        if self.new_image is not None:
            self.old_image = self.new_image
        self.new_image = None
        self.inject_at = None
        self.particles = []
        self.pal_from = list(self.pal_to)
        self.theme.set(self.pal_to)
        r = self.root
        r.setProperty("trans", -1.0)
        r.setProperty("particles", [])
        self.show_palette(self.pal_to, "A")
        r.setProperty("mix", 0.0)
        r.setProperty("haloColor", qcolor(self.pal_to[1]))
        self.show_cover(self.old_image)
        if emit:
            self.finished.emit()

    def tick(self):
        if not self.busy:
            return
        t = time.monotonic() - self.inject_at
        if t >= INJECT_DONE:
            self._finish()
            return
        k = smoothstep(0, INJECT_PALETTE, t)
        if t <= INJECT_PALETTE + 0.05:
            # other widgets follow the blend; the rings cross-fade their two picture sets
            self.theme.set([mix(a, b, k) for a, b in zip(self.pal_from, self.pal_to)])
            self.root.setProperty("mix", k)
            self.root.setProperty("haloColor", qcolor(self.theme.palette[1]))
        self.root.setProperty("trans", t)
        self.show_transition_frame(t)

    def show_transition_frame(self, t):
        """The swirl is drawn with QPainter, but only over the small cover square."""
        u = self.u
        side = self.cover_side()
        dpr = self.devicePixelRatio()
        img = blank(side, side, dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, side, side)
        c = rect.center()
        if self.cover_old:
            p.drawImage(QPointF(0, 0), self.cover_old)
        reveal = smoothstep(*REVEAL, t)
        if reveal > 0:
            swirl = Stage.swirl(c, side * 0.74, reveal)
            p.save()
            p.setClipPath(swirl)
            p.drawImage(QPointF(0, 0), self.cover_new)
            p.restore()
            if reveal < 1:
                shape = QPainterPath()
                shape.addRoundedRect(rect, L.COVER_RADIUS * u, L.COVER_RADIUS * u)
                p.save()
                p.setClipPath(shape)
                p.setBrush(Qt.BrushStyle.NoBrush)
                for width, alpha in ((9, 60), (3, 150), (1.2, 255)):
                    p.setPen(QPen(qcolor(self.pal_to[2], alpha), width * u))
                    p.drawPath(swirl)
                p.restore()
        # the rim flares while the new song pours in
        flare = smoothstep(0.5, 1.1, t) * (1 - smoothstep(1.6, INJECT_DONE, t))
        if flare > 0:
            shape = QPainterPath()
            inset = 2 * u
            shape.addRoundedRect(rect.adjusted(inset, inset, -inset, -inset), L.COVER_RADIUS * u, L.COVER_RADIUS * u)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for width, alpha in ((6, 90), (1.8, 230)):
                p.setPen(QPen(qcolor(self.pal_to[2], alpha * flare), width * u))
                p.drawPath(shape)
        p.end()
        old = self.root.property("cover")
        self.root.setProperty("cover", self.put("frame", img))
        if old:
            self.drop([old])

    @staticmethod
    def swirl(c, R, progress):
        """A spiral-edged blob that grows from the centre until it covers the whole square."""
        path = QPainterPath()
        n = 120
        twist = -progress * math.pi
        for i in range(n + 1):
            th = 2 * math.pi * i / n
            r = max(0.0, progress * R * 1.9 - (i / n) * R * 0.9)
            pt = QPointF(c.x() + r * math.cos(th + twist), c.y() + r * math.sin(th + twist))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        path.closeSubpath()
        return path


# ─────────────────────────────────────────────
# PLAYER: controls + seek bar
# ─────────────────────────────────────────────
class Player(DesktopWidget):
    action = Signal(str, object)  # toggle / prev / next / shuffle / repeat / seek(ms)

    OFFSETS = {"shuffle": -118, "prev": -60, "toggle": 0, "next": 61, "repeat": 119}

    def __init__(self, theme, clock):
        super().__init__(theme)
        self.clock = clock
        self.drag = None  # ms while the knob is being dragged
        self.timer = precise_timer(self, 100, self.update)

    def geometry_parts(self):
        u = self.u
        mid = self.width() / 2
        pill = QRectF(mid - 158 * u, 8 * u, 316 * u, 56 * u)
        bar_y = 88 * u
        bar = (mid - 155 * u, mid + 158 * u, bar_y)
        return mid, pill, bar

    def buttons(self):
        u = self.u
        mid, pill, _ = self.geometry_parts()
        cy = pill.center().y()
        return [(name, QPointF(mid + dx * u, cy), (24 if name == "toggle" else 20) * u)
                for name, dx in self.OFFSETS.items()]

    def paintEvent(self, event):
        track = self.clock.track
        if not track:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        u = self.u
        pal = self.theme.palette
        mid, pill, (x0, x1, by) = self.geometry_parts()

        glass(p, pill, pill.height() / 2, u, pal)

        for name, centre, radius in self.buttons():
            hovered = self.hover == name
            if name == "toggle":
                p.setPen(Qt.PenStyle.NoPen)
                halo = QRadialGradient(centre, radius * 1.5)
                halo.setColorAt(0.6, qcolor(pal[0], 70 if hovered else 45))
                halo.setColorAt(1.0, qcolor(pal[0], 0))
                p.setBrush(QBrush(halo))
                p.drawEllipse(centre, radius * 1.5, radius * 1.5)
                p.setBrush(QColor(28, 20, 56, 200))
                p.setPen(QPen(qcolor(mix(pal[2], WHITE, 0.3), 230), 1.8 * u))
                p.drawEllipse(centre, radius, radius)
                icon = "pause" if track["is_playing"] else "play"
                glyph(p, QRectF(centre.x() - radius, centre.y() - radius, 2 * radius, 2 * radius),
                      icon, 21 * u, QColor(255, 255, 255))
                continue

            active = (name == "shuffle" and track.get("shuffle")) or \
                     (name == "repeat" and track.get("repeat", "off") != "off")
            colour = qcolor(mix(pal[2], WHITE, 0.15)) if active else QColor(255, 255, 255, 255 if hovered else 205)
            if hovered:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 22))
                p.drawEllipse(centre, radius * 0.95, radius * 0.95)
            icon = "repeat_one" if name == "repeat" and track.get("repeat") == "track" else name
            glyph(p, QRectF(centre.x() - radius, centre.y() - radius, 2 * radius, 2 * radius), icon, 18 * u, colour)
            if active:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(colour)
                p.drawEllipse(QPointF(centre.x(), centre.y() + 14 * u), 1.8 * u, 1.8 * u)

        # seek bar
        duration = track["duration_ms"] or 1
        pos = self.drag if self.drag is not None else self.clock.progress()
        k = max(0.0, min(1.0, pos / duration))
        h = 4 * u
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 55))
        p.drawRoundedRect(QRectF(x0, by - h / 2, x1 - x0, h), h / 2, h / 2)
        xk = x0 + (x1 - x0) * k
        g = QLinearGradient(x0, 0, max(xk, x0 + 1), 0)
        g.setColorAt(0, QColor(255, 255, 255, 235))
        g.setColorAt(1, qcolor(mix(pal[2], WHITE, 0.2), 255))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(x0, by - h / 2, xk - x0, h), h / 2, h / 2)
        spark(p, QPointF(xk, by), 13 * u, pal[2], 0.55)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(xk, by), 6 * u, 6 * u)

        p.setFont(font(14 * u))
        p.setPen(QColor(255, 255, 255, 215))
        p.drawText(QRectF(x0 - 70 * u, by - 12 * u, 56 * u, 24 * u),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, fmt_time(pos))
        p.drawText(QRectF(x1 + 16 * u, by - 12 * u, 56 * u, 24 * u),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, fmt_time(duration))
        p.end()

    def bar_ms(self, x):
        _, _, (x0, x1, _) = self.geometry_parts()
        k = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
        return round(k * (self.clock.track["duration_ms"] or 0))

    def mousePressEvent(self, event):
        if not self.clock.track:
            return
        pos = event.position()
        name = self.hit(pos)
        if name:
            if name == "toggle":
                self.clock.set_playing(not self.clock.track["is_playing"])  # flip now, confirm on next poll
            self.action.emit(name, None)
            self.update()
            return
        _, _, (x0, x1, by) = self.geometry_parts()
        if x0 - 8 * self.u <= pos.x() <= x1 + 8 * self.u and abs(pos.y() - by) <= 12 * self.u:
            self.drag = self.bar_ms(pos.x())
            self.update()

    def mouseMoveEvent(self, event):
        if self.drag is not None:
            self.drag = self.bar_ms(event.position().x())
            self.update()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.drag is not None:
            ms, self.drag = self.drag, None
            self.clock.seek(ms)
            self.action.emit("seek", ms)
            self.update()
        super().mouseReleaseEvent(event)


# ─────────────────────────────────────────────
# HEADER: logo, settings, hide, quit
# ─────────────────────────────────────────────
class Header(DesktopWidget):
    clicked = Signal(str, object)  # settings(global point) / minimize / close

    live = True   # False: live mode is off, so a "Live on" pill replaces minimize/close

    def set_live(self, live):
        if live != self.live:
            self.live = live
            self.update()

    def pill(self):
        u = self.u
        cy = self.height() / 2
        return QRectF(156 * u, cy - 14 * u, 64 * u, 28 * u)

    def buttons(self):
        u = self.u
        cy = self.height() / 2
        if not self.live:
            return [("settings", QPointF(135 * u, cy), 16 * u)]
        return [("settings", QPointF(135 * u, cy), 16 * u),
                ("minimize", QPointF(173 * u, cy), 14 * u),
                ("close", QPointF(205 * u, cy), 14 * u)]

    def hit(self, pos):
        if not self.live and self.pill().contains(pos):
            return "live_on"
        return super().hit(pos)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        u = self.u
        pal = self.theme.palette
        cy = self.height() / 2

        path = QPainterPath()
        script = font(36 * u, QFont.Weight.Normal, "Brush Script MT")
        path.addText(QPointF(10 * u, cy + 11 * u), script, "Sally")
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcolor(pal[2], 60), 5 * u))  # soft glow behind the letters
        p.drawPath(path)
        g = QLinearGradient(10 * u, 0, 70 * u, 0)
        g.setColorAt(0, QColor(255, 255, 255))
        g.setColorAt(1, qcolor(mix(pal[2], WHITE, 0.35)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawPath(path)
        wave_icon(p, 88 * u, cy, 17 * u, u, qcolor(mix(pal[2], WHITE, 0.4)))

        for name, centre, radius in self.buttons():
            hovered = self.hover == name
            if name == "settings" or hovered:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(20, 16, 40, 200) if name == "settings" else QColor(255, 255, 255, 30))
                p.drawEllipse(centre, radius, radius)
            if name == "settings":
                p.setPen(QPen(QColor(255, 255, 255, 90 if hovered else 50), 1 * u))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(centre, radius, radius)
            r = QRectF(centre.x() - radius, centre.y() - radius, 2 * radius, 2 * radius)
            glyph(p, r, name, (15 if name == "settings" else 12) * u, QColor(255, 255, 255, 240))
        if not self.live:
            rect = self.pill()
            p.setPen(QPen(qcolor(pal[2], 230 if self.hover == "live_on" else 150), 1.2 * u))
            p.setBrush(QColor(20, 16, 40, 220 if self.hover == "live_on" else 180))
            p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            p.setFont(font(11 * u, QFont.Weight.DemiBold))
            p.setPen(QColor(255, 255, 255, 240))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Live on")
        p.end()

    def mousePressEvent(self, event):
        name = self.hit(event.position())
        if name:
            self.clicked.emit(name, event.globalPosition().toPoint())


# ─────────────────────────────────────────────
# LYRICS
# ─────────────────────────────────────────────
class Lyrics(DesktopWidget):
    collapsed_changed = Signal(bool)
    SLIDE = 0.32  # seconds for a line change

    def __init__(self, theme, clock):
        super().__init__(theme)
        self.clock = clock
        self.track_id = None
        self.lines = []
        self.times = []
        self.status = ""
        self.index = -1
        self.changed_at = 0.0
        self.collapsed = False
        self.wrap_cache = {}
        self.timer = precise_timer(self, 50, self.tick)  # 60 fps only while a line slides

    def show_status(self, track_id, text):
        self.track_id, self.lines, self.times, self.status, self.index = track_id, [], [], text, -1
        self.update()

    def show_lines(self, track_id, lines):
        self.track_id, self.lines, self.status = track_id, lines, ""
        self.times = [ms for ms, _ in lines]
        self.index = -2  # force a refresh on the next tick
        self.update()

    def tick(self):
        if not self.lines:
            return
        index = bisect.bisect_right(self.times, self.clock.progress() + 150) - 1  # a hair early reads better
        if index != self.index:
            # a jump (seek, new song) snaps; the next line in order slides up
            self.changed_at = time.monotonic() if index == self.index + 1 else 0.0
            self.index = index
            self.update()
        sliding = time.monotonic() - self.changed_at < self.SLIDE + 0.1
        if sliding:
            self.update()
        self.timer.setInterval(FRAME if sliding else 50)

    def buttons(self):
        return [("collapse", QPointF(self.width() - 31 * self.u, 26 * self.u), 14 * self.u)]

    def mousePressEvent(self, event):
        if self.hit(event.position()) or event.position().y() < 46 * self.u:
            self.collapsed = not self.collapsed
            self.collapsed_changed.emit(self.collapsed)

    def line(self, i):
        if 0 <= i < len(self.lines):
            return self.lines[i][1] or "♪"
        return ""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        u = self.u
        pal = self.theme.palette
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        glass(p, rect, 18 * u, u, pal)

        wave_icon(p, 34 * u, 26 * u, 15 * u, u, QColor(255, 255, 255, 235))
        p.setFont(font(15 * u, QFont.Weight.DemiBold))
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(56 * u, 12 * u, 150 * u, 28 * u), Qt.AlignmentFlag.AlignVCenter, "Lyrics")
        centre = self.buttons()[0][1]
        glyph(p, QRectF(centre.x() - 12 * u, centre.y() - 12 * u, 24 * u, 24 * u),
              "down" if self.collapsed else "up", 13 * u, QColor(255, 255, 255, 220))
        if self.collapsed:
            p.end()
            return

        left, width = 25 * u, self.width() - 50 * u
        p.setClipRect(QRectF(0, 44 * u, self.width(), self.height() - 50 * u))
        if not self.lines:
            p.setFont(font(15 * u))
            p.setPen(QColor(255, 255, 255, 150))
            p.drawText(QRectF(left, 50 * u, width, 90 * u),
                       Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, self.status)
            p.end()
            return

        slide = 1 - smoothstep(0, self.SLIDE, time.monotonic() - self.changed_at)
        spacing = 9 * u
        rows = {   # line index -> (font, colour); wrapped lines take as many rows as they need
            self.index - 2: (15, False, QColor(255, 255, 255, round(165 * slide))),   # leaving at the top
            self.index - 1: (15, False, QColor(255, 255, 255, 165)),
            self.index: (18, True, qcolor(mix(pal[2], WHITE, 0.45))),
            self.index + 1: (15, False, QColor(255, 255, 255, 120)),
        }
        laid = {i: self.wrap(self.line(i), size, bold, width) for i, (size, bold, _) in rows.items()}

        # the current line sits in the middle; the others stack above and below it
        top_edge, bottom_edge = 44 * u, self.height() - 6 * u
        cur_h = laid[self.index][1]
        centre = (top_edge + bottom_edge) / 2 + 4 * u
        tops = {self.index: centre - cur_h / 2}
        tops[self.index - 1] = tops[self.index] - spacing - laid[self.index - 1][1]
        tops[self.index - 2] = tops[self.index - 1] - spacing - laid[self.index - 2][1]
        tops[self.index + 1] = tops[self.index] + cur_h + spacing
        # during a slide everything starts lower, where the line that just finished used to be
        prev_h = laid[self.index - 1][1]
        offset = slide * (prev_h / 2 + spacing + cur_h / 2) if self.line(self.index - 1) else 0

        for i, (size, bold, colour) in rows.items():
            f, h, text = laid[i]
            if not text or not colour.alpha():
                continue
            top = tops[i] + offset
            if top > bottom_edge or top + h < top_edge:
                continue
            p.setFont(f)
            p.setPen(colour)
            p.drawText(QRectF(left, top, width, h), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, text)
        p.end()

    def wrap(self, text, size, bold, width):
        """(font, height, text) for one lyric line: it wraps instead of being cut off, shrinking
        a little first if it would need more than two rows. Cached, since a line is laid out
        on every frame of its slide."""
        key = (text, size, bold, round(width), round(self.u, 3))
        hit = self.wrap_cache.get(key)
        if hit:
            return hit
        u = self.u
        weight = QFont.Weight.DemiBold if bold else QFont.Weight.Normal
        flags = int(Qt.TextFlag.TextWordWrap)
        for px in (size, size - 2, size - 3.5):
            f = font(px * u, weight)
            fm = QFontMetricsF(f)
            h = fm.boundingRect(QRectF(0, 0, width, 10000), flags, text).height() if text else 0
            if h <= fm.lineSpacing() * 2.2:
                break
        out = (f, max(h, fm.lineSpacing()) if text else 0, text)
        if len(self.wrap_cache) > 200:
            self.wrap_cache.clear()
        self.wrap_cache[key] = out
        return out


# ─────────────────────────────────────────────
# TOP TRACKS SPIRAL
# ─────────────────────────────────────────────
def round_cover(image, side, dpr, u):
    """A circular cover with a thin white frame, for the spiral."""
    square = square_pixmap(image, side, dpr)
    img = blank(side, side, dpr)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, side, side))
    p.setClipPath(clip)
    p.drawPixmap(QPointF(0, 0), square)
    p.setClipping(False)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 170), 1.5 * u))
    inset = 0.75 * u
    p.drawEllipse(QRectF(inset, inset, side - 2 * inset, side - 2 * inset))
    p.end()
    return img


def glow_sprite(rgb, dpr):
    img = blank(SPARK_PX, SPARK_PX, dpr)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QRadialGradient(QPointF(SPARK_PX / 2, SPARK_PX / 2), SPARK_PX / 2)
    g.setColorAt(0.65, qcolor(rgb, 110))
    g.setColorAt(1.0, qcolor(rgb, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(SPARK_PX / 2, SPARK_PX / 2), SPARK_PX / 2, SPARK_PX / 2)
    p.end()
    return img


class TopTracks(QObject):
    """Your top tracks, orbiting the cover inside the Stage (stage.qml), so they move in the
    same frame as the rings, fade out at the bottom like them and pass behind the album art.
    The Stage window normally lets every click through to the desktop; while the mouse is
    over one of the covers it takes clicks, so you can hover and play."""
    play = Signal(str)  # Spotify URI

    def __init__(self, theme, stage):
        super().__init__()
        self.theme = theme
        self.stage = stage
        self.root = stage.root
        self.u = 1.0
        self.tracks = []
        self.visible = True
        self.clickable = False
        self.serial = 0
        self.root.play.connect(self.play)
        self.palette_key = None
        # a song change blends the colours every frame; redrawing ~12 times a second is plenty
        self.recolour_timer = QTimer(self)
        self.recolour_timer.setSingleShot(True)
        self.recolour_timer.timeout.connect(self.recolour)
        theme.changed.connect(lambda: self.recolour_timer.isActive() or self.recolour_timer.start(80))
        self.input_timer = QTimer(self)
        self.input_timer.timeout.connect(self.update_input)
        self.input_timer.start(33)

    def put(self, prefix, image):
        self.serial += 1
        name = f"{prefix}-{self.serial}"
        self.stage.pictures.store[name] = image
        return "image://sally/" + name

    def forget(self, prefix):
        store = self.stage.pictures.store
        for name in [n for n in store if n.startswith(prefix + "-")]:
            del store[name]

    def recolour(self):
        key = (tuple(tuple(c) for c in self.theme.palette), round(self.u, 4))
        if key == self.palette_key:
            return
        self.palette_key = key
        self.forget("tglow")
        self.root.setProperty("trackGlow", self.put("tglow", glow_sprite(self.theme.palette[0],
                                                                         self.stage.devicePixelRatio())))
        self.root.setProperty("labelEdge", qcolor(self.theme.palette[2]))

    def set_tracks(self, tracks):
        self.tracks = [t for t in tracks if t.get("image") is not None]
        self.show_tracks()

    def show_tracks(self):
        dpr = self.stage.devicePixelRatio()
        side = 2 * 22 * 1.12 * self.u  # the largest cover, hovered
        self.forget("tcover")
        self.root.setProperty("start", float(self.root.property("tt") or 0))
        self.root.setProperty("tracks", [{
            "uri": t["uri"], "title": t["title"], "subtitle": t["subtitle"],
            "img": self.put("tcover", round_cover(t["image"], side, dpr, self.u)),
        } for t in self.tracks])

    def place(self, rect, u):
        """The covers live in the Stage, so only their size depends on this."""
        if u != self.u or self.palette_key is None:
            self.u = u
            self.palette_key = None
            self.recolour()
            if self.tracks:
                self.show_tracks()

    def setVisible(self, on):
        self.visible = on
        self.root.setProperty("showTracks", on)

    def update_input(self):
        want = False
        if self.visible and self.stage.isVisible():
            hits = self.root.property("hits")
            if hasattr(hits, "toVariant"):
                hits = hits.toVariant()
            if hits:
                pos = QCursor.pos() - self.stage.position()
                for x, y, r in hits:
                    reach = r * 1.12 + 6 * self.u
                    if (pos.x() - x) ** 2 + (pos.y() - y) ** 2 <= reach * reach:
                        want = True
                        break
        if want != self.clickable:
            self.clickable = want
            self.stage.setFlag(Qt.WindowType.WindowTransparentForInput, not want)
        if not want and (self.root.property("holding") or self.root.property("hovered") != -1):
            # the mouse left before QML saw it go: drop the label and let the covers move on
            self.root.setProperty("holding", False)
            self.root.setProperty("hovered", -1)


# ─────────────────────────────────────────────
# FADER: cross-fades the old wallpaper into the new one, under the desktop icons
# ─────────────────────────────────────────────
class Fader(QWidget):
    """Windows swaps wallpapers in one frame. This window is placed inside the wallpaper
    layer (under the icons), paints the old wallpaper, dissolves it into the new one, and
    only then is the real wallpaper switched underneath and this window hidden."""
    done = Signal()       # the new picture is fully shown: set the real wallpaper now
    DURATION = 1.6

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.old = self.new = None
        self.started = None
        self.timer = precise_timer(self, FRAME, self.tick)
        self.timer.stop()

    def attach(self):
        layer = wallpaper_layer()
        if not layer:
            return False
        hwnd = int(self.winId())
        style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), -16)  # GWL_STYLE
        # a child, not a popup, so it lives inside the layer instead of floating over it
        style = ((style & 0xFFFFFFFF) & ~0x80000000) | 0x40000000  # drop WS_POPUP, add WS_CHILD
        user32.SetWindowLongW(ctypes.c_void_p(hwnd), -16, ctypes.c_long(style).value)
        user32.SetParent(ctypes.c_void_p(hwnd), ctypes.c_void_p(layer))
        dpr = self.devicePixelRatioF()
        self.setGeometry(0, 0, round(user32.GetSystemMetrics(0) / dpr), round(user32.GetSystemMetrics(1) / dpr))
        return True

    def crossfade(self, old_path, new_path):
        """Returns False when it can't fade (the caller then sets the wallpaper directly)."""
        old, new = QImage(old_path), QImage(new_path)
        if old.isNull() or new.isNull():
            return False
        if old.size() != new.size():  # the user's own wallpaper: fill the screen like Windows does
            old = old.scaled(new.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
            old = old.copy((old.width() - new.width()) // 2, (old.height() - new.height()) // 2,
                           new.width(), new.height())
        dpr = self.devicePixelRatioF()
        old.setDevicePixelRatio(dpr)
        new.setDevicePixelRatio(dpr)
        self.old, self.new = QPixmap.fromImage(old), QPixmap.fromImage(new)
        # move into the wallpaper layer *before* showing, or it would flash over open windows
        if not self.attach():
            self.old = self.new = None
            return False
        self.started = time.monotonic()
        self.show()
        self.timer.start(FRAME)
        return True

    def tick(self):
        if self.started is None:
            return
        if time.monotonic() - self.started >= self.DURATION:
            self.started = None
            self.timer.stop()
            self.done.emit()  # keep showing the new picture until finish() is called
        self.update()

    def finish(self):
        """The real wallpaper now matches: drop the overlay."""
        self.hide()
        self.old = self.new = None

    def paintEvent(self, event):
        if not self.new:
            return
        p = QPainter(self)
        k = 1.0 if self.started is None else smoothstep(0, self.DURATION, time.monotonic() - self.started)
        if k < 1:
            p.drawPixmap(QPointF(0, 0), self.old)
            p.setOpacity(k)
        p.drawPixmap(QPointF(0, 0), self.new)
        p.end()
