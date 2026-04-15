import spotipy
from spotipy.oauth2 import SpotifyOAuth
import requests
import ctypes
import time
import os
import io
import threading
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
import tkinter as tk

# ─────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────
load_dotenv()

auth_manager = SpotifyOAuth(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
    scope="user-read-currently-playing"
)

sp = spotipy.Spotify(auth_manager=auth_manager)

image_cache = {}
build_lock = False
rate_limited_until = 0

# ─────────────────────────────────────────────
# SAFE SPOTIFY CALL (FINAL FIX)
# ─────────────────────────────────────────────
def safe_spotify_call():
    global rate_limited_until

    # ⛔ respect cooldown
    if time.time() < rate_limited_until:
        return None

    try:
        token = auth_manager.get_access_token(as_dict=False)

        headers = {
            "Authorization": f"Bearer {token}"
        }

        response = requests.get(
            "https://api.spotify.com/v1/me/player/currently-playing",
            headers=headers,
            timeout=5
        )

        if response.status_code == 200:
            return response.json()

        elif response.status_code == 204:
            return None

        elif response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 0))

            # 🔥 critical fix
            if retry_after <= 0:
                retry_after = 10

            print(f"⚠️ Rate limited. Cooling down {retry_after}s...")
            rate_limited_until = time.time() + retry_after
            return None

        else:
            print("Unexpected status:", response.status_code)
            return None

    except Exception as e:
        print("Error:", e)
        return None

# ─────────────────────────────────────────────
# CORE
# ─────────────────────────────────────────────
def set_wallpaper(path):
    ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 1 | 2)

def get_image(url):
    if url in image_cache:
        return image_cache[url]
    try:
        raw = requests.get(url, timeout=5).content
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        image_cache[url] = img
        return img
    except:
        return None

# ─────────────────────────────────────────────
# TEXT
# ─────────────────────────────────────────────
def draw_glow_text(bg, text, x, y, font, color):
    glow = Image.new("RGBA", bg.size, (0,0,0,0))
    gd = ImageDraw.Draw(glow)

    gd.text((x,y), text, font=font, fill=(255,255,255,120), anchor="mm")
    glow = glow.filter(ImageFilter.GaussianBlur(4))
    bg = Image.alpha_composite(bg, glow)

    ImageDraw.Draw(bg).text((x,y), text, font=font, fill=color, anchor="mm")
    return bg

# ─────────────────────────────────────────────
# WALLPAPER
# ─────────────────────────────────────────────
def build_wallpaper(url, song, artist):

    cover = get_image(url)
    if not cover:
        return None

    W, H = 960, 540

    bg = cover.resize((W, H)).filter(ImageFilter.GaussianBlur(6))
    bg = ImageEnhance.Brightness(bg).enhance(0.7)
    bg = bg.convert("RGBA")

    cx, cy = W//2, H//2
    size = 180

    art = cover.resize((size, size))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0,0,size,size],30,fill=255)

    x = cx - size//2
    y = cy - size//2

    # shadow
    shadow = Image.new("RGBA", bg.size, (0,0,0,0))
    sd = ImageDraw.Draw(shadow)

    sd.rounded_rectangle(
        [x+8, y+12, x+size+8, y+size+12],
        radius=30,
        fill=(0,0,0,120)
    )

    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    bg = Image.alpha_composite(bg, shadow)

    bg.paste(art, (x,y), mask)

    try:
        f1 = ImageFont.truetype("arialbd.ttf", 30)
        f2 = ImageFont.truetype("arial.ttf", 18)
    except:
        f1 = f2 = None

    sy = y + size + 40
    ay = sy + 30

    bg = draw_glow_text(bg, song, cx, sy, f1, (255,255,255))
    bg = draw_glow_text(bg, artist, cx, ay, f2, (180,180,180))

    bg = bg.resize((1920,1080), Image.LANCZOS)

    path = os.path.abspath("wallpaper.bmp")
    bg.convert("RGB").save(path, "BMP")

    return path

# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────
class GlassBubble:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        self.root.geometry("340x80+1500+30")
        self.canvas = tk.Canvas(self.root, width=340, height=80, bg="#111", highlightthickness=0)
        self.canvas.pack()

    def update(self, song, artist):
        self.canvas.delete("all")
        self.canvas.create_text(15,20,text=song,fill="white",anchor="nw")
        self.canvas.create_text(15,45,text=artist,fill="#aaa",anchor="nw")

    def run(self):
        self.root.mainloop()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run():
    bubble = GlassBubble()
    last_track_id = None

    CHECK_INTERVAL = 6  # 🔥 safe interval

    def worker(url, song, artist):
        global build_lock
        wall = build_wallpaper(url, song, artist)
        if wall:
            set_wallpaper(wall)
        build_lock = False

    def loop():
        global build_lock
        nonlocal last_track_id

        while True:
            try:
                cur = safe_spotify_call()

                if cur and cur.get("is_playing"):
                    track_id = cur["item"]["id"]

                    if track_id != last_track_id:
                        last_track_id = track_id

                        song = cur["item"]["name"]
                        artist = cur["item"]["artists"][0]["name"]
                        url = cur["item"]["album"]["images"][0]["url"]

                        print("Now:", song)

                        if not build_lock:
                            build_lock = True
                            threading.Thread(
                                target=worker,
                                args=(url, song, artist),
                                daemon=True
                            ).start()

                        bubble.root.after(0, lambda: bubble.update(song, artist))

                time.sleep(CHECK_INTERVAL)

            except Exception as e:
                print("Error:", e)
                time.sleep(5)

    threading.Thread(target=loop, daemon=True).start()
    bubble.run()

if __name__ == "__main__":
    run()

