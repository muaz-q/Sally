import atexit
import colorsys
import ctypes
import io
import json
import logging
import os
import sys
import threading
import time
import winreg
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import auth, config, control, history, paths, power
from . import layout as L
from . import lyrics
from .dashboard import Dashboard
from .widgets import (Clock, Fader, Header, Lyrics, Player, Stage, Theme, TopTracks, desktop_host, pil_to_qimage,
                     pin_to_desktop)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
POLL_INTERVAL = 5          # seconds between Spotify checks (keeps us well under the rate limit)
TRACK_END_GRACE = 0.4      # poll this long after a track should end, so natural changes are instant
IMAGE_CACHE_SIZE = 20      # album covers kept in memory
NOW_PLAYING_PORT = 47823   # local-only feed of the current track, read by Freeloader / Stack Rank
TOP_TRACKS_REFRESH = 30 * 60
APP_DIR = str(paths.DATA)       # generated wallpapers live here
LOG_PATH = str(paths.LOG)
POWER_CHECK_MS = 15_000         # how often the battery is looked at

# SystemParametersInfoW actions/flags
SPI_GETDESKWALLPAPER = 0x0073
SPI_SETDESKWALLPAPER = 0x0014
SPIF_UPDATE_AND_BROADCAST = 0x01 | 0x02

# Sally runs without a console (pythonw), so everything goes to sally.log
log = logging.getLogger("sally")
log.setLevel(logging.INFO)


def setup_logging():
    paths.ensure()
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)

# ─────────────────────────────────────────────
# SPOTIFY
# ─────────────────────────────────────────────
API = "https://api.spotify.com/v1"
COMMAND_ERRORS = {
    "PREMIUM_REQUIRED": "Spotify only allows playback control with Premium.",
    "NO_ACTIVE_DEVICE": "Start Spotify on one of your devices first.",
}


class SpotifyClient:
    def __init__(self, auth_manager):
        self.auth = auth_manager
        self.session = requests.Session()
        self.rate_limited_until = 0
        # the first call after a scope change opens the browser; only one thread may do that
        self.token_lock = threading.Lock()

    def _request(self, method, path, **kwargs):
        with self.token_lock:
            token = self.auth.get_access_token()
        return self.session.request(method, API + path, headers={"Authorization": f"Bearer {token}"},
                                    timeout=5, **kwargs)

    def _cool_down(self, response):
        try:
            retry_after = int(response.headers.get("Retry-After", 0))
        except ValueError:
            retry_after = 0
        retry_after = max(retry_after, 10)
        # set the cooldown before logging, so nothing can skip it
        self.rate_limited_until = time.time() + retry_after
        log.warning(f"Rate limited. Cooling down {retry_after}s")

    def current(self):
        """Return the playing item as a dict, or None (nothing playing / error / cooling down)."""
        if time.time() < self.rate_limited_until:
            return None

        try:
            response = self._request("GET", "/me/player", params={"additional_types": "track,episode"})
        except Exception as e:
            log.warning(f"Spotify request failed: {e}")
            return None

        if response.status_code == 200:
            try:
                return parse_playing(response.json())
            except ValueError:
                return None
        if response.status_code == 204:
            return None
        if response.status_code == 429:
            self._cool_down(response)
            return None

        log.warning(f"Unexpected Spotify status: {response.status_code}")
        return None

    def command(self, method, path, params=None, body=None):
        """Send a playback command. Returns None on success, else a message for the user."""
        if time.time() < self.rate_limited_until:
            return "Spotify asked Sally to slow down. Try again in a few seconds."
        try:
            response = self._request(method, "/me/player/" + path, params=params, json=body)
        except Exception as e:
            log.warning(f"Spotify command {path} failed: {e}")
            return "Couldn't reach Spotify."
        if response.status_code in (200, 202, 204):
            return None
        if response.status_code == 429:
            self._cool_down(response)
            return "Spotify asked Sally to slow down. Try again in a few seconds."
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        reason = error.get("reason", "")
        log.warning(f"Spotify command {path} -> {response.status_code} {reason} {error.get('message', '')}")
        if reason in COMMAND_ERRORS:
            return COMMAND_ERRORS[reason]
        if response.status_code == 404:
            return COMMAND_ERRORS["NO_ACTIVE_DEVICE"]
        if response.status_code == 403 and "premium" in str(error.get("message", "")).lower():
            return COMMAND_ERRORS["PREMIUM_REQUIRED"]
        return f"Spotify refused that ({response.status_code})."

    def last_played(self):
        """The most recently played track, shaped like current(); None if unavailable."""
        try:
            response = self._request("GET", "/me/player/recently-played", params={"limit": 1})
            items = (response.json().get("items") or []) if response.status_code == 200 else []
        except Exception as e:
            log.warning(f"Recently played request failed: {e}")
            return None
        return parse_playing({"item": items[0].get("track"), "is_playing": False}) if items else None

    def top_tracks(self, limit=21):
        """Your most-played tracks lately; falls back to longer-term, then to recently played."""
        tracks = []
        for path, params in (("/me/top/tracks", {"limit": limit, "time_range": "short_term"}),
                             ("/me/top/tracks", {"limit": limit, "time_range": "medium_term"}),
                             ("/me/player/recently-played", {"limit": 50})):
            try:
                response = self._request("GET", path, params=params)
            except Exception as e:
                log.warning(f"Top tracks request failed: {e}")
                return []
            if response.status_code != 200:
                log.warning(f"Top tracks: {path} -> {response.status_code}")
                continue
            items = response.json().get("items") or []
            items = [i.get("track") or i for i in items]  # recently-played wraps each track
            seen = set()
            for item in items:
                if item.get("uri") and item["uri"] not in seen:
                    seen.add(item["uri"])
                    tracks.append(item)
            if tracks:
                break
        return [{
            "uri": t["uri"],
            "title": t.get("name") or "Unknown",
            "subtitle": ", ".join(a.get("name", "") for a in t.get("artists") or []),
            "art_url": pick_image((t.get("album") or {}).get("images") or [], 160),
        } for t in tracks[:limit]]


def pick_image(images, at_least):
    """Smallest cover that is still at least this many pixels wide (Spotify lists largest first)."""
    good = [i for i in images if (i.get("width") or 0) >= at_least]
    return (good[-1] if good else images[0])["url"] if images else None


def parse_playing(data):
    """Flatten the player payload. Handles songs, podcasts, local files and ads."""
    item = data.get("item")
    if not item:
        return None  # ads, or a type Spotify doesn't describe

    if item.get("type") == "episode":
        subtitle = (item.get("show") or {}).get("name", "")
        artist, album = subtitle, ""
        images = item.get("images") or (item.get("show") or {}).get("images") or []
    else:
        names = [a.get("name") for a in item.get("artists") or [] if a.get("name")]
        subtitle = ", ".join(names)
        artist = names[0] if names else ""
        album = (item.get("album") or {}).get("name", "")
        images = (item.get("album") or {}).get("images") or []

    title = item.get("name") or "Unknown"
    return {
        # local files have no id; fall back to something that still changes per track
        "id": item.get("id") or item.get("uri") or f"{title}|{subtitle}",
        "uri": item.get("uri"),
        "type": item.get("type") or "track",
        "title": title,
        "subtitle": subtitle,
        "artist": artist,
        "album": album,
        "art_url": images[0]["url"] if images else None,
        "is_playing": bool(data.get("is_playing")),
        "progress_ms": data.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
        "shuffle": bool(data.get("shuffle_state")),
        "repeat": data.get("repeat_state") or "off",
    }

# ─────────────────────────────────────────────
# WINDOWS
# ─────────────────────────────────────────────
def screen_size():
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

def get_wallpaper():
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, len(buf), buf, 0)
    return buf.value


def set_wallpaper(path):
    """Instant switch. Sally's own Fader has already dissolved to the new picture on top,
    so a Windows-side transition would only run unseen underneath it."""
    return bool(ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, os.path.abspath(path), SPIF_UPDATE_AND_BROADCAST))

# ─────────────────────────────────────────────
# IMAGES
# ─────────────────────────────────────────────
image_cache = OrderedDict()
image_lock = threading.Lock()

def get_image(url):
    with image_lock:
        if url in image_cache:
            image_cache.move_to_end(url)
            return image_cache[url]
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        img = Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as e:
        log.warning(f"Cover download failed: {e}")
        return None

    with image_lock:
        image_cache[url] = img
        while len(image_cache) > IMAGE_CACHE_SIZE:
            image_cache.popitem(last=False)
    return img

def cover_crop(img, w, h):
    """Scale to fill w x h without stretching, cropping the overflow."""
    scale = max(w / img.width, h / img.height)
    rw, rh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    img = img.resize((rw, rh), Image.BILINEAR)
    left, top = (rw - w) // 2, (rh - h) // 2
    return img.crop((left, top, left + w, top + h))

def rounded_mask(size, radius):
    # draw at 4x and shrink, so the corners are antialiased
    big = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle([0, 0, size * 4 - 1, size * 4 - 1], radius * 4, fill=255)
    return big.resize((size, size), Image.LANCZOS)


def _hue_gap(a, b):
    d = abs(a - b)
    return min(d, 1 - d)


def cover_palette(cover):
    """Three neon colours taken from the cover: main, second, and a pale highlight.
    Grey or black-and-white covers keep the default violet and blue."""
    # vote per pixel, not per averaged colour cluster: averaging turns a big muted-blue sea
    # grey, and then a few saturated skin pixels win
    # grey, and then a few saturated skin pixels win. Count area, since area is what the
    # blurred background shows, over 50-degree windows so a broad band of one colour
    # (water, sky) isn't split across buckets.
    bins = [0.0] * 36
    small = cover.resize((48, 48), Image.BILINEAR)
    for r, g, b in small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata():
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.07 or v < 0.15:
            continue  # greys, blacks and whites say nothing about the mood
        bins[int(h * 36) % 36] += 0.6 + s
    smooth = [sum(bins[(i + k) % 36] * (1 - abs(k) / 3) for k in range(-2, 3)) for i in range(36)]
    total = sum(smooth)
    if total < 48 * 48 * 0.04:
        return list(L.DEFAULT_PALETTE)  # essentially a black-and-white cover

    order = sorted(range(36), key=lambda i: smooth[i], reverse=True)
    first = order[0]
    hues = [(first + 0.5) / 36]
    for i in order[1:]:
        h = (i + 0.5) / 36
        if _hue_gap(h, hues[0]) > 0.12:
            if smooth[i] >= smooth[first] * 0.35:
                hues.append(h)
            break
    if len(hues) == 1:
        hues.append((hues[0] + 0.12) % 1)  # a neighbouring hue keeps one-colour covers lively

    def neon(h, s, v=1.0):
        return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))
    return [neon(hues[0], 0.72), neon(hues[1], 0.68), neon(hues[0], 0.30)]

# ─────────────────────────────────────────────
# TEXT
# ─────────────────────────────────────────────
FONT_FILES = {
    "title": ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"],
    "body": ["segoeui.ttf", "arial.ttf"],
}

def load_font(size, kind):
    for name in FONT_FILES[kind]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()

def fit_text(text, font, max_width):
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"

def draw_glow_text(bg, text, x, y, font, color, blur):
    if not text:
        return
    # glow only the area around the text; blurring the whole screen is the slow part
    left, top, right, bottom = font.getbbox(text, anchor="mm")
    pad = blur * 3
    box = (int(x + left - pad), int(y + top - pad), int(x + right + pad), int(y + bottom + pad))
    patch = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    ImageDraw.Draw(patch).text((x - box[0], y - box[1]), text, font=font,
                               fill=(255, 255, 255, 120), anchor="mm")
    patch = patch.filter(ImageFilter.GaussianBlur(blur))
    bg.alpha_composite(patch, (box[0], box[1]))

    ImageDraw.Draw(bg).text((x, y), text, font=font, fill=color, anchor="mm")

# ─────────────────────────────────────────────
# WALLPAPER
# ─────────────────────────────────────────────
def build_wallpaper(cover, title, subtitle, W, H, path):
    """Blurred cover background, the cover itself, title and artist.

    The moving rings are not in here: the Stage widget draws them live on top, exactly over
    this cover, so the wallpaper still looks complete when the widgets are hidden."""
    u = L.unit(W, H)

    # the background is heavily blurred, so build it small and upscale - it looks identical
    small = cover_crop(cover, max(1, W // 4), max(1, H // 4)).filter(ImageFilter.GaussianBlur(2.2 * u))
    bg = small.resize((W, H), Image.BILINEAR)
    bg = ImageEnhance.Brightness(bg).enhance(0.62).convert("RGBA")

    # darker edges pull the eye to the middle
    shade = Image.radial_gradient("L").resize((W, H), Image.BILINEAR).point(lambda v: max(0, v - 100) * 3 // 4)
    vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vignette.putalpha(shade)
    bg.alpha_composite(vignette)

    cx, cy = L.cover_centre(0, 0, W, H)
    size = round(L.COVER * u)
    radius = round(L.COVER_RADIUS * u)
    x, y = round(cx - size / 2), round(cy - size / 2)

    # shadow
    off_x, off_y, blur = round(6 * u), round(14 * u), round(16 * u)
    pad = blur * 3
    shadow = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([pad, pad, pad + size, pad + size],
                                             radius=radius, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    bg.alpha_composite(shadow, (x + off_x - pad, y + off_y - pad))

    art = cover_crop(cover, size, size) if cover.width != cover.height else cover.resize((size, size), Image.LANCZOS)
    bg.paste(art, (x, y), rounded_mask(size, radius))

    f1 = load_font(round(L.TITLE_SIZE * u), "title")
    f2 = load_font(round(L.ARTIST_SIZE * u), "body")
    max_w = W * 0.6
    draw_glow_text(bg, fit_text(title, f1, max_w), round(cx), round(cy + L.TITLE_DY * u), f1,
                   (255, 255, 255), round(5 * u))
    draw_glow_text(bg, fit_text(subtitle, f2, max_w), round(cx), round(cy + L.ARTIST_DY * u), f2,
                   (204, 204, 216), round(4 * u))

    bg.convert("RGB").save(path, "BMP")
    return path


class WallpaperWorker:
    """One background thread that always renders the newest track.

    A track change that arrives mid-render is queued instead of dropped, so the
    wallpaper can never get stuck on an old song. It reports twice per song: as soon as
    the cover is downloaded (so the animation can start) and when the wallpaper is built.
    """

    def __init__(self, stopped, on_cover, on_built):
        self.stopped = stopped
        self.on_cover = on_cover
        self.on_built = on_built
        self.pending = None
        self.cond = threading.Condition()
        self.turn = 0  # alternate files: Windows may still hold the previous one open
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, track):
        with self.cond:
            self.pending = track
            self.cond.notify()

    def _run(self):
        while True:
            with self.cond:
                while self.pending is None:
                    self.cond.wait()
                track, self.pending = self.pending, None

            try:
                cover = get_image(track["art_url"])
                if cover is None:
                    continue
                self.on_cover(track, cover)
                path = os.path.join(APP_DIR, f"wallpaper_{self.turn}.bmp")
                self.turn ^= 1
                start = time.perf_counter()
                build_wallpaper(cover, track["title"], track["subtitle"], *screen_size(), path)
                log.info(f"Wallpaper built in {time.perf_counter() - start:.2f}s")
                if self.stopped.is_set():
                    return  # quitting: don't overwrite the restored wallpaper
                self.on_built(track, path)
            except Exception:
                log.exception("Wallpaper build failed")

# ─────────────────────────────────────────────
# SETTINGS / STARTUP
# ─────────────────────────────────────────────
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
load_settings = config.load
save_settings = config.save


def set_autostart(on):
    # pythonw.exe: start at login with no console window
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, "Sally", 0, winreg.REG_SZ, f'"{exe}" -m sally run')
        else:
            try:
                winreg.DeleteValue(key, "Sally")
            except FileNotFoundError:
                pass

is_sally_wallpaper = paths.is_sally_wallpaper


def already_running():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    already_running.mutex = kernel32.CreateMutexW(None, False, "Local\\SallyWallpaper")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS

def tray_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QRadialGradient(32, 32, 30)
    g.setColorAt(0.0, QColor(255, 255, 255))
    g.setColorAt(0.45, QColor(0, 220, 255))
    g.setColorAt(1.0, QColor(150, 80, 255, 0))
    p.setBrush(g)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 60, 60)
    p.end()
    return QIcon(pix)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
class NowPlayingFeed:
    """Serves the last polled track as JSON on 127.0.0.1, so local pages can show what's playing.
    It never calls Spotify itself; it only reports what the poll loop already fetched."""

    def __init__(self):
        self.track = None
        self.updated = 0.0
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def _headers(self, status, length=0):
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Cache-Control", "no-store")
                if length:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(length))
                self.end_headers()

            def do_OPTIONS(self):
                self._headers(204)

            def do_GET(self):
                if self.path.split("?")[0] != "/now-playing":
                    self._headers(404)
                    return
                body = json.dumps({"track": feed.track, "updated": feed.updated, "now": time.time()}).encode()
                self._headers(200, len(body))
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # pythonw has no console, and a hit per second would flood sally.log

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", NOW_PLAYING_PORT), Handler)
        except OSError as e:
            log.warning(f"Now-playing feed disabled, port {NOW_PLAYING_PORT} unavailable: {e}")
            self.server = None
            return
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log.info(f"Now-playing feed on http://127.0.0.1:{NOW_PLAYING_PORT}/now-playing")

    def update(self, track):
        self.track = track
        self.updated = time.time()


class Bridge(QObject):
    """Carries updates from background threads to the Qt main thread."""
    track = Signal(object)       # every poll: the playing item, or None
    cover = Signal(object)       # (track, QImage, palette) as soon as a new cover downloads
    built = Signal(object)       # (track, wallpaper path)
    lyrics = Signal(object)      # (track id, kind, lines)
    top_tracks = Signal(object)  # [track dicts with a QImage]
    notice = Signal(str)         # something the user should hear about
    wallpaper_set = Signal(str)  # Windows now shows this file
    command = Signal(object)     # from the `sally` command, via the pipe


def next_poll_delay(track):
    if track and track["is_playing"] and track["duration_ms"]:
        remaining = (track["duration_ms"] - track["progress_ms"]) / 1000
        if remaining > 0:
            return max(1.5, min(POLL_INTERVAL, remaining + TRACK_END_GRACE))
    return POLL_INTERVAL


LYRICS_STATUS = {
    "plain": "Only unsynced lyrics exist for this song.",
    "instrumental": "♪  Instrumental",
    "none": "No lyrics found for this song.",
    "error": "Couldn't reach the lyrics service.",
    "episode": "No lyrics for podcasts.",
}


class Sally:
    def __init__(self, app, auth_manager):
        self.app = app
        self.settings = load_settings()

        # remember the user's own wallpaper across restarts, so quitting can always put it back
        current = get_wallpaper()
        if current and not is_sally_wallpaper(current):
            self.settings["original_wallpaper"] = current
        save_settings(self.settings)
        self.apply_autostart()

        self.stopped = threading.Event()
        self.wake = threading.Event()        # set to poll Spotify right away (after a button press)
        self.wallpaper_lock = threading.Lock()
        self.pending_wallpaper = None
        self.current_wallpaper = get_wallpaper()  # what the fade starts from
        self.fading = None         # the file being faded in
        self.next_fade = None      # a newer wallpaper that arrived mid-fade
        self.last_track = None
        self.last_tray_text = None
        self.lyrics_cache = OrderedDict()
        self.lyrics_for = None
        self.notice_times = {}
        self.power_reason = None   # why live mode is resting to save battery, if it is
        self.live = False

        self.bridge = Bridge()
        self.worker = WallpaperWorker(
            self.stopped,
            on_cover=lambda track, cover: self.bridge.cover.emit((track, pil_to_qimage(cover), cover_palette(cover))),
            on_built=lambda track, path: self.bridge.built.emit((track, path)))
        self.client = SpotifyClient(auth_manager)
        self.feed = NowPlayingFeed()

        self.build_widgets()
        self.bridge.track.connect(self.on_track)
        self.bridge.cover.connect(self.on_cover)
        self.bridge.built.connect(self.on_built)
        self.bridge.lyrics.connect(self.on_lyrics)
        self.bridge.top_tracks.connect(self.top.set_tracks)
        self.bridge.notice.connect(self.notify)
        self.build_tray()
        self.bridge.command.connect(self.on_command)
        self.power_timer = QTimer()
        self.power_timer.timeout.connect(self.check_power)
        self.power_timer.start(POWER_CHECK_MS)
        self.check_power()
        control.serve(self.on_pipe, log)
        atexit.register(self.restore_wallpaper)

        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.top_tracks_loop, daemon=True).start()
        log.info("Sally started")

    # ── widgets ──────────────────────────────
    def build_widgets(self):
        self.theme = Theme()
        self.clock = Clock()
        self.stage = Stage(self.theme)
        self.player = Player(self.theme, self.clock)
        self.header = Header(self.theme)
        self.lyrics = Lyrics(self.theme, self.clock)
        self.top = TopTracks(self.theme, self.stage)
        self.lyrics.collapsed = self.settings["lyrics_collapsed"]

        self.stage.finished.connect(self.apply_pending_wallpaper)
        self.fader = Fader()
        self.fader.done.connect(lambda: self.set_now(self.fading))
        self.bridge.wallpaper_set.connect(self.on_wallpaper_set)
        self.player.action.connect(self.control)
        self.header.clicked.connect(self.on_header)
        self.lyrics.collapsed_changed.connect(self.on_lyrics_collapsed)
        self.top.play.connect(lambda uri: self.control("play_uri", uri))
        self.dashboard = Dashboard(self.theme)
        self.dashboard.play.connect(lambda uri: self.control("play_uri", uri))

        screen = self.app.primaryScreen()
        screen.geometryChanged.connect(self.place_widgets)
        screen.availableGeometryChanged.connect(self.place_widgets)
        self.place_widgets()
        self.update_visibility()

        # stop animating while windows hide the widgets; re-attach if Explorer restarted
        self.cover_timer = QTimer()
        self.cover_timer.timeout.connect(self.check_covered)
        self.cover_timer.start(1500)

    def place_widgets(self, *_):
        screen = self.app.primaryScreen()
        g, a = screen.geometry(), screen.availableGeometry()
        u = L.unit(g.width(), g.height())
        cx, cy = L.cover_centre(g.x(), g.y(), g.width(), g.height())
        h = L.STAGE_HALF * u
        self.stage.place(QRectF(cx - h, cy - h, 2 * h, h + L.STAGE_BOTTOM * u), u)
        x, y, w, hh = L.PLAYER
        self.player.place(QRectF(cx + x * u, cy + y * u, w * u, hh * u), u)
        right, top, w, hh = L.HEADER
        self.header.place(QRectF(g.right() + 1 - (right + w) * u, g.y() + top * u, w * u, hh * u), u)
        right, dy, w, hh = L.LYRICS
        if self.lyrics.collapsed:
            hh = L.LYRICS_COLLAPSED_H
        self.lyrics.place(QRectF(g.right() + 1 - (right + w) * u, cy + dy * u, w * u, hh * u), u)
        self.top.place(QRectF(cx - h, cy - h, 2 * h, h + L.STAGE_BOTTOM * u), u)   # orbits the cover

    def update_visibility(self):
        on = self.live
        has_track = self.clock.track is not None
        self.stage.setVisible(on)
        self.header.set_live(on)
        self.header.setVisible(True)   # stays up while off, so its "Live on" button can bring it back
        self.player.setVisible(on and has_track)
        self.lyrics.setVisible(on and has_track and self.settings["show_lyrics"])
        self.top.setVisible(on and self.settings["show_top_tracks"])
        if hasattr(self, "live_action"):
            for action, key in ((self.live_action, "live_mode"), (self.battery_action, "battery_auto")):
                action.blockSignals(True)
                action.setChecked(self.settings[key])
                action.blockSignals(False)
            resting = self.settings["live_mode"] and self.power_reason
            self.live_action.setText(f"Live mode (resting: {self.power_reason})" if resting else "Live mode")

    # ── live mode and battery ────────────────
    def check_power(self):
        reason = None
        if self.settings["battery_auto"]:
            reason = power.saving_reason(power.read(), self.settings["battery_threshold"],
                                         already_saving=self.power_reason is not None)
        if reason != self.power_reason:
            if reason and not self.power_reason and self.settings["live_mode"]:
                self.notify(f"Live mode is resting because the {reason}. The plain wallpaper stays on.")
            elif self.power_reason and not reason and self.settings["live_mode"]:
                self.notify("Live mode is back on.")
            self.power_reason = reason
            log.info(f"Power saving: {reason or 'off'}")
        self.apply_live()

    def apply_live(self):
        live = bool(self.settings["live_mode"] and not self.power_reason)
        if live != self.live:
            self.live = live
            log.info(f"Live mode {'on' if live else 'off'}")
            if not live and self.stage.busy:
                self.stage._finish()   # puts up the waiting wallpaper right away
        self.update_visibility()

    def all_widgets(self):
        return (self.stage, self.player, self.header, self.lyrics)

    def check_covered(self):
        """Re-attach to the desktop if Explorer restarted. The animation itself never pauses:
        it must already be moving the moment a window gets out of the way."""
        host = desktop_host()
        for widget in self.all_widgets():
            if widget.isVisible():
                pin_to_desktop(int(widget.winId()), host)

    def set_setting(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)
        if key == "autostart":
            self.apply_autostart()
        if key in ("battery_auto", "battery_threshold"):
            self.check_power()
        else:
            self.apply_live()

    # ── the `sally` command ──────────────────
    def on_pipe(self, message):
        """Runs on the pipe thread: answer from a snapshot, hand changes to the UI thread."""
        if message == "status":
            track = self.clock.track
            return {"live": self.live, "live_mode": self.settings["live_mode"], "resting": self.power_reason,
                    "battery_auto": self.settings["battery_auto"],
                    "battery_threshold": self.settings["battery_threshold"],
                    "track": f"{track['title']} - {track['subtitle']}" if track else None,
                    "playing": bool(track and track["is_playing"])}
        if isinstance(message, tuple) and message and message[0] in ("set", "quit", "dashboard"):
            self.bridge.command.emit(message)
            return "ok"
        return "unknown command"

    def on_command(self, message):
        if message[0] == "quit":
            self.quit()
        elif message[0] == "dashboard":
            self.dashboard.open()
        elif message[0] == "set" and message[1] in config.DEFAULTS:
            self.set_setting(message[1], message[2])

    def on_header(self, name, point):
        if name == "minimize":
            self.set_setting("live_mode", False)
            self.notify("Live mode off. Click \"Live on\" next to Sally at the top right to bring it back.")
        elif name == "live_on":
            self.set_setting("live_mode", True)
            if self.power_reason:
                self.notify(f"Live mode is resting because the {self.power_reason}. It comes back when you "
                            "plug in, or turn off \"Rest on low battery\" in the ⚙ menu.")
        elif name == "close":
            self.quit()
        elif name == "settings":
            menu = QMenu()
            for label, key in (("Show lyrics", "show_lyrics"), ("Show top tracks", "show_top_tracks")):
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.settings[key])
                action.toggled.connect(lambda on, k=key: self.set_setting(k, on))
            menu.addSeparator()
            menu.addAction("Refresh top tracks").triggered.connect(
                lambda: threading.Thread(target=self.load_top_tracks, daemon=True).start())
            battery = menu.addAction("Rest on low battery")
            battery.setCheckable(True)
            battery.setChecked(self.settings["battery_auto"])
            battery.toggled.connect(lambda on: self.set_setting("battery_auto", on))
            if self.live:
                menu.addAction("Turn off live mode").triggered.connect(lambda: self.on_header("minimize", None))
            else:
                menu.addAction("Turn on live mode").triggered.connect(lambda: self.on_header("live_on", None))
            menu.addAction("Listening history").triggered.connect(self.dashboard.toggle)
            menu.addAction("Open log").triggered.connect(lambda: os.startfile(LOG_PATH))
            menu.exec(point)

    def on_lyrics_collapsed(self, collapsed):
        self.settings["lyrics_collapsed"] = collapsed
        save_settings(self.settings)
        self.place_widgets()

    def notify(self, text):
        now = time.time()
        if now - self.notice_times.get(text, 0) > 30:  # don't repeat the same complaint every click
            self.notice_times[text] = now
            self.tray.showMessage("Sally", text, tray_icon(), 4000)

    # ── tray ─────────────────────────────────
    def build_tray(self):
        self.tray = QSystemTrayIcon(tray_icon(), self.app)
        menu = QMenu()
        self.now_playing = QAction("Nothing playing", menu)
        self.now_playing.setEnabled(False)
        menu.addAction(self.now_playing)
        menu.addSeparator()
        self.live_action = self.add_toggle(menu, "Live mode", "live_mode")
        self.battery_action = self.add_toggle(menu, "Rest on low battery", "battery_auto")
        self.add_toggle(menu, "Start with Windows", "autostart")
        menu.addSeparator()
        menu.addAction("Listening history").triggered.connect(self.dashboard.toggle)
        menu.addAction("Open log").triggered.connect(lambda: os.startfile(LOG_PATH))
        menu.addAction("Quit Sally").triggered.connect(self.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Sally")
        self.tray.activated.connect(self.on_tray_click)
        self.tray.show()

    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.set_setting("live_mode", not self.settings["live_mode"])

    def add_toggle(self, menu, label, key, after=None):
        action = QAction(label, menu)
        action.setCheckable(True)
        action.setChecked(self.settings[key])

        def toggled(on):
            if self.settings[key] != on:
                self.set_setting(key, on)
            if after:
                after()
        action.toggled.connect(toggled)
        menu.addAction(action)
        return action

    def apply_autostart(self):
        try:
            set_autostart(self.settings["autostart"])
        except OSError as e:
            log.warning(f"Could not change autostart: {e}")

    # ── track updates ────────────────────────
    def on_track(self, track):
        changed = (track or {}).get("id") != (self.clock.track or {}).get("id")
        self.clock.set(track)
        self.player.update()
        if changed or track is None:
            self.update_visibility()
        if track and track["id"] != self.lyrics_for:
            self.load_lyrics(track)

        if track:
            icon = "▶" if track["is_playing"] else "⏸"
            text = f"{icon} {track['title']}"
            if track["subtitle"]:
                text += f" — {track['subtitle']}"
        else:
            text = "Nothing playing"
        if text != self.last_tray_text:
            self.last_tray_text = text
            self.now_playing.setText(text if len(text) <= 60 else text[:59] + "…")
            self.tray.setToolTip(f"Sally\n{text}"[:127])

    def on_cover(self, payload):
        track, image, palette = payload
        log.info(f"Palette for {track['title']}: {palette}")
        self.stage.inject(image, palette)

    def on_built(self, payload):
        _, path = payload
        if self.stage.busy:
            self.pending_wallpaper = path  # goes up once the cover has finished swirling in
        else:
            self.apply_wallpaper(path)

    def apply_pending_wallpaper(self):
        if self.pending_wallpaper:
            path, self.pending_wallpaper = self.pending_wallpaper, None
            self.apply_wallpaper(path)

    def apply_wallpaper(self, path):
        """Dissolve from the current wallpaper to this one, then make it the real wallpaper."""
        if self.fading:
            self.next_fade = path  # one fade at a time; the newest waiting one wins
            return
        old = self.current_wallpaper
        if self.live and old and os.path.exists(old) and self.fader.crossfade(old, path):
            self.fading = path
        else:
            self.set_now(path)

    def set_now(self, path):
        def run():
            with self.wallpaper_lock:
                if self.stopped.is_set():
                    return
                if set_wallpaper(path):
                    log.info("Wallpaper set")
                else:
                    log.warning("Windows refused the wallpaper change")
                self.bridge.wallpaper_set.emit(path)
        threading.Thread(target=run, daemon=True).start()

    def on_wallpaper_set(self, path):
        self.current_wallpaper = path
        if path == self.fading:
            # give Explorer a moment to paint the new wallpaper before uncovering it
            QTimer.singleShot(500, self.end_fade)

    def end_fade(self):
        self.fader.finish()
        self.fading = None
        if self.next_fade:
            path, self.next_fade = self.next_fade, None
            self.apply_wallpaper(path)

    # ── lyrics ───────────────────────────────
    def load_lyrics(self, track):
        self.lyrics_for = track["id"]
        if track["id"] in self.lyrics_cache:
            self.on_lyrics((track["id"], *self.lyrics_cache[track["id"]]))
            return
        if track.get("type") == "episode":
            self.on_lyrics((track["id"], "episode", None))
            return
        self.lyrics.show_status(track["id"], "Looking for lyrics…")

        def run():
            try:
                kind, lines = lyrics.fetch(track)
            except Exception as e:
                log.warning(f"Lyrics lookup failed: {e}")
                kind, lines = "error", None
            self.bridge.lyrics.emit((track["id"], kind, lines))
        threading.Thread(target=run, daemon=True).start()

    def on_lyrics(self, payload):
        track_id, kind, lines = payload
        if kind != "error":
            self.lyrics_cache[track_id] = (kind, lines)
            while len(self.lyrics_cache) > 50:
                self.lyrics_cache.popitem(last=False)
        if track_id != self.lyrics_for:
            return  # the song changed while we were looking
        if kind == "synced":
            self.lyrics.show_lines(track_id, lines)
        else:
            self.lyrics.show_status(track_id, LYRICS_STATUS[kind])

    # ── top tracks ───────────────────────────
    def load_top_tracks(self):
        tracks = self.client.top_tracks()
        for t in tracks:
            img = get_image(t["art_url"]) if t["art_url"] else None
            t["image"] = pil_to_qimage(img) if img else None
        log.info(f"Loaded {len(tracks)} top tracks")
        self.bridge.top_tracks.emit(tracks)

    def top_tracks_loop(self):
        while not self.stopped.is_set():
            try:
                self.load_top_tracks()
            except Exception:
                log.exception("Top tracks failed")
            self.stopped.wait(TOP_TRACKS_REFRESH)

    # ── playback control ─────────────────────
    def control(self, kind, value=None):
        track = self.clock.track

        def run():
            c = self.client
            if kind == "toggle":
                # the Player already flipped the icon, so the clock holds the state we want
                error = c.command("PUT", "play" if track and track["is_playing"] else "pause")
            elif kind == "next":
                error = c.command("POST", "next")
            elif kind == "prev":
                error = c.command("POST", "previous")
            elif kind == "shuffle":
                error = c.command("PUT", "shuffle", {"state": "false" if track and track.get("shuffle") else "true"})
            elif kind == "repeat":
                order = ["off", "context", "track"]
                now = track.get("repeat", "off") if track else "off"
                error = c.command("PUT", "repeat", {"state": order[(order.index(now) + 1) % 3]})
            elif kind == "seek":
                error = c.command("PUT", "seek", {"position_ms": int(value)})
            elif kind == "play_uri":
                error = c.command("PUT", "play", body={"uris": [value]})
            else:
                return
            if error:
                self.bridge.notice.emit(error)
            time.sleep(0.35)  # Spotify takes a moment to report the new state
            self.wake.set()
        threading.Thread(target=run, daemon=True).start()

    def poll_loop(self):
        last_id = None
        while not self.stopped.is_set():
            try:
                track = self.client.current()
                if track is None and last_id is None:
                    # nothing playing at startup: colour the rings from the last song anyway
                    recent = self.client.last_played()
                    if recent and recent["art_url"]:
                        last_id = recent["id"]
                        self.worker.submit(recent)
                # a paused song at startup still gets its wallpaper; after that only playing ones
                if track and track["id"] != last_id and (track["is_playing"] or last_id is None):
                    last_id = track["id"]
                    log.info(f"Now: {track['title']} - {track['subtitle']}")
                    if track["is_playing"]:
                        history.record(track)   # the log rotates; this keeps every song for the dashboard
                    if track["art_url"]:
                        self.worker.submit(track)

                cooling = track is None and self.client.rate_limited_until > time.time()
                if not cooling:
                    self.feed.update(track)
                    self.bridge.track.emit(track)  # every poll, so the seek bar re-syncs

                self.wake.wait(next_poll_delay(track))
                self.wake.clear()
            except Exception:
                log.exception("Poll loop error")
                time.sleep(POLL_INTERVAL)

    def restore_wallpaper(self):
        if self.stopped.is_set():
            return
        with self.wallpaper_lock:
            self.stopped.set()
            original = self.settings.get("original_wallpaper")
            if original and os.path.exists(original):
                set_wallpaper(original)

    def quit(self):
        self.restore_wallpaper()
        self.tray.hide()
        self.app.quit()


def run():
    setup_logging()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    if already_running():
        return 0  # started twice (e.g. by login and by hand); the first copy keeps running

    signed_in = auth.logged_in()
    sally = Sally(app, auth.manager())
    if not signed_in:
        sally.notify("Sally isn't signed in to Spotify. Open a terminal and run: sally login")
    app.exec()
    return 0
