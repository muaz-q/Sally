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

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
    scope="user-read-currently-playing"
))

image_cache = {}
build_lock = False

# ─────────────────────────────────────────────
# CORE
# ─────────────────────────────────────────────
def safe_spotify_call():
    try:
        return sp.current_user_playing_track()
    except:
        return None

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
# TEXT GLOW (CLEAN)
# ─────────────────────────────────────────────
def draw_glow_text(bg, text, x, y, font, color, alpha):
    glow = Image.new("RGBA", bg.size, (0,0,0,0))
    gd = ImageDraw.Draw(glow)

    gd.text((x,y), text, font=font, fill=(255,255,255,int(100*alpha)), anchor="mm")
    glow = glow.filter(ImageFilter.GaussianBlur(4))
    bg = Image.alpha_composite(bg, glow)

    ImageDraw.Draw(bg).text(
        (x,y), text,
        font=font,
        fill=(color[0], color[1], color[2], int(255*alpha)),
        anchor="mm"
    )

    return bg

# ─────────────────────────────────────────────
# BUILD WALLPAPER FRAME
# ─────────────────────────────────────────────
def build_wallpaper_frame(cover, song, artist, alpha):

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

    # album
    bg.paste(art, (x,y), mask)

    # fonts
    try:
        f1 = ImageFont.truetype("arialbd.ttf", 30)
        f2 = ImageFont.truetype("arial.ttf", 18)
    except:
        f1 = f2 = None

    sy = y + size + 40
    ay = sy + 30

    bg = draw_glow_text(bg, song, cx, sy, f1, (255,255,255), alpha)
    bg = draw_glow_text(bg, artist, cx, ay, f2, (180,180,180), alpha)

    return bg.resize((1920,1080), Image.LANCZOS)

# ─────────────────────────────────────────────
# SAFE FADE (NO SPAMMING)
# ─────────────────────────────────────────────
def set_wallpaper_with_fade(url, song, artist):

    cover = get_image(url)
    if not cover:
        return

    path = os.path.abspath("wallpaper.bmp")

    # 🔥 only 3 frames → safe
    for alpha in [0.4, 0.7, 1.0]:
        frame = build_wallpaper_frame(cover, song, artist, alpha)
        frame.convert("RGB").save(path, "BMP")
        set_wallpaper(path)
        time.sleep(0.12)

# ─────────────────────────────────────────────
# BUBBLE
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
    last = None

    def worker(url, song, artist):
        global build_lock
        set_wallpaper_with_fade(url, song, artist)
        build_lock = False

    def loop():
        global build_lock
        nonlocal last

        while True:
            cur = safe_spotify_call()

            if cur and cur["is_playing"]:
                tid = cur["item"]["id"]

                if tid != last:
                    last = tid

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

            time.sleep(0.6)

    threading.Thread(target=loop, daemon=True).start()
    bubble.run()

if __name__ == "__main__":
    run()