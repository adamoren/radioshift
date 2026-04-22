#!/usr/bin/env python3
"""
radioshift — time-shifted internet radio for diaspora listeners

Records a live stream into hourly chunks and serves it delayed so that
listeners in different timezones hear the source station at the matching
local time of day (e.g. morning show at 8 AM Israel = 8 AM New York).

Requirements: Python 3.11+, ffmpeg (with ffplay for local playback)

Usage:
  python radioshift.py [--config config.toml] start | stop | status
"""

import os
import sys
import time
import signal
import argparse
import threading
import subprocess
import datetime
from pathlib import Path
from typing import Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import tomllib
except ImportError:
    sys.exit("Python 3.11+ is required (uses tomllib). Please upgrade Python.")

try:
    from zoneinfo import ZoneInfo
except ImportError:
    sys.exit("Python 3.9+ is required (uses zoneinfo). Please upgrade Python.")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    # Resolve cache_dir relative to the config file's directory
    cache_raw = cfg.get("server", {}).get("cache_dir", "./cache")
    cfg["server"]["cache_dir"] = str((path.parent / cache_raw).resolve())

    return cfg


# ---------------------------------------------------------------------------
# Runtime state (populated after config load)
# ---------------------------------------------------------------------------

CFG: dict = {}

def station()      -> dict: return CFG["station"]
def server_cfg()   -> dict: return CFG["server"]
def tz_routes()    -> dict: return CFG["timezones"]      # label → IANA name
def default_tz()   -> str:  return next(iter(tz_routes()))

def CACHE_DIR()    -> Path:  return Path(server_cfg()["cache_dir"])
def PID_FILE()     -> Path:  return CACHE_DIR() / "daemon.pid"
def LOG_FILE()     -> Path:  return CACHE_DIR() / "daemon.log"
def HTTP_PORT()    -> int:   return int(server_cfg().get("port", 8765))
def MAX_AGE_H()    -> int:   return int(server_cfg().get("max_age_hours", 13))
def STREAM_URL()   -> str:   return station()["stream_url"]
def SOURCE_TZ()    -> str:   return station()["source_timezone"]
def BITRATE_KBPS() -> int:   return int(station().get("bitrate_kbps", 128))
def BYTES_PER_SEC()-> int:   return BITRATE_KBPS() * 1000 // 8
def ACCENT()       -> str:   return station().get("accent_color", "#0077cc")

_active_ffmpeg: Optional[subprocess.Popen] = None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def hour_floor(dt: datetime.datetime) -> datetime.datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def chunk_path(dt: datetime.datetime) -> Path:
    return CACHE_DIR() / f"chunk_{hour_floor(dt).strftime('%Y%m%d_%H%M')}.mp3"


def parse_chunk_dt(path: Path) -> Optional[datetime.datetime]:
    try:
        parts = path.stem.split("_")   # chunk_YYYYMMDD_HHMM
        return datetime.datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M")
    except (ValueError, IndexError):
        return None


def delay_hours_for_tz(iana: str) -> float:
    """Hours to delay so listener hears the source at the same local time of day."""
    now = datetime.datetime.now(datetime.timezone.utc)
    src_off = now.astimezone(ZoneInfo(SOURCE_TZ())).utcoffset().total_seconds()
    lst_off = now.astimezone(ZoneInfo(iana)).utcoffset().total_seconds()
    return (src_off - lst_off) / 3600


def fmt_local(utc_dt: datetime.datetime, iana: str) -> str:
    aware = utc_dt.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo(iana))
    return aware.strftime("%-I:%M %p %Z")


def fmt_source(utc_dt: datetime.datetime) -> str:
    return fmt_local(utc_dt, SOURCE_TZ())


# ---------------------------------------------------------------------------
# Daemon helpers
# ---------------------------------------------------------------------------

def log(msg: str):
    ts = utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with open(LOG_FILE(), "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def clean_old_chunks():
    cutoff = utcnow() - datetime.timedelta(hours=MAX_AGE_H())
    for f in sorted(CACHE_DIR().glob("chunk_*.mp3")):
        dt = parse_chunk_dt(f)
        if dt and dt < cutoff:
            try:
                f.unlink()
                log(f"Deleted {f.name}")
            except OSError:
                pass


def read_pid() -> Optional[int]:
    try:
        return int(PID_FILE().read_text().strip())
    except Exception:
        return None


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _tz_tabs(active_tz: str) -> str:
    tabs = ""
    for label, iana in tz_routes().items():
        slug = label.lower()
        active = ' class="active"' if slug == active_tz else ""
        tabs += f'<a href="/{slug}"{active}>{label}</a>\n            '
    return tabs


def html_player(tz: str, iana: str, delay: float,
                now: datetime.datetime, target_dt: datetime.datetime) -> str:
    s = station()
    name       = s.get("name", "Radio")
    name_local = s.get("name_local", "")
    desc       = s.get("description", "")
    accent     = ACCENT()
    source_time = fmt_source(target_dt)
    local_time  = fmt_local(target_dt, iana)
    tz_label    = tz.upper()
    tabs        = _tz_tabs(tz)
    display_name = name_local if name_local else name

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{name}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{ --accent: {accent}; }}
    body {{
      background: #0d0d0d;
      color: #f0f0f0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      min-height: 100dvh;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .card {{
      background: #181818;
      border-radius: 20px;
      padding: 40px 36px 32px;
      width: min(420px, 94vw);
      box-shadow: 0 20px 60px rgba(0,0,0,.6);
    }}
    .header {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 28px;
    }}
    .logo-wrap {{
      width: 64px; height: 64px;
      background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 60%, black), var(--accent));
      border-radius: 16px;
      display: flex; align-items: center; justify-content: center;
      font-size: 32px;
      flex-shrink: 0;
    }}
    .name {{ font-size: 26px; font-weight: 700; letter-spacing: -.5px; }}
    .sub  {{ font-size: 13px; color: #777; margin-top: 2px; }}
    .times {{
      background: #111;
      border-radius: 12px;
      padding: 14px 18px;
      margin-bottom: 24px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px 0;
    }}
    .times .label {{ font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: .05em; }}
    .times .value {{ font-size: 17px; font-weight: 600; margin-top: 2px; }}
    .times .value.src {{ color: var(--accent); }}
    .delay-note {{
      grid-column: 1 / -1;
      font-size: 12px;
      color: #444;
      padding-top: 8px;
      border-top: 1px solid #222;
      margin-top: 4px;
    }}
    .dot {{
      display: inline-block;
      width: 7px; height: 7px;
      border-radius: 50%;
      background: var(--accent);
      margin-right: 5px;
      animation: pulse 1.6s ease-in-out infinite;
    }}
    @keyframes pulse {{ 0%,100%{{ opacity:1 }} 50%{{ opacity:.25 }} }}
    audio {{
      width: 100%;
      height: 48px;
      margin-bottom: 20px;
      border-radius: 8px;
      accent-color: var(--accent);
    }}
    .tz-tabs {{ display: flex; gap: 8px; }}
    .tz-tabs a {{
      flex: 1;
      text-align: center;
      padding: 10px 0;
      border-radius: 10px;
      background: #222;
      color: #888;
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
      letter-spacing: .03em;
      transition: background .15s, color .15s;
    }}
    .tz-tabs a.active, .tz-tabs a:hover {{
      background: var(--accent);
      color: #fff;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="logo-wrap">&#128251;</div>
      <div>
        <div class="name">{display_name}</div>
        <div class="sub">{name if name_local else ''}{' &middot; ' if name_local else ''}{desc}</div>
      </div>
    </div>

    <div class="times">
      <div>
        <div class="label">Source time</div>
        <div class="value src">{source_time}</div>
      </div>
      <div>
        <div class="label">Your time ({tz_label})</div>
        <div class="value">{local_time}</div>
      </div>
      <div class="delay-note">
        <span class="dot"></span>Playing {delay:.0f}h behind live
      </div>
    </div>

    <audio controls autoplay>
      <source src="/stream/{tz}" type="audio/mpeg">
    </audio>

    <div class="tz-tabs">
      {tabs}
    </div>
  </div>
</body>
</html>"""


def html_buffering(tz: str, iana: str, delay: float, mins: int) -> str:
    s = station()
    name       = s.get("name", "Radio")
    name_local = s.get("name_local", "")
    desc       = s.get("description", "")
    accent     = ACCENT()
    tz_label   = tz.upper()
    tabs       = _tz_tabs(tz)
    hours, rmins = divmod(mins, 60)
    eta  = f"{hours}h {rmins}m" if hours else f"{rmins}m"
    pct  = max(2, min(98, round((1 - mins / max(1, delay * 60)) * 100)))
    display_name = name_local if name_local else name

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>{name} &mdash; Buffering</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{ --accent: {accent}; }}
    body {{
      background: #0d0d0d;
      color: #f0f0f0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      min-height: 100dvh;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .card {{
      background: #181818;
      border-radius: 20px;
      padding: 40px 36px 32px;
      width: min(420px, 94vw);
      box-shadow: 0 20px 60px rgba(0,0,0,.6);
      text-align: center;
    }}
    .header {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 32px;
      text-align: left;
    }}
    .logo-wrap {{
      width: 64px; height: 64px;
      background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 60%, black), var(--accent));
      border-radius: 16px;
      display: flex; align-items: center; justify-content: center;
      font-size: 32px;
      flex-shrink: 0;
    }}
    .name {{ font-size: 26px; font-weight: 700; letter-spacing: -.5px; }}
    .sub  {{ font-size: 13px; color: #777; margin-top: 2px; }}
    .spinner {{
      width: 56px; height: 56px;
      border: 3px solid #222;
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 1s linear infinite;
      margin: 0 auto 20px;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .eta {{ font-size: 28px; font-weight: 700; margin-bottom: 6px; }}
    .eta-sub {{ font-size: 13px; color: #555; margin-bottom: 28px; }}
    .bar-wrap {{ background: #222; border-radius: 99px; height: 6px; margin-bottom: 32px; overflow: hidden; }}
    .bar {{ height: 100%; background: var(--accent); border-radius: 99px; width: {pct}%; }}
    .note {{ font-size: 12px; color: #444; margin-bottom: 24px; }}
    .tz-tabs {{ display: flex; gap: 8px; }}
    .tz-tabs a {{
      flex: 1; text-align: center; padding: 10px 0; border-radius: 10px;
      background: #222; color: #888; text-decoration: none;
      font-size: 13px; font-weight: 600;
      transition: background .15s, color .15s;
    }}
    .tz-tabs a.active, .tz-tabs a:hover {{ background: var(--accent); color: #fff; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="logo-wrap">&#128251;</div>
      <div style="text-align:left">
        <div class="name">{display_name}</div>
        <div class="sub">{name if name_local else ''}{' &middot; ' if name_local else ''}{desc}</div>
      </div>
    </div>

    <div class="spinner"></div>
    <div class="eta">{eta}</div>
    <div class="eta-sub">until {tz_label} stream is ready ({delay:.0f}h buffer)</div>
    <div class="bar-wrap"><div class="bar"></div></div>
    <div class="note">Page refreshes every minute.</div>

    <div class="tz-tabs">
      {tabs}
    </div>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class TimeshiftHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path.startswith("/stream/"):
            slug = path[len("/stream/"):]
            iana = tz_routes().get(slug.upper())
            if iana:
                self._serve_stream(slug, iana)
            else:
                self._text(404, "Unknown stream.\n")
            return

        if path == "/":
            path = f"/{default_tz().lower()}"

        slug = path.lstrip("/")
        iana = tz_routes().get(slug.upper())
        if iana is None:
            self._text(404, "Not found.\n")
            return

        self._serve_page(slug, iana)

    def _serve_page(self, tz: str, iana: str):
        delay = delay_hours_for_tz(iana)
        now = utcnow()
        target_dt = now - datetime.timedelta(hours=delay)
        target_chunk = chunk_path(target_dt)

        if not target_chunk.exists():
            chunks = sorted(CACHE_DIR().glob("chunk_*.mp3"))
            first_dt = parse_chunk_dt(chunks[0]) if chunks else None
            if first_dt:
                avail_at = first_dt + datetime.timedelta(hours=delay + 1)
                mins = max(0, int((avail_at - now).total_seconds() / 60))
            else:
                mins = int(delay * 60)
            self._html(503, html_buffering(tz, iana, delay, mins))
        else:
            self._html(200, html_player(tz, iana, delay, now, target_dt))

    def _serve_stream(self, tz: str, iana: str):
        delay = delay_hours_for_tz(iana)
        now = utcnow()
        target_dt = now - datetime.timedelta(hours=delay)
        target_chunk = chunk_path(target_dt)

        if not target_chunk.exists():
            self._text(503, "Stream not ready yet.\n")
            return

        seek_sec = int((target_dt - hour_floor(target_dt)).total_seconds())
        seek_bytes = seek_sec * BYTES_PER_SEC()

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("icy-name", f"{station().get('name', 'Radio')} ({tz.upper()} -{delay:.0f}h)")
        self.send_header("icy-br", str(BITRATE_KBPS()))
        self.end_headers()

        current = target_chunk
        first = True
        try:
            while True:
                if not current.exists():
                    time.sleep(2)
                    continue
                with open(current, "rb") as f:
                    if first:
                        f.seek(seek_bytes)
                        first = False
                    while True:
                        data = f.read(8192)
                        if not data:
                            break
                        self.wfile.write(data)
                        self.wfile.flush()
                dt = parse_chunk_dt(current)
                if dt is None:
                    break
                current = chunk_path(dt + datetime.timedelta(hours=1))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _html(self, code: int, body: str):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _text(self, code: int, body: str):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def run_http_server():
    server = HTTPServer(("127.0.0.1", HTTP_PORT()), TimeshiftHandler)
    log(f"HTTP server on 127.0.0.1:{HTTP_PORT()}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Recording loop
# ---------------------------------------------------------------------------

def recording_loop():
    global _active_ffmpeg
    CACHE_DIR().mkdir(parents=True, exist_ok=True)
    log(f"Recording {STREAM_URL()}")

    while True:
        now = utcnow()
        next_hour = hour_floor(now + datetime.timedelta(hours=1))
        duration = max(1, int((next_hour - now).total_seconds()))
        out = chunk_path(now)

        log(f"Recording {out.name} for {duration}s")
        _active_ffmpeg = subprocess.Popen(
            ["ffmpeg", "-y", "-i", STREAM_URL(), "-t", str(duration), "-c", "copy", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        while True:
            ret = _active_ffmpeg.poll()
            if ret is not None:
                log(f"ffmpeg exited ({ret}), retrying in 5s")
                time.sleep(5)
                break
            if utcnow() >= next_hour:
                _active_ffmpeg.terminate()
                try:
                    _active_ffmpeg.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _active_ffmpeg.kill()
                log(f"Chunk {out.name} complete")
                break
            time.sleep(1)

        clean_old_chunks()


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------

def _sigterm(sig, frame):
    log("SIGTERM - shutting down")
    if _active_ffmpeg and _active_ffmpeg.poll() is None:
        _active_ffmpeg.terminate()
        try:
            _active_ffmpeg.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _active_ffmpeg.kill()
    PID_FILE().unlink(missing_ok=True)
    sys.exit(0)


def daemonize():
    CACHE_DIR().mkdir(parents=True, exist_ok=True)
    if (pid := os.fork()) > 0:
        sys.exit(0)
    os.setsid()
    if (pid := os.fork()) > 0:
        sys.exit(0)

    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)
    lfd = os.open(str(LOG_FILE()), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(lfd, sys.stdout.fileno())
    os.dup2(lfd, sys.stderr.fileno())
    os.close(lfd)

    PID_FILE().write_text(str(os.getpid()))


def daemon_main():
    signal.signal(signal.SIGTERM, _sigterm)
    CACHE_DIR().mkdir(parents=True, exist_ok=True)
    log(f"Daemon started (PID {os.getpid()})")
    threading.Thread(target=run_http_server, daemon=True).start()
    recording_loop()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_start():
    pid = read_pid()
    if pid and is_running(pid):
        print(f"Already running (PID {pid})")
        return
    name = station().get("name", "radio")
    print(f"Starting {name} timeshift daemon (port {HTTP_PORT()}, cache {CACHE_DIR()})")
    daemonize()
    daemon_main()


def cmd_stop():
    pid = read_pid()
    if not pid:
        print("Not running (no PID file)")
        return
    if not is_running(pid):
        print(f"Not running (stale PID {pid})")
        PID_FILE().unlink(missing_ok=True)
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.5)
        if not is_running(pid):
            print(f"Stopped (PID {pid})")
            return
    os.kill(pid, signal.SIGKILL)
    PID_FILE().unlink(missing_ok=True)
    print(f"Killed (PID {pid})")


def cmd_status():
    pid = read_pid()
    if pid and is_running(pid):
        print(f"Daemon:  running (PID {pid})  port {HTTP_PORT()}")
    elif pid:
        print(f"Daemon:  not running (stale PID {pid})")
    else:
        print("Daemon:  not running")

    chunks = sorted(CACHE_DIR().glob("chunk_*.mp3"))
    if chunks:
        mb = sum(c.stat().st_size for c in chunks) / 1024 / 1024
        a = parse_chunk_dt(chunks[0])
        b = parse_chunk_dt(chunks[-1])
        span = f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')} UTC" if a and b else "?"
        print(f"Cached:  {len(chunks)} chunk(s)  {mb:.0f} MB  [{span}]")
    else:
        print("Cached:  none")

    now = utcnow()
    print("Streams:")
    for label, iana in tz_routes().items():
        delay = delay_hours_for_tz(iana)
        target = now - datetime.timedelta(hours=delay)
        tchunk = chunk_path(target)
        if tchunk.exists():
            seek = int((target - hour_floor(target)).total_seconds())
            print(f"  /{label.lower()}  (-{delay:.0f}h)  ready  [{tchunk.name} +{seek}s]")
        else:
            fdt = parse_chunk_dt(chunks[0]) if chunks else None
            if fdt:
                mins = max(0, int(((fdt + datetime.timedelta(hours=delay+1)) - now).total_seconds() / 60))
                print(f"  /{label.lower()}  (-{delay:.0f}h)  buffering ~{mins}min")
            else:
                print(f"  /{label.lower()}  (-{delay:.0f}h)  no data")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="radioshift - time-shifted internet radio")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="Path to config TOML file (default: config.toml next to script)")
    parser.add_argument("command", choices=["start", "stop", "status"])
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        sys.exit(f"Config file not found: {config_path}\nCopy config.example.toml to config.toml and edit it.")

    global CFG
    CFG = load_config(config_path)

    {"start": cmd_start, "stop": cmd_stop, "status": cmd_status}[args.command]()


if __name__ == "__main__":
    main()
