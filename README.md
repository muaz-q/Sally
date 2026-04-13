# Sally 🎧✨

**Your Music-Responsive Desktop Companion**

Sally is a dynamic desktop application that transforms your wallpaper based on the music you're playing on Spotify. It blends album art, adaptive glow effects, and a minimal floating UI to create a clean, immersive experience.

---

## ✨ Features

* 🎧 **Real-Time Spotify Sync**
  Detects currently playing tracks instantly

* 🖼️ **Dynamic Wallpaper Engine**
  Generates wallpapers from album art

* 🎨 **Adaptive Glow Effects**
  Colors dynamically match the music artwork

* 📱 **Minimal Centered UI**
  Clean, iPhone-inspired layout

* 🎬 **Smooth Transitions**
  Seamless visual changes between tracks

* 🫧 **Floating Mini Player**
  Displays song and artist in a lightweight overlay

---

## 🛠️ Tech Stack

* **Python**
* **Tkinter**
* **Pillow (PIL)**
* **Spotipy (Spotify API)**
* **ctypes (Windows API)**
* **PyInstaller**

---

## ⚙️ Setup

### Clone the repo

```bash
git clone https://github.com/muaz-q/Sally.git
cd Sally
```

### Install dependencies

```bash
pip install -r requirements.txt
```

### Add `.env`

```env
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

### Run

```bash
python main.py
```

---

## 📦 Build as .exe

```bash
pyinstaller --onefile --noconsole --add-data ".env;." main.py
```

---

## 🎥 Demo


https://github.com/user-attachments/assets/9613e066-bc41-43c2-81a6-1c68a558c95b



---

## 🚀 Future Plans

* Audio-reactive visuals
* GPU-based rendering (no lag)
* Settings panel
* Auto-start on boot

---

## 📌 Notes

* Works best when Spotify is playing on the same device
* Optimized for Windows

---

## 👨‍💻 Author

**Muaz**
CS (AI/ML) Student

---

## ⭐ Support

If you like Sally, give the repo a star ⭐
