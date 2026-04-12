import spotipy
from spotipy.oauth2 import SpotifyOAuth
import requests
import ctypes
import time
import os
import threading
import io
from dotenv import load_dotenv
from PIL import Image, ImageFilter, ImageDraw, ImageFont, ImageTk
import tkinter as tk
import tkinter.font as tkfont

# ─────────────────────────────────────────────
#  ENV & AUTH
# ─────────────────────────────────────────────
load_dotenv()

CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("SPOTIFY_REDIRECT_URI")
SCOPE         = "user-read-currently-playing user-modify-playback-state"

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope=SCOPE
))

# ─────────────────────────────────────────────
#  WALLPAPER HELPERS
# ─────────────────────────────────────────────
def set_wallpaper(path: str):
    ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 1 | 2)


def dominant_color(img: Image.Image) -> tuple[int, int, int]:
    """Sample centre crop for a rough dominant colour."""
    thumb = img.convert("RGB").resize((50, 50))
    pixels = list(thumb.getdata())
    r = sum(p[0] for p in pixels) // len(pixels)
    g = sum(p[1] for p in pixels) // len(pixels)
    b = sum(p[2] for p in pixels) // len(pixels)
    return r, g, b


def build_wallpaper(url: str, song: str, artist: str) -> tuple[str, tuple[int, int, int]]:
    """Download cover, build wallpaper, return (path, dominant_rgb)."""
    raw = requests.get(url).content
    cover = Image.open(io.BytesIO(raw)).convert("RGB")
    dom   = dominant_color(cover)

    base = cover.resize((1920, 1080), Image.LANCZOS)
    bg   = base.filter(ImageFilter.GaussianBlur(14))

    # Gradient vignette (bottom-heavy)
    grad = Image.new("L", (1920, 1080))
    draw_g = ImageDraw.Draw(grad)
    for y in range(1080):
        val = int(210 * (y / 1080) ** 1.4)
        draw_g.line([(0, y), (1920, y)], fill=val)
    black = Image.new("RGB", (1920, 1080), (0, 0, 0))
    bg = Image.composite(black, bg, grad)

    # Subtle album art thumbnail (bottom-right)
    thumb = cover.resize((220, 220), Image.LANCZOS)
    mask  = Image.new("L", (220, 220), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, 219, 219], radius=22, fill=255)
    bg.paste(thumb, (1650, 830), mask)

    # Song / artist text
    drw = ImageDraw.Draw(bg)
    try:
        f_big   = ImageFont.truetype("arialbd.ttf", 56)
        f_small = ImageFont.truetype("arial.ttf",   36)
    except Exception:
        f_big = f_small = None

    def shadow_text(text, pos, font, size=56):
        ox, oy = pos
        for dx, dy in [(-2,-2),(2,-2),(-2,2),(2,2),(0,3)]:
            drw.text((ox+dx, oy+dy), text, fill=(0,0,0,160), font=font)
        drw.text(pos, text, fill=(255,255,255), font=font)

    shadow_text(song,   (80, 840), f_big,   56)
    shadow_text(artist, (84, 910), f_small, 36)

    path = os.path.abspath("now_playing_wall.jpg")
    bg.save(path, quality=95)
    return path, dom


def slide_transition(old_path: str, new_path: str, steps=18, delay=0.025):
    try:
        old = Image.open(old_path).resize((1920, 1080))
    except Exception:
        set_wallpaper(new_path)
        return

    new = Image.open(new_path).resize((1920, 1080))
    tmp = os.path.abspath("_transition.jpg")

    for i in range(steps + 1):
        t      = i / steps
        ease   = t * t * (3 - 2 * t)          # smoothstep
        offset = int(ease * 1920)
        frame  = Image.new("RGB", (1920, 1080))
        frame.paste(new.crop((0,      0, offset, 1080)), (0,      0))
        frame.paste(old.crop((offset, 0, 1920,   1080)), (offset, 0))
        frame.save(tmp, quality=85)
        set_wallpaper(tmp)
        time.sleep(delay)

    set_wallpaper(new_path)


# ─────────────────────────────────────────────
#  GLASS BUBBLE  (tkinter overlay)
# ─────────────────────────────────────────────
import ctypes

class GlassBubble:
    W, H   = 300, 80
    PAD    = 20
    RADIUS = 28
    ART_S  = 50

    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-transparentcolor", "#010101")
        self.root.configure(bg="#010101")

        # Glass transparency
        self.root.attributes("-alpha", 0.85)

        # ✅ TOP RIGHT POSITION (FIXED)
        self.root.update_idletasks()  # important fix
        sw = self.root.winfo_screenwidth()

        x = sw - self.W - self.PAD
        y = self.PAD + 10

        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=self.W, height=self.H,
            bg="#010101", highlightthickness=0
        )
        self.canvas.pack()

        # State
        self.song = "Nothing playing"
        self.artist = ""
        self.dom_color = (40, 40, 40)
        self._art_photo = None

        self._build_ui()

        # ✅ ALWAYS VISIBLE (FIXED)
        self.update_visibility()

    # ---------------- UI ----------------
    def _rr(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1+r, y1, x2-r, y1,
            x2, y1, x2, y1+r,
            x2, y2-r, x2, y2,
            x2-r, y2, x1+r, y2,
            x1, y2, x1, y2-r,
            x1, y1+r, x1, y1,
            x1+r, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _build_ui(self):
        c = self.canvas
        W, H = self.W, self.H

        r, g, b = self.dom_color
        br = int(r * 0.15 + 20)
        bg = int(g * 0.15 + 20)
        bb = int(b * 0.15 + 20)

        glass = f"#{br:02x}{bg:02x}{bb:02x}"

        c.delete("all")

        self._rr(0, 0, W, H, self.RADIUS, fill=glass, outline="#ffffff", width=1)

        ax, ay = 12, (H - self.ART_S)//2

        if self._art_photo:
            c.create_image(ax, ay, image=self._art_photo, anchor="nw")
        else:
            c.create_rectangle(ax, ay, ax+self.ART_S, ay+self.ART_S,
                               fill="#222", outline="")

        tx = ax + self.ART_S + 14

        c.create_text(tx, ay + 6, text=self.song,
                      fill="#ffffff",
                      anchor="nw",
                      font=("Segoe UI", 12, "bold"),
                      width=W - tx - 10)

        c.create_text(tx, ay + 26, text=self.artist,
                      fill="#dddddd",
                      anchor="nw",
                      font=("Segoe UI", 10),
                      width=W - tx - 10)

    # ---------------- VISIBILITY (FIXED) ----------------
    def update_visibility(self):
        self.root.deiconify()  # always visible
        self.root.after(1000, self.update_visibility)

    # ---------------- UPDATE ----------------
    def update_track(self, song, artist, cover_url, dom):
        self.song = song
        self.artist = artist
        self.dom_color = dom

        raw = requests.get(cover_url).content
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        img = img.resize((self.ART_S, self.ART_S))

        mask = Image.new("L", (self.ART_S, self.ART_S), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, self.ART_S-1, self.ART_S-1], radius=10, fill=255)

        img.putalpha(mask)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        self._art_photo = ImageTk.PhotoImage(Image.open(buf))

        self._build_ui()

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────
#  BACKGROUND POLLING THREAD
# ─────────────────────────────────────────────
def poll_spotify(bubble: GlassBubble):
    last_track_id    = None
    current_wallpaper = None

    while True:
        try:
            current = sp.current_user_playing_track()

            if current and current["is_playing"]:
                track_id  = current["item"]["id"]
                cover_url = current["item"]["album"]["images"][0]["url"]
                song      = current["item"]["name"]
                artist    = current["item"]["artists"][0]["name"]

                if track_id != last_track_id:
                    last_track_id = track_id
                    print(f"▶  {song}  —  {artist}")

                    new_wall, dom = build_wallpaper(cover_url, song, artist)

                    # Wallpaper transition
                    if current_wallpaper:
                        slide_transition(current_wallpaper, new_wall)
                    else:
                        set_wallpaper(new_wall)
                    current_wallpaper = new_wall

                    # Update bubble (schedule on main thread)
                    bubble.root.after(0, lambda s=song, a=artist,
                                      u=cover_url, d=dom:
                                      bubble.update_track(s, a, u, d))

        except Exception as err:
            print("Poll error:", err)

        time.sleep(5)


# ─────────────────────────────────────────────
#  ENTRY
# ─────────────────────────────────────────────
if __name__ == "__main__":
    bubble = GlassBubble()

    t = threading.Thread(target=poll_spotify, args=(bubble,), daemon=True)
    t.start()

    bubble.run()