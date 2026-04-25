#!/usr/bin/env python3
"""
Retroactively build playlist entries by scanning existing chunks.
Run once after enabling playlist logging to populate today's data.

Usage: python3.11 playlist_backfill.py [--config config.toml] [--interval 90]
"""

import argparse
import datetime
import json
import subprocess
import sys
import threading
from pathlib import Path

try:
    import tomllib
except ImportError:
    sys.exit("Python 3.11+ required.")

try:
    from zoneinfo import ZoneInfo
except ImportError:
    sys.exit("Python 3.9+ required.")

SCRIPT_DIR = Path(__file__).resolve().parent
_RECOGNIZE_PY = SCRIPT_DIR / "recognize.py"

_cache: dict = {}
_cache_lock = threading.Lock()
_playlist_lock = threading.Lock()


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cache_raw = cfg.get("server", {}).get("cache_dir", "./cache")
    cfg["server"]["cache_dir"] = str((path.parent / cache_raw).resolve())
    return cfg


def recognize(chunk: Path, seek_sec: int) -> dict:
    key = (chunk.name, seek_sec)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        ffmpeg = subprocess.Popen(
            ["ffmpeg", "-ss", str(seek_sec), "-i", str(chunk),
             "-t", "10", "-ar", "44100", "-ac", "1", "-f", "mp3", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        audio, _ = ffmpeg.communicate(timeout=15)
        if not audio:
            return {"status": "error"}
        proc = subprocess.run(
            ["python3.11", str(_RECOGNIZE_PY)],
            input=audio, capture_output=True, timeout=20,
        )
        result = json.loads(proc.stdout) if proc.stdout else {"status": "error"}
    except Exception as e:
        result = {"status": "error", "reason": str(e)}
    with _cache_lock:
        _cache[key] = result
    return result


def playlist_path(cache_dir: Path, utc_date: datetime.date) -> Path:
    return cache_dir / f"playlist_{utc_date.strftime('%Y%m%d')}.json"


def append_entry(cache_dir: Path, entry: dict):
    path = playlist_path(cache_dir, datetime.date.fromisoformat(entry["utc"][:10]))
    with _playlist_lock:
        entries = []
        if path.exists():
            try:
                entries = json.loads(path.read_text())
            except Exception:
                pass
        # Skip if same song already logged within 2 minutes
        if entries:
            last = entries[-1]
            if last["title"] == entry["title"] and last["artist"] == entry["artist"]:
                return
            last_dt = datetime.datetime.strptime(last["utc"], "%Y-%m-%dT%H:%M:%S")
            this_dt = datetime.datetime.strptime(entry["utc"], "%Y-%m-%dT%H:%M:%S")
            if abs((this_dt - last_dt).total_seconds()) < 120 and last["title"] == entry["title"]:
                return
        entries.append(entry)
        entries.sort(key=lambda e: e["utc"])
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def chunk_floor_dt(dt: datetime.datetime) -> datetime.datetime:
    base = dt.replace(minute=0, second=0, microsecond=0)
    if dt.minute < 50:
        base -= datetime.timedelta(hours=1)
    return base.replace(minute=50)


def parse_chunk_dt(path: Path) -> datetime.datetime | None:
    try:
        stem = path.stem.replace(".mod", "")
        parts = stem.split("_")
        return datetime.datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M")
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.toml"))
    parser.add_argument("--interval", type=int, default=90,
                        help="Seconds between recognition probes within each chunk (default: 90)")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    cache_dir = Path(cfg["server"]["cache_dir"])
    iana = next(iter(cfg["timezones"].values()))
    source_iana = cfg["station"]["source_timezone"]

    # Get the source timezone UTC offset at a reference time
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    src_off = now_utc.astimezone(ZoneInfo(source_iana)).utcoffset().total_seconds()
    lst_off = now_utc.astimezone(ZoneInfo(iana)).utcoffset().total_seconds()
    delay_h = (src_off - lst_off) / 3600

    chunks = sorted([
        f for f in cache_dir.glob("chunk_*.mp3")
        if f.stat().st_size >= 1_000_000 and ".mod" not in f.stem
    ])

    if not chunks:
        sys.exit("No chunks found.")

    print(f"Found {len(chunks)} chunks. Scanning every {args.interval}s per chunk.")
    print("This will take a few minutes — Shazam is called for each probe.\n")

    total_logged = 0
    last_song = {"title": None, "artist": None}

    for chunk in chunks:
        chunk_dt = parse_chunk_dt(chunk)
        if chunk_dt is None:
            continue

        chunk_dur = int(chunk.stat().st_size / (cfg["station"].get("bitrate_kbps", 128) * 1000 // 8))
        chunk_dur = min(chunk_dur, 3600)

        for seek in range(0, chunk_dur, args.interval):
            # Compute what UTC wall-clock time this position corresponds to (for ET listener)
            audio_utc = chunk_dt + datetime.timedelta(seconds=seek)
            wall_utc  = audio_utc + datetime.timedelta(hours=delay_h)

            result = recognize(chunk, seek)
            if result.get("status") != "ok":
                print(f"  {chunk.name} +{seek:4d}s  ✗  (not recognized)")
                continue

            title  = result.get("title", "")
            artist = result.get("artist", "")
            if not title:
                continue

            changed = (title != last_song["title"] or artist != last_song["artist"])
            marker  = "→" if changed else "·"
            print(f"  {chunk.name} +{seek:4d}s  {marker}  {artist} — {title}")

            if changed:
                last_song = {"title": title, "artist": artist}
                entry = {
                    "utc":    wall_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                    "title":  title,
                    "artist": artist,
                    "cover":  result.get("cover", ""),
                }
                append_entry(cache_dir, entry)
                total_logged += 1

    print(f"\nDone. Logged {total_logged} song transitions.")
    print("Run spotify_sync.py to push to Spotify.")


if __name__ == "__main__":
    main()
