import spotipy
from spotipy.oauth2 import SpotifyOAuth
import requests
import ctypes
import time
import os
import threading
import io
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageTk, ImageFilter
import tkinter as tk

# ─────────────────────────────────────────────
# ENV & AUTH
# ─────────────────────────────────────────────
load_dotenv()

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
    scope="user-read-currently-playing"
))

# ─────────────────────────────────────────────
# WALLPAPER SETTER
# ─────────────────────────────────────────────
def set_wallpaper(path):
    ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 1 | 2)

# ─────────────────────────────────────────────
# WALLPAPER BUILDER (STRONG GLOW)
# ─────────────────────────────────────────────
def build_wallpaper(url, song, artist):
    raw = requests.get(url).content
    cover = Image.open(io.BytesIO(raw)).convert("RGB")

    # 🎨 dominant color
    def dominant_color(img):
        img = img.resize((50, 50))
        pixels = list(img.getdata())
        r = sum(p[0] for p in pixels)//len(pixels)
        g = sum(p[1] for p in pixels)//len(pixels)
        b = sum(p[2] for p in pixels)//len(pixels)
        return r, g, b

    r, g, b = dominant_color(cover)

    # boost color
    r = min(255, int(r * 1.5))
    g = min(255, int(g * 1.5))
    b = min(255, int(b * 1.5))

    # base background (sharp)
    base = cover.resize((1920, 1080))
    bg = base.copy()

    # gradient overlay
    gradient = Image.new("L", (1920, 1080))
    draw_g = ImageDraw.Draw(gradient)

    for y in range(1080):
        val = int(200 * (y / 1080) ** 1.4)
        draw_g.line([(0, y), (1920, y)], fill=val)

    black = Image.new("RGB", (1920, 1080), (0, 0, 0))
    bg = Image.composite(black, bg, gradient)

    cx = 1920 // 2
    cy = 1080 // 2 - 60

    art_size = 320
    art = cover.resize((art_size, art_size))

    # rounded mask
    mask = Image.new("L", (art_size, art_size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, art_size, art_size],
        radius=40,
        fill=255
    )

    # ✨ STRONG GLOW
    glow_layer = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)

    # inner glow (bright core)
    glow_draw.ellipse(
        [
            cx - art_size//2 - 30,
            cy - art_size//2 - 30,
            cx + art_size//2 + 30,
            cy + art_size//2 + 30
        ],
        fill=(r, g, b, 180)
    )

    # outer glow layers
    for i in range(6):
        size = art_size + 80 + i * 60
        alpha = int(120 / (i + 1))

        glow_draw.ellipse(
            [
                cx - size//2,
                cy - size//2,
                cx + size//2,
                cy + size//2
            ],
            fill=(r, g, b, alpha)
        )

    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(40))
    bg = Image.alpha_composite(bg.convert("RGBA"), glow_layer)

    # paste album art
    x = cx - art_size // 2
    y = cy - art_size // 2
    bg.paste(art, (x, y), mask)

    draw = ImageDraw.Draw(bg)

    # fonts
    try:
        f_big = ImageFont.truetype("arialbd.ttf", 48)
        f_small = ImageFont.truetype("arial.ttf", 32)
    except:
        f_big = f_small = None

    def center_text(text, y_pos, font):
        w = draw.textlength(text, font=font)
        draw.text(((1920 - w)//2, y_pos), text, fill="white", font=font)

    center_text(song, y + art_size + 30, f_big)
    center_text(artist, y + art_size + 90, f_small)

    path = os.path.abspath("wallpaper.png")
    bg.convert("RGB").save(path, quality=95)

    return path

# ─────────────────────────────────────────────
# TRANSITION
# ─────────────────────────────────────────────
def slide_transition(old_path, new_path):
    try:
        old = Image.open(old_path).resize((1920, 1080))
    except:
        set_wallpaper(new_path)
        return

    new = Image.open(new_path).resize((1920, 1080))
    tmp = os.path.abspath("_transition.jpg")

    for i in range(10):
        alpha = i / 10
        frame = Image.blend(old, new, alpha)
        frame.save(tmp, quality=40)
        set_wallpaper(tmp)
        time.sleep(0.02)

    set_wallpaper(new_path)

# ─────────────────────────────────────────────
# FLOATING UI
# ─────────────────────────────────────────────
class GlassBubble:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0.85)

        self.visible = True

        self.root.bind("<b>", self.toggle)
        self.root.bind("<plus>", self.opacity_up)
        self.root.bind("<minus>", self.opacity_down)

        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"300x80+{sw-320}+30")

        self.canvas = tk.Canvas(self.root, width=300, height=80)
        self.canvas.pack()

        self.song = ""
        self.artist = ""
        self.art = None

        self.draw()

    def toggle(self, e=None):
        self.visible = not self.visible
        if self.visible:
            self.root.deiconify()
        else:
            self.root.withdraw()

    def opacity_up(self, e=None):
        v = self.root.attributes("-alpha")
        self.root.attributes("-alpha", min(1.0, v + 0.05))

    def opacity_down(self, e=None):
        v = self.root.attributes("-alpha")
        self.root.attributes("-alpha", max(0.3, v - 0.05))

    def draw(self):
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, 300, 80, fill="#222")

        if self.art:
            self.canvas.create_image(10, 15, image=self.art, anchor="nw")

        self.canvas.create_text(70, 20, text=self.song, fill="white", anchor="nw")
        self.canvas.create_text(70, 45, text=self.artist, fill="#aaa", anchor="nw")

    def update(self, song, artist, url):
        self.song = song
        self.artist = artist

        raw = requests.get(url).content
        img = Image.open(io.BytesIO(raw)).resize((50, 50))
        self.art = ImageTk.PhotoImage(img)

        self.draw()

    def run(self):
        self.root.mainloop()

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def run():
    bubble = GlassBubble()
    last_id = None
    current_wall = None

    def loop():
        nonlocal last_id, current_wall

        while True:
            try:
                current = sp.current_user_playing_track()

                if current and current["is_playing"]:
                    track_id = current["item"]["id"]

                    if track_id != last_id:
                        last_id = track_id

                        song = current["item"]["name"]
                        artist = current["item"]["artists"][0]["name"]
                        url = current["item"]["album"]["images"][0]["url"]

                        print("Now:", song)

                        new_wall = build_wallpaper(url, song, artist)

                        if current_wall:
                            slide_transition(current_wall, new_wall)
                        else:
                            set_wallpaper(new_wall)

                        current_wall = new_wall

                        bubble.root.after(0, lambda:
                            bubble.update(song, artist, url))

                time.sleep(2)

            except Exception as e:
                print("Error:", e)
                time.sleep(4)

    threading.Thread(target=loop, daemon=True).start()
    bubble.run()

if __name__ == "__main__":
    run()