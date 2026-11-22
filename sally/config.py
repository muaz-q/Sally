"""settings.json: read and written by both the app and the `sally` command."""
import json
import os

from . import paths

DEFAULTS = {
    "autostart": True,
    "original_wallpaper": None,
    "live_mode": True,          # the animated rings, controls, lyrics and spiral
    "battery_auto": True,       # drop to the plain wallpaper on battery saver / low battery
    "battery_threshold": 20,    # percent, while unplugged
    "show_lyrics": True,
    "lyrics_collapsed": False,
    "show_top_tracks": True,
    "spotify_client_id": "",    # empty = Sally's own Spotify app
}


def load():
    try:
        # utf-8-sig: Notepad and PowerShell add a BOM, which plain utf-8 json.load rejects,
        # and falling back to defaults would forget the user's real wallpaper
        with open(paths.SETTINGS, encoding="utf-8-sig") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if "live_mode" not in saved and "show_widgets" in saved:  # Sally 1.x name
        saved["live_mode"] = saved["show_widgets"]
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save(settings):
    paths.DATA.mkdir(parents=True, exist_ok=True)
    tmp = str(paths.SETTINGS) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, paths.SETTINGS)
