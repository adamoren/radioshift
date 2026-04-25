#!/usr/bin/env python3
"""
spotify_sync.py — push a radioshift daily playlist to Spotify

Usage:
  python3 spotify_sync.py [--date YYYY-MM-DD] [--tz ET] [--config config.toml]

  --date   defaults to today (in the specified timezone)
  --tz     defaults to the first timezone in config.toml

First run: will open a browser for Spotify authorization.
Tokens are saved to spotify_tokens.json alongside this script.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

try:
    import tomllib
except ImportError:
    sys.exit("Python 3.11+ required.")

try:
    from zoneinfo import ZoneInfo
except ImportError:
    sys.exit("Python 3.9+ required.")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).resolve().parent
TOKENS_FILE  = SCRIPT_DIR / "spotify_tokens.json"
DEFAULT_CONFIG = SCRIPT_DIR / "config.toml"

SPOTIFY_AUTH_URL  = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API       = "https://api.spotify.com/v1"
SCOPES = "playlist-modify-public playlist-modify-private"


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cache_raw = cfg.get("server", {}).get("cache_dir", "./cache")
    cfg["server"]["cache_dir"] = str((path.parent / cache_raw).resolve())
    return cfg


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def _request(url: str, data=None, headers=None, method=None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data else None
    req  = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _api(access_token: str, method: str, path: str, body=None) -> dict:
    url  = f"{SPOTIFY_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            text = r.read()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Spotify API {method} {path} → {e.code}: {body}") from e


def load_tokens() -> dict:
    if TOKENS_FILE.exists():
        return json.loads(TOKENS_FILE.read_text())
    return {}


def save_tokens(tokens: dict):
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


def refresh_access_token(cfg: dict, tokens: dict) -> str:
    sp = cfg["spotify"]
    import base64
    creds = base64.b64encode(f"{sp['client_id']}:{sp['client_secret']}".encode()).decode()
    result = _request(
        SPOTIFY_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    tokens["access_token"] = result["access_token"]
    if "refresh_token" in result:
        tokens["refresh_token"] = result["refresh_token"]
    save_tokens(tokens)
    return tokens["access_token"]


def authorize(cfg: dict) -> dict:
    sp = cfg["spotify"]
    params = urllib.parse.urlencode({
        "client_id":     sp["client_id"],
        "response_type": "code",
        "redirect_uri":  sp["redirect_uri"],
        "scope":         SCOPES,
    })
    auth_url = f"{SPOTIFY_AUTH_URL}?{params}"

    print("\n── Spotify Authorization ─────────────────────────────────────")
    print("Opening browser. Log in and authorize the app.")
    print("Your browser will redirect to a URL starting with:")
    print(f"  {sp['redirect_uri']}?code=...")
    print("(The page will show a connection error — that's expected.)")
    print("Copy the FULL URL from your browser's address bar and paste it here.\n")
    webbrowser.open(auth_url)

    redirected = input("Paste the full redirect URL: ").strip()
    parsed = urllib.parse.urlparse(redirected)
    code   = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        sys.exit("Could not extract authorization code from URL.")

    import base64
    creds = base64.b64encode(f"{sp['client_id']}:{sp['client_secret']}".encode()).decode()
    result = _request(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": sp["redirect_uri"],
        },
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    tokens = {
        "access_token":  result["access_token"],
        "refresh_token": result["refresh_token"],
    }
    save_tokens(tokens)
    print("✓ Authorized and tokens saved.\n")
    return tokens


def get_access_token(cfg: dict) -> str:
    tokens = load_tokens()
    if not tokens.get("refresh_token"):
        tokens = authorize(cfg)
    try:
        return refresh_access_token(cfg, tokens)
    except Exception:
        tokens = authorize(cfg)
        return tokens["access_token"]


# ---------------------------------------------------------------------------
# Playlist reading
# ---------------------------------------------------------------------------

def read_playlist(cfg: dict, tz_label: str, date: datetime.date) -> list:
    iana     = cfg["timezones"][tz_label]
    tz_obj   = ZoneInfo(iana)
    midnight = datetime.datetime.combine(date, datetime.time.min, tzinfo=tz_obj)
    utc_start = midnight.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    utc_end   = utc_start + datetime.timedelta(days=1)

    cache_dir = Path(cfg["server"]["cache_dir"])
    entries = []
    for delta in range(-1, 2):
        check = (utc_start + datetime.timedelta(days=delta)).date()
        path  = cache_dir / f"playlist_{check.strftime('%Y%m%d')}.json"
        if not path.exists():
            continue
        try:
            for e in json.loads(path.read_text()):
                utc_dt = datetime.datetime.strptime(e["utc"], "%Y-%m-%dT%H:%M:%S")
                if utc_start <= utc_dt < utc_end:
                    entries.append((utc_dt, e))
        except Exception:
            pass

    entries.sort(key=lambda x: x[0])
    return [e for _, e in entries]


# ---------------------------------------------------------------------------
# Spotify sync
# ---------------------------------------------------------------------------

def search_track(access_token: str, title: str, artist: str) -> str | None:
    q = urllib.parse.quote(f'track:"{title}" artist:"{artist}"')
    try:
        result = _api(access_token, "GET", f"/search?q={q}&type=track&limit=3&market=US")
        items  = result.get("tracks", {}).get("items", [])
        if items:
            return items[0]["uri"]
    except Exception:
        pass
    # Fallback: looser query without field filters
    q2 = urllib.parse.quote(f"{title} {artist}")
    try:
        result = _api(access_token, "GET", f"/search?q={q2}&type=track&limit=3&market=US")
        items  = result.get("tracks", {}).get("items", [])
        if items:
            return items[0]["uri"]
    except Exception:
        pass
    return None


def get_or_create_playlist(access_token: str, cfg: dict, name: str) -> str:
    sp = cfg.get("spotify", {})

    # Check for saved playlist ID
    saved_id = sp.get("playlist_id", "").strip()
    if saved_id:
        return saved_id

    # Get current user ID
    me = _api(access_token, "GET", "/me")
    user_id = me["id"]

    # Search existing playlists for matching name
    offset = 0
    while True:
        result = _api(access_token, "GET", f"/me/playlists?limit=50&offset={offset}")
        for pl in result.get("items", []):
            if pl and pl.get("name") == name:
                playlist_id = pl["id"]
                _save_playlist_id(cfg, playlist_id)
                return playlist_id
        if result.get("next"):
            offset += 50
        else:
            break

    # Create new playlist
    pl = _api(access_token, "POST", "/me/playlists", {
        "name":        name,
        "public":      False,
        "description": "Auto-generated by radioshift from Shazam-identified songs",
    })
    playlist_id = pl["id"]
    _save_playlist_id(cfg, playlist_id)
    return playlist_id


def _save_playlist_id(cfg: dict, playlist_id: str):
    """Persist the playlist ID back to config.toml so future runs reuse it."""
    config_path = DEFAULT_CONFIG
    text = config_path.read_text()
    if "playlist_id" in text:
        import re
        text = re.sub(r'playlist_id\s*=\s*"[^"]*"', f'playlist_id = "{playlist_id}"', text)
    else:
        text = text.rstrip() + f'\nplaylist_id = "{playlist_id}"\n'
    config_path.write_text(text)
    cfg.setdefault("spotify", {})["playlist_id"] = playlist_id


def sync(cfg: dict, access_token: str, entries: list, date: datetime.date, tz_label: str):
    station_name = cfg["station"].get("name", "Radio")
    playlist_name = f"{station_name} — Daily Mix"

    print(f"Searching Spotify for {len(entries)} songs...")
    uris   = []
    missed = []
    for e in entries:
        uri = search_track(access_token, e["title"], e["artist"])
        if uri:
            uris.append(uri)
            print(f"  ✓  {e['artist']} — {e['title']}")
        else:
            missed.append(e)
            print(f"  ✗  {e['artist']} — {e['title']}  (not found)")

    if not uris:
        print("\nNo tracks found on Spotify.")
        return

    playlist_id = get_or_create_playlist(access_token, cfg, playlist_name)

    # Replace playlist contents with today's songs
    # First chunk: PUT (replace), remaining chunks: POST (add)
    chunk_size = 100
    first = True
    for i in range(0, len(uris), chunk_size):
        chunk = uris[i:i + chunk_size]
        if first:
            _api(access_token, "PUT", f"/playlists/{playlist_id}/tracks", {"uris": chunk})
            first = False
        else:
            _api(access_token, "POST", f"/playlists/{playlist_id}/tracks", {"uris": chunk})

    print(f"\n✓ Synced {len(uris)}/{len(entries)} tracks to '{playlist_name}'")
    if missed:
        print(f"  {len(missed)} not found on Spotify:")
        for e in missed:
            print(f"    · {e['artist']} — {e['title']}")
    print(f"\n  Open: https://open.spotify.com/playlist/{playlist_id}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sync radioshift playlist to Spotify")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--date",   default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--tz",     default=None, help="Timezone label, e.g. ET (default: first in config)")
    parser.add_argument("--auth",   action="store_true", help="Just authorize with Spotify and save tokens, then exit")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    if args.auth:
        get_access_token(cfg)
        print("Authorization complete. Run without --auth to sync a playlist.")
        return

    tz_label = (args.tz or next(iter(cfg["timezones"]))).upper()
    if tz_label not in cfg["timezones"]:
        sys.exit(f"Unknown timezone '{tz_label}'. Options: {', '.join(cfg['timezones'])}")

    if args.date:
        date = datetime.date.fromisoformat(args.date)
    else:
        iana = cfg["timezones"][tz_label]
        date = datetime.datetime.now(ZoneInfo(iana)).date()

    print(f"Radioshift → Spotify  [{tz_label}  {date}]")

    entries = read_playlist(cfg, tz_label, date)
    if not entries:
        sys.exit(f"No playlist entries found for {date} ({tz_label}). "
                 "The daemon logs songs every 60s — check that it's running.")

    print(f"Found {len(entries)} songs in radioshift playlist.\n")

    access_token = get_access_token(cfg)
    sync(cfg, access_token, entries, date, tz_label)


if __name__ == "__main__":
    main()
