import atexit
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
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon
from spotipy.oauth2 import SpotifyOAuth

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
POLL_INTERVAL = 5          # seconds between Spotify checks (keeps us well under the rate limit)
TRACK_END_GRACE = 0.4      # poll this long after a track should end, so natural changes are instant
IMAGE_CACHE_SIZE = 20      # album covers kept in memory
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
LOG_PATH = os.path.join(APP_DIR, "sally.log")

# SystemParametersInfoW actions/flags
SPI_GETDESKWALLPAPER = 0x0073
SPI_SETDESKWALLPAPER = 0x0014
SPIF_UPDATE_AND_BROADCAST = 0x01 | 0x02

# Sally runs without a console (pythonw), so everything goes to sally.log
log = logging.getLogger("sally")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)

# ─────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────
def load_auth():
    load_dotenv(os.path.join(APP_DIR, ".env"))
    keys = ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI"]
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)} in .env.\n\n"
                           f"Copy .env.example to .env in {APP_DIR} and fill it in.")

    return SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
        scope="user-read-currently-playing",
        cache_path=os.path.join(APP_DIR, ".cache"),
    )

# ─────────────────────────────────────────────
# SPOTIFY
# ─────────────────────────────────────────────
class SpotifyClient:
    URL = "https://api.spotify.com/v1/me/player/currently-playing"

    def __init__(self, auth_manager):
        self.auth = auth_manager
        self.session = requests.Session()
        self.rate_limited_until = 0

    def current(self):
        """Return the playing item as a dict, or None (nothing playing / error / cooling down)."""
        if time.time() < self.rate_limited_until:
            return None

        try:
            token = self.auth.get_access_token(as_dict=False)
            response = self.session.get(
                self.URL,
                headers={"Authorization": f"Bearer {token}"},
                params={"additional_types": "track,episode"},
                timeout=5,
            )
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
            try:
                retry_after = int(response.headers.get("Retry-After", 0))
            except ValueError:
                retry_after = 0
            retry_after = max(retry_after, 10)
            # set the cooldown before printing, so nothing can skip it
            self.rate_limited_until = time.time() + retry_after
            log.warning(f"Rate limited. Cooling down {retry_after}s")
            return None

        log.warning(f"Unexpected Spotify status: {response.status_code}")
        return None


def parse_playing(data):
    """Flatten the currently-playing payload. Handles songs, podcasts, local files and ads."""
    item = data.get("item")
    if not item:
        return None  # ads, or a type Spotify doesn't describe

    if item.get("type") == "episode":
        subtitle = (item.get("show") or {}).get("name", "")
        images = item.get("images") or (item.get("show") or {}).get("images") or []
    else:
        subtitle = ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name"))
        images = (item.get("album") or {}).get("images") or []

    title = item.get("name") or "Unknown"
    return {
        # local files have no id; fall back to something that still changes per track
        "id": item.get("id") or item.get("uri") or f"{title}|{subtitle}",
        "title": title,
        "subtitle": subtitle,
        "art_url": images[0]["url"] if images else None,
        "is_playing": bool(data.get("is_playing")),
        "progress_ms": data.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
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
    return bool(ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, path, SPIF_UPDATE_AND_BROADCAST))

# ─────────────────────────────────────────────
# IMAGES
# ─────────────────────────────────────────────
image_cache = OrderedDict()

def get_image(url):
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

# ─────────────────────────────────────────────
# TEXT
# ─────────────────────────────────────────────
FONT_FILES = {
    True: ["arialbd.ttf", "segoeuib.ttf"],
    False: ["arial.ttf", "segoeui.ttf"],
}

def load_font(size, bold):
    for name in FONT_FILES[bold]:
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
    s = min(W / 960, H / 540)  # the design was laid out on a 960x540 canvas

    # the background is heavily blurred, so build it small and upscale - it looks identical
    small = cover_crop(cover, max(1, W // 4), max(1, H // 4)).filter(ImageFilter.GaussianBlur(1.5 * s))
    bg = small.resize((W, H), Image.BILINEAR)
    bg = ImageEnhance.Brightness(bg).enhance(0.7).convert("RGBA")

    cx, cy = W // 2, H // 2
    size = round(180 * s)
    radius = round(30 * s)
    x, y = cx - size // 2, cy - size // 2

    # shadow
    off_x, off_y, blur = round(8 * s), round(12 * s), round(10 * s)
    pad = blur * 3
    shadow = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([pad, pad, pad + size, pad + size],
                                             radius=radius, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    bg.alpha_composite(shadow, (x + off_x - pad, y + off_y - pad))

    art = cover.resize((size, size), Image.LANCZOS)
    bg.paste(art, (x, y), rounded_mask(size, radius))

    f1 = load_font(round(30 * s), bold=True)
    f2 = load_font(round(18 * s), bold=False)
    max_w = W * 0.8

    sy = y + size + round(40 * s)
    ay = sy + round(30 * s)
    draw_glow_text(bg, fit_text(title, f1, max_w), cx, sy, f1, (255, 255, 255), round(4 * s))
    draw_glow_text(bg, fit_text(subtitle, f2, max_w), cx, ay, f2, (180, 180, 180), round(4 * s))

    bg.convert("RGB").save(path, "BMP")
    return path




class WallpaperWorker:
    """One background thread that always renders the newest track.

    A track change that arrives mid-render is queued instead of dropped, so the
    wallpaper can never get stuck on an old song.
    """

    def __init__(self, stopped):
        self.stopped = stopped
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
                path = os.path.join(APP_DIR, f"wallpaper_{self.turn}.bmp")
                self.turn ^= 1
                start = time.perf_counter()
                build_wallpaper(cover, track["title"], track["subtitle"], *screen_size(), path)
                if self.stopped.is_set():
                    return  # quitting: don't overwrite the restored wallpaper
                if set_wallpaper(path):
                    log.info(f"Wallpaper updated in {time.perf_counter() - start:.2f}s")
                else:
                    log.warning("Windows refused the wallpaper change")
            except Exception:
                log.exception("Wallpaper build failed")

# ─────────────────────────────────────────────
# SETTINGS / STARTUP
# ─────────────────────────────────────────────
DEFAULT_SETTINGS = {"autostart": True, "original_wallpaper": None}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)

def save_settings(settings):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)

def set_autostart(on):
    # pythonw.exe: start at login with no console window
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, "Sally", 0, winreg.REG_SZ, f'"{exe}" "{os.path.abspath(__file__)}"')
        else:
            try:
                winreg.DeleteValue(key, "Sally")
            except FileNotFoundError:
                pass

def is_sally_wallpaper(path):
    return bool(path) and os.path.dirname(os.path.abspath(path)).lower() == APP_DIR.lower() \
        and os.path.basename(path).lower().startswith("wallpaper")  # also the old wallpaper.bmp

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
class Bridge(QObject):
    """Carries updates from background threads to the Qt main thread."""
    track = Signal(object)


def next_poll_delay(track):
    if track and track["is_playing"] and track["duration_ms"]:
        remaining = (track["duration_ms"] - track["progress_ms"]) / 1000
        if remaining > 0:
            return max(1.5, min(POLL_INTERVAL, remaining + TRACK_END_GRACE))
    return POLL_INTERVAL


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
        self.bridge = Bridge()
        self.worker = WallpaperWorker(self.stopped)
        self.client = SpotifyClient(auth_manager)

        self.bridge.track.connect(self.on_track)
        self.build_tray()
        atexit.register(self.restore_wallpaper)

        threading.Thread(target=self.poll_loop, daemon=True).start()
        log.info("Sally started")

    def build_tray(self):
        self.tray = QSystemTrayIcon(tray_icon(), self.app)
        menu = QMenu()
        self.now_playing = QAction("Nothing playing", menu)
        self.now_playing.setEnabled(False)
        menu.addAction(self.now_playing)
        menu.addSeparator()
        self.add_toggle(menu, "Start with Windows", "autostart", self.apply_autostart)
        menu.addSeparator()
        menu.addAction("Open log").triggered.connect(lambda: os.startfile(LOG_PATH))
        menu.addAction("Quit Sally").triggered.connect(self.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Sally")
        self.tray.show()

    def add_toggle(self, menu, label, key, after=None):
        action = QAction(label, menu)
        action.setCheckable(True)
        action.setChecked(self.settings[key])

        def toggled(on):
            self.settings[key] = on
            save_settings(self.settings)
            if after:
                after()
        action.toggled.connect(toggled)
        menu.addAction(action)

    def apply_autostart(self):
        try:
            set_autostart(self.settings["autostart"])
        except OSError as e:
            log.warning(f"Could not change autostart: {e}")

    def on_track(self, track):
        if track:
            icon = "▶" if track["is_playing"] else "⏸"
            text = f"{icon} {track['title']}"
            if track["subtitle"]:
                text += f" — {track['subtitle']}"
        else:
            text = "Nothing playing"
        self.now_playing.setText(text if len(text) <= 60 else text[:59] + "…")
        self.tray.setToolTip(f"Sally\n{text}"[:127])

    def poll_loop(self):
        last_id = None
        last_state = object()
        while not self.stopped.is_set():
            try:
                track = self.client.current()
                if track and track["is_playing"] and track["id"] != last_id:
                    last_id = track["id"]
                    log.info(f"Now: {track['title']} - {track['subtitle']}")
                    if track["art_url"]:
                        self.worker.submit(track)

                cooling = track is None and self.client.rate_limited_until > time.time()
                state = None if track is None else (track["id"], track["is_playing"])
                if state != last_state and not cooling:
                    last_state = state
                    self.bridge.track.emit(track)

                time.sleep(next_poll_delay(track))
            except Exception:
                log.exception("Poll loop error")
                time.sleep(POLL_INTERVAL)

    def restore_wallpaper(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        original = self.settings.get("original_wallpaper")
        if original and os.path.exists(original):
            set_wallpaper(original)

    def quit(self):
        self.restore_wallpaper()
        self.tray.hide()
        self.app.quit()


def run():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if already_running():
        return  # started twice (e.g. by login and by hand); the first copy keeps running

    try:
        auth_manager = load_auth()
    except RuntimeError as e:
        QMessageBox.critical(None, "Sally", str(e))
        return

    sally = Sally(app, auth_manager)  # noqa: F841 - keeps everything alive
    app.exec()


if __name__ == "__main__":
    run()
