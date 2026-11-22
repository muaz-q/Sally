"""Time-synced lyrics from LRCLIB (lrclib.net), a free public lyrics database.

Spotify's API has no lyrics, so this is the only outside service Sally talks to besides
Spotify. It is sent the song title, artist, album and length, nothing else.
"""
import bisect
import re

import requests

API = "https://lrclib.net/api"
HEADERS = {"User-Agent": "Sally/1.0 (desktop wallpaper app)"}
STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text):
    """'[01:02.50] words' lines -> sorted [(ms, words)]. A line may carry several stamps."""
    lines = []
    for raw in (text or "").splitlines():
        stamps = STAMP.findall(raw)
        if not stamps:
            continue
        words = STAMP.sub("", raw).strip()
        for minutes, seconds in stamps:
            lines.append((round((int(minutes) * 60 + float(seconds)) * 1000), words))
    lines.sort(key=lambda line: line[0])
    return lines


def _interpret(data):
    if not data:
        return ("none", None)
    if data.get("instrumental"):
        return ("instrumental", None)
    synced = parse_lrc(data.get("syncedLyrics"))
    if synced:
        return ("synced", synced)
    if data.get("plainLyrics"):
        return ("plain", None)
    return ("none", None)


def fetch(track):
    """-> (kind, lines): kind is synced | plain | instrumental | none. Raises on network errors."""
    seconds = round((track.get("duration_ms") or 0) / 1000)
    params = {"track_name": track["title"], "artist_name": track.get("artist") or track["subtitle"]}
    if track.get("album"):
        params["album_name"] = track["album"]
    if seconds:
        params["duration"] = seconds

    response = requests.get(f"{API}/get", params=params, headers=HEADERS, timeout=8)
    if response.status_code == 200:
        return _interpret(response.json())
    if response.status_code != 404:
        response.raise_for_status()

    # the exact match failed (album or length differ): search, prefer synced, closest length
    response = requests.get(f"{API}/search", headers=HEADERS, timeout=8,
                            params={"track_name": params["track_name"], "artist_name": params["artist_name"]})
    response.raise_for_status()
    results = [r for r in response.json() or []
               if not seconds or abs((r.get("duration") or seconds) - seconds) <= 4]
    results.sort(key=lambda r: (not r.get("syncedLyrics"), abs((r.get("duration") or seconds) - seconds)))
    return _interpret(results[0] if results else None)


def line_index(lines, progress_ms):
    """Index of the line being sung at progress_ms, or -1 before the first line."""
    return bisect.bisect_right([ms for ms, _ in lines], progress_ms) - 1
