"""Where Sally keeps its files: one folder per Windows user, outside the install."""
import os
import shutil
from pathlib import Path

DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Sally"
SETTINGS = DATA / "settings.json"
LOG = DATA / "sally.log"
TOKEN = DATA / "spotify-token.json"
CONTROL_KEY = DATA / "control.key"
PACKAGE = Path(__file__).resolve().parent

# Sally 1.x ran from a cloned folder and kept its files there
LEGACY_DIRS = [PACKAGE.parent, Path.home() / "Sally"]


def ensure():
    DATA.mkdir(parents=True, exist_ok=True)
    if not SETTINGS.exists():
        for folder in LEGACY_DIRS:
            # carries over the user's real wallpaper, so quitting can still put it back
            if (folder / "settings.json").is_file():
                shutil.copyfile(folder / "settings.json", SETTINGS)
                break


def is_sally_wallpaper(path):
    """True for the pictures Sally made, never for the user's own wallpaper."""
    if not path:
        return False
    p = Path(path)
    if not p.name.lower().startswith("wallpaper"):
        return False
    folder = str(p.resolve().parent).lower()
    return any(folder == str(d.resolve()).lower() for d in [DATA, *LEGACY_DIRS])
