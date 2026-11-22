"""What you've listened to, for the dashboard.

Every song Sally sees is written to the log as "Now: <title> - <artist>". The log rotates
(1 MB, one backup), so each new song is also appended to history.jsonl, which keeps it for
good. load() merges both, plus the logs Sally 1.x kept in its cloned folder.
"""
import json
import re
from collections import Counter
from datetime import datetime, timedelta

from . import paths

HISTORY = paths.DATA / "history.jsonl"
LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO Now: (.+)$")
SAME_PLAY = timedelta(minutes=15)   # Sally restarting mid-song logs it again; count it once


def log_files():
    seen, files = set(), []
    for folder in [paths.DATA, *paths.LEGACY_DIRS]:
        for name in ("sally.log.1", "sally.log"):     # oldest first
            f = folder / name
            try:
                key = str(f.resolve()).lower()
            except OSError:
                continue
            if key not in seen and f.is_file():
                seen.add(key)
                files.append(f)
    return files


def sources():
    """Files whose change means the history changed (cheap check for the dashboard)."""
    files = log_files() + ([HISTORY] if HISTORY.is_file() else [])
    return tuple((str(f), f.stat().st_mtime) for f in files)


def record(track):
    """Append one play. Runs on the poll thread; a failure must never stop Sally."""
    try:
        paths.DATA.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "title": track["title"],
                 "artist": track["subtitle"], "uri": track.get("uri"), "art": track.get("art_url")}
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, KeyError, TypeError):
        pass


def _from_logs():
    out = []
    for f in log_files():
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = LOG_LINE.match(line.rstrip("\n"))
                    if m:
                        # the log joins them with " - "; titles contain that more often than artists
                        title, _, artist = m.group(2).rpartition(" - ")
                        out.append({"at": datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                                    "title": title or m.group(2), "artist": artist if title else "",
                                    "uri": None, "art": None})
        except OSError:
            continue
    return out


def _from_history():
    out = []
    try:
        with open(HISTORY, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                    e["at"] = datetime.fromisoformat(e["at"])
                    out.append(e)
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


def load():
    """All plays, newest first: [{at, title, artist, uri, art}]."""
    plays = sorted(_from_logs() + _from_history(), key=lambda e: e["at"])
    merged = []
    last_seen = {}   # "title - artist" -> index of its last kept play
    for e in plays:
        key = f"{e['title']} - {e['artist']}".lower()
        i = last_seen.get(key)
        if i is not None and e["at"] - merged[i]["at"] < SAME_PLAY and i == len(merged) - 1:
            kept = merged[i]   # the same play, seen twice (log + history, or a restart)
            kept["uri"] = kept.get("uri") or e.get("uri")
            kept["art"] = kept.get("art") or e.get("art")
            continue
        last_seen[key] = len(merged)
        merged.append(dict(e))
    merged.reverse()
    return merged


def stats(plays, now=None):
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=6)
    this_week = [p for p in plays if p["at"] >= week]
    artists = Counter()
    for p in this_week:
        for name in p["artist"].split(", "):
            if name:
                artists[name] += 1
    songs = Counter(f"{p['title']}\n{p['artist']}" for p in this_week)
    top_song = songs.most_common(1)[0] if songs else None
    return {
        "today": sum(1 for p in plays if p["at"] >= today),
        "week": len(this_week),
        "total": len(plays),
        "since": plays[-1]["at"] if plays else None,
        "top_artist": artists.most_common(1)[0] if artists else None,
        "top_song": (top_song[0].split("\n")[0], top_song[1]) if top_song else None,
    }


if __name__ == "__main__":   # quick look from a terminal: python -m sally.history
    ps = load()
    print(stats(ps))
    for p in ps[:15]:
        print(p["at"], "|", p["title"], "|", p["artist"])
    print(f"{len(ps)} plays")
