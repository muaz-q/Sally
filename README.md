# 🎧 Sally — Real-Time Spotify Wallpaper Engine

Sally transforms your desktop into a dynamic music experience by automatically updating your wallpaper based on the song you're currently playing on Spotify.

It creates a clean, minimal UI with album art, blurred background, and glowing text — all updated in real-time with low latency.

---

## 🎬 Demo

### 🖥️ Wallpaper in Action
![Wallpaper Demo](assets/demo-wallpaper.gif)


---

## ✨ Features

- 🎵 **Live Spotify Sync**
  - Detects currently playing track instantly

- 🖼️ **Dynamic Wallpaper Generation**
  - Uses album art to generate a blurred, aesthetic background

- ⚡ **Low Latency (~0.5–1s)**
  - Optimized pipeline for near real-time updates

- 🎯 **Minimal UI Design**
  - Centered album card with soft shadow
  - Clean typography with subtle glow

- 🧠 **Smart Rendering Pipeline**
  - Image caching
  - Reduced resolution rendering + upscale
  - Efficient blur usage

- 🪟 **Overlay Widget**
  - Floating window showing current song + artist

---

## ⚙️ Tech Stack

- Python  
- Spotipy (Spotify API)  
- Pillow (Image Processing)  
- Tkinter (Overlay UI)  
- Windows API (Wallpaper Control)

---

## 🧠 How It Works

1. Fetch current track from Spotify API  
2. Download album artwork  
3. Generate wallpaper:
   - Blur background  
   - Add album card with shadow  
   - Render glowing text  
4. Apply wallpaper using Windows API  
5. Update overlay UI  

---

## ⚡ Performance Optimizations

- Reduced render resolution (960x540 → upscale to 1080p)  
- Image caching to avoid re-downloads  
- Controlled threading (no overload)  
- Lightweight blur strategy  
- Stable file handling (BMP for fast wallpaper updates)  

---

## 🚀 Setup

### 1. Clone the repo

git clone https://github.com/muaz-q/Sally.git  
cd Sally  

---

### 2. Install dependencies

pip install spotipy pillow python-dotenv  

---

### 3. Setup Spotify API

Create a `.env` file:

SPOTIFY_CLIENT_ID=your_client_id  
SPOTIFY_CLIENT_SECRET=your_client_secret  
SPOTIFY_REDIRECT_URI=http://localhost:8888/callback  

---

### 4. Run the project

python main.py  

---

## ⚠️ Limitations

- Windows only (uses Windows wallpaper API)  
- Requires active Spotify playback  
- Wallpaper animations are limited by OS constraints  

---

## 🔮 Future Improvements

- Smooth transitions between tracks  
- Better animation system (overlay-based)  
- Multi-monitor support  
- Custom themes (dark/light/neon)  
- Lyrics / karaoke mode  

---

## 💡 Inspiration

Inspired by modern music UIs like:
- Spotify Canvas  
- Apple Music  
- iOS Lock Screen widgets  

---

## 👤 Author

Muaz  
Second-year AIML student building real-world systems 🚀  

---

## ⭐ If you like this project

Give it a star — it helps a lot!
