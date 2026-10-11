import atexit
import ctypes
import io
import os
import queue
import sys
import threading
import time
from collections import OrderedDict

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from spotipy.oauth2 import SpotifyOAuth
import tkinter as tk

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
POLL_INTERVAL = 5          # seconds between Spotify checks (keeps us well under the rate limit)
TRACK_END_GRACE = 0.4      # poll this long after a track should end, so natural changes are instant
IMAGE_CACHE_SIZE = 20      # album covers kept in memory
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# SystemParametersInfoW actions/flags
SPI_GETDESKWALLPAPER = 0x0073
SPI_SETDESKWALLPAPER = 0x0014
SPIF_UPDATE_AND_BROADCAST = 0x01 | 0x02

# A redirected or legacy console can't encode emoji / non-Latin titles; never crash on print
if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

# ─────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────
def load_auth():
    load_dotenv(os.path.join(APP_DIR, ".env"))
    keys = ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI"]
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing {', '.join(missing)} in .env - copy .env.example to .env and fill it in.")

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
            print("Spotify request failed:", e)
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
            print(f"Rate limited. Cooling down {retry_after}s...")
            return None

        print("Unexpected Spotify status:", response.status_code)
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
def enable_dpi_awareness():
    # without this, a scaled display (125%, 150%) reports a smaller screen than it is
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

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
        print("Cover download failed:", e)
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

    def __init__(self, screen, stopped):
        self.screen = screen
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
                build_wallpaper(cover, track["title"], track["subtitle"], *self.screen, path)
                if self.stopped.is_set():
                    return  # quitting: don't overwrite the restored wallpaper
                if set_wallpaper(path):
                    print(f"Wallpaper updated in {time.perf_counter() - start:.2f}s")
                else:
                    print("Windows refused the wallpaper change")
            except Exception as e:
                print("Wallpaper build failed:", e)

# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────
class GlassBubble:
    W, H = 340, 80

    def __init__(self, screen_w, on_quit):
        self.on_quit = on_quit
        self.updates = queue.Queue()  # tkinter is not thread-safe: other threads only put here

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{self.W}x{self.H}+{max(0, screen_w - self.W - 30)}+30")

        self.canvas = tk.Canvas(self.root, width=self.W, height=self.H, bg="#111", highlightthickness=0)
        self.canvas.pack()
        self.title = self.canvas.create_text(15, 20, text="Sally", fill="white", anchor="nw",
                                             font=("Segoe UI", 11, "bold"), width=self.W - 30)
        self.subtitle = self.canvas.create_text(15, 45, text="Waiting for Spotify...", fill="#aaa",
                                                anchor="nw", font=("Segoe UI", 9), width=self.W - 30)

        # borderless windows can't be moved or closed by default
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<Button-3>", lambda e: self.quit())

        self.root.after(200, self._poll)

    def show(self, title, subtitle):
        self.updates.put((title, subtitle))

    def _poll(self):
        try:
            while True:
                title, subtitle = self.updates.get_nowait()
                self.canvas.itemconfigure(self.title, text=self._clip(title, 36))
                self.canvas.itemconfigure(self.subtitle, text=self._clip(subtitle, 48))
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    @staticmethod
    def _clip(text, n):
        return text if len(text) <= n else text[:n - 1].rstrip() + "…"

    def _start_drag(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def quit(self):
        self.on_quit()
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.quit()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def next_poll_delay(track):
    if track and track["is_playing"] and track["duration_ms"]:
        remaining = (track["duration_ms"] - track["progress_ms"]) / 1000
        if remaining > 0:
            return max(1.5, min(POLL_INTERVAL, remaining + TRACK_END_GRACE))
    return POLL_INTERVAL


def run():
    if sys.platform != "win32":
        sys.exit("Sally only runs on Windows (it uses the Windows wallpaper API).")

    auth_manager = load_auth()
    enable_dpi_awareness()
    screen = screen_size()

    # put the user's own wallpaper back when we exit
    original = get_wallpaper()
    if os.path.basename(original).startswith("wallpaper_") and os.path.dirname(original) == APP_DIR:
        original = None  # left over from a crashed run; nothing better to restore
    restored = threading.Event()

    def restore():
        if not restored.is_set():
            restored.set()
            if original:
                set_wallpaper(original)
    atexit.register(restore)

    client = SpotifyClient(auth_manager)
    worker = WallpaperWorker(screen, stopped=restored)
    bubble = GlassBubble(screen[0], on_quit=restore)
    print("Sally running. Right-click the overlay to quit (your wallpaper will be restored).")

    def loop():
        last_id = None
        last_state = None
        while True:
            try:
                track = client.current()

                if track and track["is_playing"]:
                    if track["id"] != last_id:
                        last_id = track["id"]
                        print("Now:", track["title"], "-", track["subtitle"])
                        if track["art_url"]:
                            worker.submit(track)
                    state = (track["title"], track["subtitle"] or " ")
                elif track:
                    state = (track["title"], "Paused")
                elif client.rate_limited_until > time.time():
                    state = last_state  # keep showing what we had while cooling down
                else:
                    state = ("Nothing playing", "Start something on Spotify")

                if state and state != last_state:
                    last_state = state
                    bubble.show(*state)

                time.sleep(next_poll_delay(track))
            except Exception as e:
                print("Error:", e)
                time.sleep(POLL_INTERVAL)

    threading.Thread(target=loop, daemon=True).start()
    bubble.run()


if __name__ == "__main__":
    run()
