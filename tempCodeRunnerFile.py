import spotipy
from spotipy.oauth2 import SpotifyOAuth
import requests
import ctypes
import time
import os
import io
import threading
import tempfile
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
import tkinter as tk

# ─────────────────────────────────────────────
# ENV / AUTH
# ─────────────────────────────────────────────
load_dotenv()

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
    scope="user-read-currently-playing"
))

image_cache = {}
wallpaper_cache = {}

build_lock = False

# ─────────────────────────────────────────────
# CORE
# ─────────────────────────────────────────────
def safe_spotify_call():
    for _ in range(3):
        try:
            return sp.current_user_playing_track()
        except:
            time.sleep(0.4)
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
# COLOR
# ─────────────────────────────────────────────
def dominant_color(img):
    small = img.resize((20, 20))
    pixels = list(small.getdata())

    r = sum(p[0] for p in pixels) // len(pixels)
    g = sum(p[1] for p in pixels) // len(pixels)
    b = sum(p[2] for p in pixels) // len(pixels)

    return (
        min(255, int(r * 1.1)),
        min(255, int(g * 1.3)),
        min(255, int(b * 1.5))
    )

# ─────────────────────────────────────────────
# TEXT (reduced passes)
# ─────────────────────────────────────────────
def draw_glowing_text(bg, text, x, y, font, color, glow_color):
    for i in range(3, 0, -1):  # ↓ reduced from 6 → 3
        layer = Image.new("RGBA", bg.size, (0,0,0,0))
        ImageDraw.Draw(layer).text(
            (x,y), text,
            font=font,
            fill=(*glow_color, int(200*(i/3))),
            anchor="mm"
        )
        layer = layer.filter(ImageFilter.GaussianBlur(6*(i/3)))  # lighter blur
        bg = Image.alpha_composite(bg, layer)

    ImageDraw.Draw(bg).text((x,y), text, font=font, fill=color, anchor="mm")
    return bg

# ─────────────────────────────────────────────
# GLOW (lighter)
# ─────────────────────────────────────────────
def draw_card_glow(bg, x, y, w, h, radius, glow_color):

    outer = Image.new("RGBA", bg.size, (0,0,0,0))
    od = ImageDraw.Draw(outer)
    od.rounded_rectangle(
        [x-6, y-6, x+w+6, y+h+6],
        radius=radius+6,
        outline=(*glow_color,120),
        width=5
    )
    outer = outer.filter(ImageFilter.GaussianBlur(5))
    bg = Image.alpha_composite(bg, outer)

    sharp = Image.new("RGBA", bg.size, (0,0,0,0))
    sd = ImageDraw.Draw(sharp)
    sd.rounded_rectangle(
        [x,y,x+w,y+h],
        radius=radius,
        outline=(*glow_color,255),
        width=2
    )
    bg = Image.alpha_composite(bg, sharp)

    return bg

# ─────────────────────────────────────────────
# WALLPAPER (FAST PIPELINE)
# ─────────────────────────────────────────────
def build_wallpaper(url, song, artist):

    if url in wallpaper_cache:
        return wallpaper_cache[url]

    cover = get_image(url)
    if not cover:
        return None

    glow = dominant_color(cover)

    # 🔥 LOW RES RENDER (BIGGEST SPEED BOOST)
    W, H = 960, 540

    bg = cover.resize((W, H)).filter(ImageFilter.GaussianBlur(3))
    bg = ImageEnhance.Brightness(bg).enhance(0.9)
    bg = bg.convert("RGBA")

    cx, cy = W//2, H//2
    size = 180

    art = cover.resize((size,size))

    mask = Image.new("L",(size,size),0)
    ImageDraw.Draw(mask).rounded_rectangle([0,0,size,size],30,fill=255)

    x = cx-size//2
    y = cy-size//2

    # ambient (lighter + fewer layers)
    ambient = Image.new("RGBA",(W,H),(0,0,0,0))
    ad = ImageDraw.Draw(ambient)

    for i in range(3):
        s = size+80+i*40
        ad.ellipse([cx-s//2,cy-s//2,cx+s//2,cy+s//2],
                   fill=(*glow,int(100/(i+1))))

    ambient = ambient.filter(ImageFilter.GaussianBlur(10))
    bg = Image.alpha_composite(bg, ambient)

    bg = draw_card_glow(bg,x,y,size,size,30,glow)
    bg.paste(art,(x,y),mask)

    # fonts
    try:
        f1=ImageFont.truetype("arialbd.ttf",30)
        f2=ImageFont.truetype("arial.ttf",18)
    except:
        f1=f2=None

    sy=y+size+40
    ay=sy+30

    bg = draw_glowing_text(bg,song,cx,sy,f1,(255,255,255),glow)
    bg = draw_glowing_text(bg,artist,cx,ay,f2,(220,220,220),glow)

    # 🔥 UPSCALE TO 1080p
    bg = bg.resize((1920,1080), Image.LANCZOS)

    # 🔥 FAST BMP WRITE (NO JPEG COMPRESSION)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bmp")
    bg.convert("RGB").save(tmp.name, "BMP")

    wallpaper_cache[url] = tmp.name
    return tmp.name

# ─────────────────────────────────────────────
# BUBBLE
# ─────────────────────────────────────────────
class GlassBubble:
    def __init__(self):
        self.root=tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost",True)

        self.root.geometry("340x80+1500+30")
        self.canvas=tk.Canvas(self.root,width=340,height=80,bg="#111",highlightthickness=0)
        self.canvas.pack()

    def update(self,song,artist):
        self.canvas.delete("all")
        self.canvas.create_rectangle(0,0,340,80,fill="#111",outline="")
        self.canvas.create_text(15,20,text=song,fill="white",anchor="nw")
        self.canvas.create_text(15,45,text=artist,fill="#aaa",anchor="nw")

    def run(self):
        self.root.mainloop()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run():
    bubble=GlassBubble()
    last=None
    last_url=None

    def worker(url, song, artist):
        global build_lock
        wall = build_wallpaper(url, song, artist)
        if wall:
            set_wallpaper(wall)
        build_lock = False

    def loop():
        global build_lock
        nonlocal last, last_url

        while True:
            try:
                cur=safe_spotify_call()

                if cur and cur["is_playing"]:
                    tid=cur["item"]["id"]
                    url=cur["item"]["album"]["images"][0]["url"]

                    if tid!=last:
                        last=tid

                        if url == last_url:
                            continue
                        last_url = url

                        song=cur["item"]["name"]
                        artist=cur["item"]["artists"][0]["name"]

                        print("Now:",song)

                        if not build_lock:
                            build_lock = True
                            threading.Thread(
                                target=worker,
                                args=(url, song, artist),
                                daemon=True
                            ).start()

                        bubble.root.after(0,lambda: bubble.update(song,artist))

                time.sleep(0.6)

            except Exception as e:
                print("Error:",e)
                time.sleep(2)

    threading.Thread(target=loop,daemon=True).start()
    bubble.run()

if __name__=="__main__":
    run()