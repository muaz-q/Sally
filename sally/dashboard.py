"""The listening-history dashboard: a frosted-glass window over the desktop.

Windows blurs whatever is behind it (the acrylic "accent" every Windows 10/11 build has);
Qt paints a light tint, the text and the list on top, and a light that keeps running
around the edge (like Siri's glow around an iPhone screen).
"""
import ctypes
import os
import time
import zlib
from ctypes import wintypes
from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QConicalGradient, QFont, QFontMetricsF, QGuiApplication, QLinearGradient,
                           QPainter, QPainterPath, QPen)
from PySide6.QtQuick import QQuickView, QQuickWindow
from PySide6.QtWidgets import QScrollArea, QWidget

from . import history
from . import layout as L
from .widgets import Pictures, blank, font, mix, qcolor, spark_sprite, wave_icon

WHITE = (255, 255, 255)
W, H = 620, 760          # design units
ROW, DAY = 54, 40        # list row heights
TOP = 338                # where the list starts
FILTERS = (("all", "All"), ("today", "Today"), ("week", "7 days"))


class _Accent(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                ("colour", ctypes.c_uint), ("animation", ctypes.c_int)]


class _CompositionData(ctypes.Structure):
    _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]


def frost(hwnd):
    """Blur what's behind the window (acrylic) and round its corners. False if unsupported."""
    try:
        accent = _Accent(4, 2, 0x22180E14, 0)   # ACRYLICBLURBEHIND, tint AABBGGRR
        data = _CompositionData(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
        ok = ctypes.windll.user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data))
        round_ = ctypes.c_int(2)   # DWMWCP_ROUND, Windows 11 only; harmless elsewhere
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(round_), 4)
        return bool(ok)
    except (AttributeError, OSError):
        return False


def clock(dt):
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def day_label(d, today):
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return f"{d:%A}, {d.day} {d:%b}" + ("" if d.year == today.year else f" {d.year}")


def stable_pick(text, n):
    return zlib.crc32(text.lower().encode("utf-8")) % n   # same colour for an artist every time


class PlayList(QWidget):
    """Every play, grouped by day. Paints only the rows in view, so thousands are fine."""
    play = Signal(str)

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.u = 1.0
        self.rows = []       # ["day", label, count, date] | ["play", entry]
        self.tops = []
        self.hover = -1
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_plays(self, plays):
        today = datetime.now().date()
        rows, head = [], None
        for p in plays:
            d = p["at"].date()
            if head is None or head[3] != d:
                head = ["day", day_label(d, today), 0, d]
                rows.append(head)
            head[2] += 1
            rows.append(["play", p])
        self.rows = rows
        self.hover = -1
        self.relayout()

    def relayout(self):
        u = self.u
        y, self.tops = 0.0, []
        for r in self.rows:
            self.tops.append(y)
            y += (DAY if r[0] == "day" else ROW) * u
        self.setFixedHeight(int(max(y, 80 * u) + 16 * u))
        self.update()

    def row_at(self, y):
        lo, hi = 0, len(self.tops) - 1
        while lo <= hi:   # binary search: rows are sorted by top
            mid = (lo + hi) // 2
            if self.tops[mid] <= y:
                lo = mid + 1
            else:
                hi = mid - 1
        return hi

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        u = self.u
        pal = self.theme.palette
        w = self.width()
        if not self.rows:
            p.setFont(font(13 * u))
            p.setPen(QColor(255, 255, 255, 140))
            p.drawText(QRectF(0, 30 * u, w, 40 * u), Qt.AlignmentFlag.AlignCenter, "No songs here yet")
            p.end()
            return
        clip = event.rect()
        title_font = font(13.5 * u, QFont.Weight.DemiBold)
        small = font(11.5 * u)
        fm_title, fm_small = QFontMetricsF(title_font), QFontMetricsF(small)
        for i in range(max(0, self.row_at(clip.top())), len(self.rows)):
            top = self.tops[i]
            if top > clip.bottom():
                break
            r = self.rows[i]
            if r[0] == "day":
                p.setFont(font(11.5 * u, QFont.Weight.Bold))
                p.setPen(qcolor(mix(pal[2], WHITE, 0.45)))
                p.drawText(QRectF(14 * u, top + 15 * u, w, 20 * u), Qt.AlignmentFlag.AlignLeft, r[1].upper())
                p.setFont(small)
                p.setPen(QColor(255, 255, 255, 110))
                p.drawText(QRectF(0, top + 15 * u, w - 16 * u, 20 * u), Qt.AlignmentFlag.AlignRight,
                           f"{r[2]} song{'s' if r[2] != 1 else ''}")
                continue
            e = r[1]
            box = QRectF(4 * u, top + 3 * u, w - 8 * u, (ROW - 6) * u)
            playable = bool(e.get("uri"))
            if i == self.hover:
                path = QPainterPath()
                path.addRoundedRect(box, 12 * u, 12 * u)
                p.fillPath(path, QColor(255, 255, 255, 30))
            # artist tile: their initial on a gradient in the song's colours
            tile = QRectF(box.x() + 8 * u, box.center().y() - 17 * u, 34 * u, 34 * u)
            k = stable_pick(e["artist"] or e["title"], 3)
            g = QLinearGradient(tile.topLeft(), tile.bottomRight())
            g.setColorAt(0, qcolor(pal[k], 230))
            g.setColorAt(1, qcolor(mix(pal[(k + 1) % 3], (20, 16, 40), 0.35), 230))
            path = QPainterPath()
            path.addRoundedRect(tile, 9 * u, 9 * u)
            p.fillPath(path, g)
            p.setFont(font(14 * u, QFont.Weight.Bold))
            p.setPen(QColor(255, 255, 255, 240))
            initial = next((c for c in (e["artist"] or e["title"]) if c.isalnum()), "♪").upper()
            p.drawText(tile, Qt.AlignmentFlag.AlignCenter, "▶" if i == self.hover and playable else initial)

            right = w - 16 * u
            p.setFont(small)
            p.setPen(QColor(255, 255, 255, 120))
            p.drawText(QRectF(0, box.top(), right, box.height()),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, clock(e["at"]))
            text_x = tile.right() + 12 * u
            text_w = right - text_x - 70 * u
            p.setFont(title_font)
            p.setPen(QColor(255, 255, 255, 245))
            p.drawText(QPointF(text_x, box.top() + 21 * u),
                       fm_title.elidedText(e["title"], Qt.TextElideMode.ElideRight, text_w))
            p.setFont(small)
            p.setPen(QColor(255, 255, 255, 150))
            p.drawText(QPointF(text_x, box.top() + 38 * u),
                       fm_small.elidedText(e["artist"], Qt.TextElideMode.ElideRight, text_w))
        p.end()

    def mouseMoveEvent(self, event):
        i = self.row_at(event.position().y())
        i = i if 0 <= i < len(self.rows) and self.rows[i][0] == "play" else -1
        if i != self.hover:
            self.hover = i
            playable = i >= 0 and bool(self.rows[i][1].get("uri"))
            self.setCursor(Qt.CursorShape.PointingHandCursor if playable else Qt.CursorShape.ArrowCursor)
            if playable:
                self.setToolTip("Click to play")
            elif i >= 0:
                self.setToolTip("Played before Sally kept song links, so it can't be replayed from here")
            else:
                self.setToolTip("")
            self.update()

    def leaveEvent(self, event):
        self.hover = -1
        self.update()

    def mouseReleaseEvent(self, event):
        i = self.row_at(event.position().y())
        if 0 <= i < len(self.rows) and self.rows[i][0] == "play" and self.rows[i][1].get("uri"):
            self.play.emit(self.rows[i][1]["uri"])


def edge_colours(pal):
    return [pal[0], pal[1], pal[2], (255, 120, 200), pal[0]]


def edge_band(w, h, radius, u, pal, angle, dpr):
    """The soft glow along the edge, in the song's colours turned to `angle`. Drawn at half
    resolution: it's all blur, and the graphics card scales it up for free."""
    img = blank(w, h, dpr * 0.5)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(1.5 * u, 1.5 * u, w - 3 * u, h - 3 * u), radius, radius)
    for width, alpha in ((12 * u, 30), (6 * u, 60), (2.5 * u, 150)):
        g = QConicalGradient(QPointF(w / 2, h / 2), angle)
        cols = edge_colours(pal)
        for i, col in enumerate(cols):
            g.setColorAt(i / (len(cols) - 1), qcolor(mix(col, WHITE, 0.2), alpha))
        p.setPen(QPen(g, width))
        p.drawPath(path)
    p.end()
    return img


class EdgeLight(QQuickView):
    """A click-through window lying exactly over the dashboard, with the running light in it.
    Animating it inside the dashboard itself made Windows re-send the whole see-through
    window every frame (over half a CPU core); on the graphics card it's nearly free."""

    def __init__(self, theme):
        QQuickWindow.setDefaultAlphaBuffer(True)
        super().__init__()
        self.theme = theme
        self.setColor(QColor(0, 0, 0, 0))
        self.setFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                      | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        self.pictures = Pictures()
        self.engine().addImageProvider("sally", self.pictures)
        self.setSource(QUrl.fromLocalFile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge.qml")))
        self.root = self.rootObject()
        if self.root is None:
            raise RuntimeError("edge.qml failed to load: " + "; ".join(e.toString() for e in self.errors()))
        self.key = None
        self.serial = 0
        self.dash = None
        # a song change blends the colours every frame; redrawing ~12 times a second is plenty
        self.recolour = QTimer(self)
        self.recolour.setSingleShot(True)
        self.recolour.timeout.connect(lambda: self.dash and self.isVisible() and self.redraw())
        theme.changed.connect(lambda: self.recolour.isActive() or self.recolour.start(80))

    def put(self, image):
        self.serial += 1
        name = f"edge-{self.serial}"
        self.pictures.store[name] = image
        return "image://sally/" + name

    def follow(self, dash):
        self.setGeometry(dash.geometry())
        self.root.setProperty("u", dash.u)
        self.root.setProperty("radius", dash.radius())
        self.redraw(dash)

    def redraw(self, dash=None):
        dash = dash or self.dash
        self.dash = dash
        pal = self.theme.palette
        w, h = dash.width(), dash.height()
        key = (w, h, dash.radius(), tuple(map(tuple, pal)))
        if key == self.key:
            return
        self.key = key
        self.pictures.store.clear()
        dpr = self.devicePixelRatio()
        self.root.setProperty("bands", [self.put(edge_band(w, h, dash.radius(), dash.u, pal, a, dpr))
                                        for a in (0, 120, 240)])
        self.root.setProperty("spark", self.put(spark_sprite(mix(pal[2], WHITE, 0.35), dpr)))
        self.root.setProperty("halo", self.put(spark_sprite(mix(pal[2], WHITE, 0.6), dpr)))

    def lift_above(self, dash):
        """Owned by the dashboard, so Windows always keeps it just above the dashboard."""
        GWLP_HWNDPARENT = -8
        ctypes.windll.user32.SetWindowLongPtrW(wintypes.HWND(int(self.winId())), GWLP_HWNDPARENT,
                                               wintypes.HWND(int(dash.winId())))


class Dashboard(QWidget):
    play = Signal(str)

    def __init__(self, theme):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Sally: listening history")
        self.theme = theme
        self.u = 1.0
        self.frosted = False
        self.plays = []
        self.stats = history.stats([])
        self.days = []           # [(date, count)] for the last 7 days
        self.filter = "all"
        self.stamp = None
        self.hover = None        # "close" | a filter key
        self.pressed = None
        self.was_active = False
        self.hidden_at = 0.0
        self.t0 = time.monotonic()
        self.setMouseTracking(True)

        self.list = PlayList(theme)
        self.list.play.connect(self.play)
        self.scroll = QScrollArea(self)
        self.scroll.setWidget(self.list)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setStyleSheet("""
            QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 0 4px 0; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,60); border-radius: 3px; min-height: 30px; }
            QScrollBar::handle:vertical:hover { background: rgba(255,255,255,120); }
            QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page
                { background: none; height: 0; }
        """)
        theme.changed.connect(self.update)
        theme.changed.connect(self.list.update)

        # new songs while it's open: checking a few file times is nearly free
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.edge = EdgeLight(theme)

    # ── showing and closing ──────────────────
    def toggle(self):
        if self.isVisible():
            self.hide()
        elif time.monotonic() - self.hidden_at > 0.5:
            # (clicking the menu item to close it first closes it by clicking outside)
            self.open()

    def open(self):
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.u = u = max(0.85, min(1.25, L.unit(g.width(), g.height()) * 1.15))
        self.list.u = u
        w, h = W * u, min(H * u, g.height() - 40)
        self.setGeometry(int(g.x() + (g.width() - w) / 2), int(g.y() + (g.height() - h) / 2), int(w), int(h))
        self.stamp = None
        self.was_active = False
        self.refresh()
        self.show()
        if not self.frosted:
            self.frosted = frost(int(self.winId()))
        self.raise_()
        # Windows won't hand focus to a background app, so lift it to the top explicitly:
        # topmost and straight back, which leaves it in front of whatever you were using
        hwnd = wintypes.HWND(int(self.winId()))
        flags = 0x1 | 0x2 | 0x40   # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        ctypes.windll.user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags)
        ctypes.windll.user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, flags)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        self.activateWindow()
        self.refresh_timer.start(5000)
        self.edge.follow(self)
        self.edge.show()
        self.edge.lift_above(self)

    def hideEvent(self, event):
        self.hidden_at = time.monotonic()
        self.refresh_timer.stop()
        self.edge.hide()
        super().hideEvent(event)

    def moveEvent(self, event):
        if self.edge.isVisible():
            self.edge.setPosition(self.pos())   # dragging the dashboard carries the light along

    def changeEvent(self, event):
        # clicking anywhere outside closes it, like a menu
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self.was_active = True
            elif self.was_active and self.isVisible():
                self.hide()
        super().changeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()

    # ── data ─────────────────────────────────
    def refresh(self):
        stamp = history.sources()
        if stamp == self.stamp:
            return
        self.stamp = stamp
        self.plays = history.load()
        self.stats = history.stats(self.plays)
        today = datetime.now().date()
        counts = {}
        for p in self.plays:
            d = p["at"].date()
            if (today - d).days < 7:
                counts[d] = counts.get(d, 0) + 1
        self.days = [(today - timedelta(days=i), counts.get(today - timedelta(days=i), 0)) for i in range(6, -1, -1)]
        self.apply_filter()

    def apply_filter(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = {"all": None, "today": today, "week": today - timedelta(days=6)}[self.filter]
        self.list.set_plays([p for p in self.plays if since is None or p["at"] >= since])
        self.scroll.verticalScrollBar().setValue(0)
        self.update()

    # ── layout ───────────────────────────────
    def resizeEvent(self, event):
        u = self.u
        self.scroll.setGeometry(int(22 * u), int(TOP * u), int(self.width() - 40 * u),
                                int(self.height() - TOP * u - 22 * u))
        self.list.relayout()

    def close_rect(self):
        u = self.u
        return QRectF(self.width() - 60 * u, 20 * u, 38 * u, 38 * u)

    def chip_rects(self):
        u = self.u
        fm = QFontMetricsF(font(12 * u, QFont.Weight.DemiBold))
        x, out = 26 * u, []
        for key, label in FILTERS:
            w = fm.horizontalAdvance(label) + 30 * u
            out.append((key, label, QRectF(x, 290 * u, w, 30 * u)))
            x += w + 8 * u
        return out

    def target(self, pos):
        if self.close_rect().adjusted(-6, -6, 6, 6).contains(pos):
            return "close"
        for key, _, r in self.chip_rects():
            if r.contains(pos):
                return key
        return None

    # ── painting ─────────────────────────────
    def radius(self):
        return (8 if self.frosted else 18) * self.u   # Windows 11 rounds a frosted window by itself

    def edge_path(self):
        inset = 1.5 * self.u
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), self.radius(), self.radius())
        return path

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.theme.palette
        path = self.edge_path()
        # the blur does the frosting; this tint keeps white text readable on bright wallpapers
        p.fillPath(path, QColor(16, 12, 30, 125 if self.frosted else 230))
        g = QConicalGradient(QPointF(self.width() / 2, self.height() / 2), 90)
        cols = edge_colours(pal)
        for i, col in enumerate(cols):
            g.setColorAt(i / (len(cols) - 1), qcolor(mix(col, WHITE, 0.3), 120))
        p.setPen(QPen(g, 1.2 * self.u))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        self.paint_header(p, pal)
        p.end()

    def paint_header(self, p, pal):
        u = self.u
        s = self.stats
        # title
        wave_icon(p, 40 * u, 42 * u, 22 * u, u, qcolor(mix(pal[2], WHITE, 0.4)))
        p.setPen(QColor(255, 255, 255, 245))
        p.setFont(font(22 * u, QFont.Weight.DemiBold))
        p.drawText(QPointF(62 * u, 50 * u), "Listening history")
        p.setFont(font(11.5 * u))
        p.setPen(QColor(255, 255, 255, 130))
        since = f"since {s['since']:%d %b %Y}" if s["since"] else "nothing yet"
        p.drawText(QPointF(28 * u, 78 * u), f"Every song Sally has seen you play, {since}")

        # close: a real button, easy to hit
        c = self.close_rect()
        hovered = self.hover == "close"
        p.setPen(QPen(QColor(255, 255, 255, 90 if hovered else 45), 1 * u))
        p.setBrush(QColor(232, 72, 96, 200) if hovered else QColor(255, 255, 255, 22))
        p.drawEllipse(c)
        p.setPen(QPen(QColor(255, 255, 255, 240), 1.8 * u, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        k = 6.5 * u
        m = c.center()
        p.drawLine(QPointF(m.x() - k, m.y() - k), QPointF(m.x() + k, m.y() + k))
        p.drawLine(QPointF(m.x() - k, m.y() + k), QPointF(m.x() + k, m.y() - k))

        # stat tiles
        tiles = [(str(s["today"]), "today"), (str(s["week"]), "last 7 days"), (str(s["total"]), "all time")]
        x, y = 22 * u, 98 * u
        for value, label in tiles:
            box = QRectF(x, y, 108 * u, 78 * u)
            self.card(p, box)
            p.setFont(font(25 * u, QFont.Weight.DemiBold))
            p.setPen(qcolor(mix(pal[2], WHITE, 0.55)))
            p.drawText(QRectF(box.x() + 14 * u, box.y() + 8 * u, box.width(), 40 * u),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, value)
            p.setFont(font(11 * u))
            p.setPen(QColor(255, 255, 255, 135))
            p.drawText(QPointF(box.x() + 14 * u, box.y() + 63 * u), label)
            x += 116 * u
        box = QRectF(x, y, self.width() - x - 22 * u, 78 * u)
        self.card(p, box)
        inner = box.width() - 28 * u
        top_artist, top_song = s["top_artist"], s["top_song"]
        p.setFont(font(10 * u, QFont.Weight.DemiBold))
        p.setPen(QColor(255, 255, 255, 120))
        p.drawText(QPointF(box.x() + 14 * u, box.y() + 20 * u), "THIS WEEK'S FAVOURITES")
        big = font(15 * u, QFont.Weight.DemiBold)
        p.setFont(big)
        p.setPen(QColor(255, 255, 255, 240))
        p.drawText(QPointF(box.x() + 14 * u, box.y() + 43 * u), QFontMetricsF(big).elidedText(
            f"{top_artist[0]}  ·  {top_artist[1]} plays" if top_artist else "—", Qt.TextElideMode.ElideRight, inner))
        small = font(11.5 * u)
        p.setFont(small)
        p.setPen(QColor(255, 255, 255, 150))
        p.drawText(QPointF(box.x() + 14 * u, box.y() + 64 * u), QFontMetricsF(small).elidedText(
            f"♪ {top_song[0]}  ·  {top_song[1]}×" if top_song else "", Qt.TextElideMode.ElideRight, inner))

        # the last 7 days, one bar each
        box = QRectF(22 * u, 186 * u, self.width() - 44 * u, 92 * u)
        self.card(p, box)
        most = max([n for _, n in self.days] + [1])
        slot = (box.width() - 28 * u) / 7
        full = 46 * u
        for i, (d, n) in enumerate(self.days):
            bx = box.x() + 14 * u + i * slot
            bh = max(3 * u, full * n / most)
            bar = QRectF(bx + slot * 0.28, box.y() + 18 * u + full - bh, slot * 0.44, bh)
            today = i == 6
            g = QLinearGradient(bar.topLeft(), bar.bottomLeft())
            g.setColorAt(0, qcolor(mix(pal[2], WHITE, 0.35), 250 if today else 190))
            g.setColorAt(1, qcolor(pal[0], 200 if today else 120))
            path = QPainterPath()
            path.addRoundedRect(bar, 4 * u, 4 * u)
            p.fillPath(path, g)
            p.setFont(font(10 * u, QFont.Weight.DemiBold))
            p.setPen(QColor(255, 255, 255, 200))
            if n:
                p.drawText(QRectF(bx, bar.top() - 15 * u, slot, 13 * u), Qt.AlignmentFlag.AlignCenter, str(n))
            p.setFont(font(10 * u, QFont.Weight.Bold if today else QFont.Weight.Normal))
            p.setPen(QColor(255, 255, 255, 230 if today else 120))
            p.drawText(QRectF(bx, box.bottom() - 22 * u, slot, 16 * u), Qt.AlignmentFlag.AlignCenter,
                       "Today" if today else f"{d:%a}")

        # filter chips
        for key, label, r in self.chip_rects():
            on = key == self.filter
            path = QPainterPath()
            path.addRoundedRect(r, r.height() / 2, r.height() / 2)
            if on:
                g = QLinearGradient(r.topLeft(), r.topRight())
                g.setColorAt(0, qcolor(pal[0], 200))
                g.setColorAt(1, qcolor(pal[1], 200))
                p.fillPath(path, g)
            else:
                p.fillPath(path, QColor(255, 255, 255, 34 if self.hover == key else 18))
            p.setFont(font(12 * u, QFont.Weight.DemiBold))
            p.setPen(QColor(255, 255, 255, 250 if on else 170))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, label)

    def card(self, p, box):
        u = self.u
        path = QPainterPath()
        path.addRoundedRect(box, 14 * u, 14 * u)
        p.fillPath(path, QColor(255, 255, 255, 18))
        p.setPen(QPen(QColor(255, 255, 255, 30), 1 * u))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    # ── mouse ────────────────────────────────
    def mouseMoveEvent(self, event):
        over = self.target(event.position())
        if over != self.hover:
            self.hover = over
            self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, event):
        if self.hover:
            self.hover = None
            self.update()

    def mousePressEvent(self, event):
        self.pressed = self.target(event.position())
        if self.pressed is None and event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()   # drag it anywhere by its glass

    def mouseReleaseEvent(self, event):
        hit = self.target(event.position())
        if hit and hit == self.pressed:
            if hit == "close":
                self.hide()
            else:
                self.filter = hit
                self.apply_filter()
        self.pressed = None
