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
import urllib.request
from pathlib import Path
from typing import Optional
import json as _json
import socketserver
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
def STREAM_LAG_S() -> int:   return int(server_cfg().get("stream_lag_seconds", 0))
def STREAM_URL()   -> str:   return station()["stream_url"]
def SOURCE_TZ()    -> str:   return station()["source_timezone"]
def BITRATE_KBPS() -> int:   return int(station().get("bitrate_kbps", 128))
def BYTES_PER_SEC()-> int:   return BITRATE_KBPS() * 1000 // 8
def ACCENT()       -> str:   return station().get("accent_color", "#0077cc")
def SKIP_NEWS()          -> bool: return bool(station().get("skip_news", False))
def NEWS_WINDOW_START()  -> int:  return int(server_cfg().get("news_window_start_s", _NEWS_WINDOW_START))
def NEWS_WINDOW_END()    -> int:  return int(server_cfg().get("news_window_end_s",   _NEWS_WINDOW_END))

_active_ffmpeg: Optional[subprocess.Popen] = None

_recognition_cache: dict = {}
_recognition_lock  = threading.Lock()
_RECOGNIZE_PY = Path(__file__).resolve().parent / "recognize.py"

def recognize_track(chunk: Path, seek_sec: int) -> dict:
    cache_key = (str(chunk.name), seek_sec // 60)
    with _recognition_lock:
        cached = _recognition_cache.get(cache_key)
    if cached is not None:
        return cached

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
        result = _json.loads(proc.stdout) if proc.stdout else {"status": "error"}
    except Exception as e:
        result = {"status": "error", "reason": str(e)}

    with _recognition_lock:
        _recognition_cache[cache_key] = result
        if len(_recognition_cache) > 500:
            for k in list(_recognition_cache)[:100]:
                del _recognition_cache[k]
    return result


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def hour_floor(dt: datetime.datetime) -> datetime.datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


# Chunks start at :50 so the :56–:07 informational block always falls within one chunk.
_CHUNK_OFFSET_MIN = 50

def chunk_floor(dt: datetime.datetime) -> datetime.datetime:
    """Round dt down to the most recent chunk boundary (:50 of each hour)."""
    base = hour_floor(dt - datetime.timedelta(minutes=_CHUNK_OFFSET_MIN))
    return base + datetime.timedelta(minutes=_CHUNK_OFFSET_MIN)


def chunk_path(dt: datetime.datetime) -> Path:
    return CACHE_DIR() / f"chunk_{chunk_floor(dt).strftime('%Y%m%d_%H%M')}.mp3"


def parse_chunk_dt(path: Path) -> Optional[datetime.datetime]:
    try:
        stem = path.stem.replace(".mod", "")
        parts = stem.split("_")   # chunk_YYYYMMDD_HHMM
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
    return aware.strftime("%-I:%M:%S %p %Z")


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


MIN_CHUNK_BYTES = 1 * 1024 * 1024  # 1 MB — ignore stub files from interrupted starts


def valid_chunks() -> list:
    """Return chunks that contain meaningful audio, sorted oldest first."""
    return [
        f for f in sorted(CACHE_DIR().glob("chunk_*.mp3"))
        if f.stat().st_size >= MIN_CHUNK_BYTES and ".mod" not in f.stem
    ]


def _is_aligned(path: Path) -> bool:
    """Return True if this chunk file is on a :50 boundary."""
    dt = parse_chunk_dt(path)
    return dt is not None and dt.minute == _CHUNK_OFFSET_MIN


def clean_old_chunks():
    cutoff = utcnow() - datetime.timedelta(hours=MAX_AGE_H())
    current = chunk_path(utcnow())
    for f in sorted(CACHE_DIR().glob("chunk_*.mp3")):
        dt = parse_chunk_dt(f)
        if not dt:
            continue
        try:
            if dt < cutoff:
                f.unlink()
                for sidecar in (mod_chunk(f),):
                    if sidecar.exists():
                        sidecar.unlink()
                log(f"Deleted old chunk: {f.name}")
            elif f != current and f.stat().st_size < MIN_CHUNK_BYTES:
                f.unlink()
                log(f"Deleted stub chunk: {f.name}")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# News detection
# ---------------------------------------------------------------------------

_NEWS_SILENCE_DB   = -45.0  # dB — sustained quiet that marks news start (songs dip to ~-35)
_NEWS_SILENCE_RUN  = 2      # consecutive seconds below threshold required (avoids song dips)
_NEWS_MAX_DUR      = 600    # seconds — cap; longer "segments" are music not news
_NEWS_END_DB       = -55.0  # dB — deep silence that may signal program transition
_NEWS_END_LOUD_N   = 3      # of next 8 seconds must be above _NEWS_MUSIC_DB to confirm transition
_NEWS_SCAN_MAX     = 3600   # seconds — scan full chunk (runs at ~10x so ~6 min/chunk)
_NEWS_WINDOW_START = 240    # seconds into chunk where news can begin (:54 past :50 start)
_NEWS_WINDOW_END   = 1020   # seconds into chunk where news must have started by (:07 past hour)
_NEWS_MUSIC_DB     = -20.0  # dB — RMS threshold for "music resumed"
_NEWS_MUSIC_RUN    = 8      # consecutive seconds above threshold = music
_NEWS_MUSIC_MEAN_DB = -19.0 # dB — 30-sec sliding mean above this = "music" (fallback start search)


def _rms_per_second(path: Path, duration: int) -> list:
    """Return list of per-second RMS dB values for the first `duration` seconds."""
    import re as _re
    result = subprocess.run(
        ["ffmpeg", "-i", str(path), "-t", str(duration),
         "-af", "astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # Parse frame timestamps and RMS levels together; bucket by integer second
    buckets: dict = {}
    cur_sec = None
    for line in result.stdout.splitlines():
        m = _re.search(r"pts_time:([\d.]+)", line)
        if m:
            cur_sec = int(float(m.group(1)))
            continue
        m = _re.search(r"RMS_level=(-?\d+\.?\d*)", line)
        if m and cur_sec is not None:
            try:
                v = float(m.group(1))
                buckets.setdefault(cur_sec, []).append(v)
            except ValueError:
                pass
    if not buckets:
        return []
    max_sec = max(buckets)
    return [
        min(buckets[s]) if s in buckets else 0.0
        for s in range(max_sec + 1)
    ]


FILL_TRACK = Path(__file__).resolve().parent / "news_break_fill.mp3"


def mod_chunk(chunk: Path) -> Path:
    return chunk.with_suffix(".mod.mp3")


def _find_news_segments(levels: list) -> list:
    """
    Scan per-second RMS levels for news/informational segments.
    Returns list of (start_sec, end_sec) tuples.
    A segment starts with a silence dip below _NEWS_SILENCE_DB within a
    short window, and ends when sustained music resumes.
    """
    segments = []
    i = 0
    n = len(levels)
    while i < n:
        # Only trigger within the news window
        if i < NEWS_WINDOW_START() or i > NEWS_WINDOW_END():
            i += 1
            continue
        # Require sustained silence (not a single quiet song moment)
        if all(i + k < n and levels[i + k] < _NEWS_SILENCE_DB for k in range(_NEWS_SILENCE_RUN)):
            seg_start = max(0, i - 2)
            # Find where news ends — two triggers, whichever comes first:
            # 1. Program-transition: deep silence + immediate loud burst (jingle)
            # 2. Fallback: sustained loud music
            run = 0
            j = i + _NEWS_SILENCE_RUN
            seg_end = None
            while j < n:
                # Trigger 1: deep silence followed by loud jingle
                if levels[j] < _NEWS_END_DB:
                    loud = sum(1 for k in range(1, 9) if j + k < n and levels[j + k] > _NEWS_MUSIC_DB)
                    if loud >= _NEWS_END_LOUD_N:
                        seg_end = j
                        break
                # Trigger 2: sustained music
                if levels[j] > _NEWS_MUSIC_DB:
                    run += 1
                    if run >= _NEWS_MUSIC_RUN:
                        seg_end = j - _NEWS_MUSIC_RUN + 1
                        break
                else:
                    run = 0
                j += 1
            if seg_end is not None:
                dur = seg_end - seg_start
                if 30 < dur <= _NEWS_MAX_DUR:
                    segments.append((seg_start, seg_end))
                i = seg_end
            else:
                break
        i += 1

    # Fallback: if primary found nothing, locate the end trigger and search backward
    # using a 30-sec sliding mean to find where music faded into news (no onset silence).
    if not segments:
        window_start = NEWS_WINDOW_START()
        window_end   = NEWS_WINDOW_END()
        end_j = None
        j = window_start
        while j < min(n, window_end + 120):
            if levels[j] < _NEWS_END_DB:
                loud = sum(1 for k in range(1, 9) if j + k < n and levels[j + k] > _NEWS_MUSIC_DB)
                if loud >= _NEWS_END_LOUD_N:
                    end_j = j
                    break
            j += 1
        if end_j is not None:
            W = 30  # sliding-mean window size in seconds
            seg_start = window_start
            lo = max(window_start - 1, end_j - _NEWS_MAX_DUR - 1)
            for i in range(end_j - W, lo, -1):
                if i + W <= n:
                    mean_db = sum(levels[i:i + W]) / W
                    if mean_db > _NEWS_MUSIC_MEAN_DB:
                        seg_start = i + W
                        break
            dur = end_j - seg_start
            if 30 < dur <= _NEWS_MAX_DUR:
                segments.append((seg_start, end_j))

    return segments


def detect_and_save_news_skip(chunk: Path):
    """Scan completed chunk for news/informational segments and bake a .mod.mp3."""
    mc = mod_chunk(chunk)
    if mc.exists():
        return  # already processed

    log(f"Scanning {chunk.name} for news segments...")
    levels = _rms_per_second(chunk, _NEWS_SCAN_MAX)
    segments = _find_news_segments(levels)

    if not segments:
        log(f"No news segments found in {chunk.name}")
        return

    for start, end in segments:
        log(f"  News segment: {start}s–{end}s ({end-start}s)")

    if not FILL_TRACK.exists():
        log("news_break_fill.mp3 not found — cannot substitute news")
        return

    # Build ffmpeg filter: splice fill track into each news segment
    inputs = ["ffmpeg", "-i", str(chunk)]
    filter_parts = []
    concat_inputs = []
    seg_idx = 0
    prev_end = 0

    for start, end in segments:
        dur = end - start
        # Pre-news segment
        label_pre = f"pre{seg_idx}"
        filter_parts.append(
            f"[0:a]atrim={prev_end}:{start},asetpts=PTS-STARTPTS[{label_pre}]"
        )
        concat_inputs.append(f"[{label_pre}]")
        # Fill segment (trimmed from fill track)
        inputs += ["-i", str(FILL_TRACK)]
        fill_idx = seg_idx + 1
        label_fill = f"fill{seg_idx}"
        filter_parts.append(
            f"[{fill_idx}:a]atrim=0:{dur},asetpts=PTS-STARTPTS[{label_fill}]"
        )
        concat_inputs.append(f"[{label_fill}]")
        prev_end = end
        seg_idx += 1

    # Post-last-segment tail
    label_tail = "tail"
    filter_parts.append(
        f"[0:a]atrim={prev_end},asetpts=PTS-STARTPTS[{label_tail}]"
    )
    concat_inputs.append(f"[{label_tail}]")

    n_segs = len(concat_inputs)
    filter_parts.append(
        f"{''.join(concat_inputs)}concat=n={n_segs}:v=0:a=1[out]"
    )
    filter_str = ";".join(filter_parts)

    tmp = mc.with_name(mc.stem + ".tmp.mp3")
    result = subprocess.run(
        inputs + ["-filter_complex", filter_str, "-map", "[out]",
                  "-b:a", f"{BITRATE_KBPS()}k", "-f", "mp3", "-y", str(tmp)],
        capture_output=True,
    )
    if result.returncode == 0:
        tmp.rename(mc)
        log(f"Wrote {mc.name} with {len(segments)} segment(s) substituted")
    else:
        log(f"ffmpeg mod failed for {chunk.name}: {result.stderr[-200:].decode(errors='replace')}")
        if tmp.exists():
            tmp.unlink()


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

def _js_routes() -> str:
    """JSON map of slug → IANA name, for client-side timezone detection."""
    return _json.dumps({label.lower(): iana for label, iana in tz_routes().items()})


def html_autodetect() -> str:
    """Tiny redirect page: detects browser timezone, bounces to the best slug."""
    routes_json = _js_routes()
    default = default_tz().lower()
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Redirecting...</title>
<script>
(function(){{
  var routes = {routes_json};
  var userTZ = (Intl && Intl.DateTimeFormat) ? Intl.DateTimeFormat().resolvedOptions().timeZone : '';

  // 1. Exact IANA match
  for (var slug in routes) {{
    if (routes[slug] === userTZ) {{ location.replace('/' + slug); return; }}
  }}

  // 2. Closest UTC-offset match
  function tzOffsetMin(iana) {{
    try {{
      var s = new Intl.DateTimeFormat('en', {{timeZone: iana, timeZoneName: 'shortOffset'}})
                      .formatToParts(new Date())
                      .find(function(p){{ return p.type === 'timeZoneName'; }});
      var m = s && s.value.match(/GMT([+-])(\\d+)(?::(\\d+))?/);
      if (!m) return 0;
      return (m[1] === '-' ? 1 : -1) * (parseInt(m[2]) * 60 + parseInt(m[3] || 0));
    }} catch(e) {{ return 0; }}
  }}

  var userOff = new Date().getTimezoneOffset();
  var best = '{default}', bestDiff = Infinity;
  for (var slug in routes) {{
    var diff = Math.abs(userOff - tzOffsetMin(routes[slug]));
    if (diff < bestDiff) {{ bestDiff = diff; best = slug; }}
  }}
  location.replace('/' + best);
}})();
</script>
<noscript><meta http-equiv="refresh" content="0;url=/{default}"></noscript>
</head><body></body></html>"""


def _tz_suggest_js(current_slug: str) -> str:
    """JS snippet: if browser timezone better matches a different slug, show a banner."""
    routes_json = _js_routes()
    accent = ACCENT()
    return f"""<script>
(function(){{
  var routes = {routes_json};
  var current = '{current_slug.lower()}';
  var userTZ = (Intl && Intl.DateTimeFormat) ? Intl.DateTimeFormat().resolvedOptions().timeZone : '';
  if (!userTZ) return;

  function tzOffsetMin(iana) {{
    try {{
      var s = new Intl.DateTimeFormat('en', {{timeZone: iana, timeZoneName: 'shortOffset'}})
                      .formatToParts(new Date())
                      .find(function(p){{ return p.type === 'timeZoneName'; }});
      var m = s && s.value.match(/GMT([+-])(\\d+)(?::(\\d+))?/);
      if (!m) return 0;
      return (m[1] === '-' ? 1 : -1) * (parseInt(m[2]) * 60 + parseInt(m[3] || 0));
    }} catch(e) {{ return 0; }}
  }}

  var best = null;
  // Exact match first
  for (var slug in routes) {{ if (routes[slug] === userTZ) {{ best = slug; break; }} }}
  // Offset match fallback
  if (!best) {{
    var userOff = new Date().getTimezoneOffset(), bestDiff = Infinity;
    for (var slug in routes) {{
      var diff = Math.abs(userOff - tzOffsetMin(routes[slug]));
      if (diff < bestDiff) {{ bestDiff = diff; best = slug; }}
    }}
  }}

  if (best && best !== current) {{
    var b = document.createElement('div');
    b.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);' +
      'background:#222;border:1px solid #333;border-radius:12px;padding:12px 18px;' +
      'font-size:13px;color:#ccc;white-space:nowrap;box-shadow:0 4px 20px rgba(0,0,0,.5);' +
      'display:flex;align-items:center;gap:12px;z-index:999;';
    b.innerHTML = 'Your timezone looks like <strong style="color:#fff">' + best.toUpperCase() + '</strong> &nbsp;' +
      '<a href="/' + best + '" style="background:{accent};color:#fff;border-radius:7px;' +
      'padding:5px 12px;text-decoration:none;font-weight:600;font-size:12px;">Switch</a>' +
      '<span onclick="this.parentNode.remove()" style="cursor:pointer;color:#555;font-size:16px;line-height:1;">&times;</span>';
    document.body.appendChild(b);
  }}
}})();
</script>"""


def _clock_js(source_iana: str, local_iana: str, target_utc_ms: int) -> str:
    """
    Ticks the unified clock display every second.
    The playback moment is the same in both timezones — show one time, two labels.
    source ticks from target_utc_ms (playback position, not current time).
    """
    now_aware  = datetime.datetime.now(datetime.timezone.utc)
    src_abbr   = now_aware.astimezone(ZoneInfo(source_iana)).strftime("%Z")
    local_abbr = now_aware.astimezone(ZoneInfo(local_iana)).strftime("%Z") if local_iana != source_iana else ""
    tz_label   = f"{src_abbr} · {local_abbr}" if local_abbr else src_abbr
    return f"""<script>
(function(){{
  var timeEl = document.getElementById('shared-time');
  if (!timeEl) return;

  var fmt = new Intl.DateTimeFormat('en-US', {{
    hour: 'numeric', minute: '2-digit', second: '2-digit',
    hour12: true, timeZone: '{source_iana}'
  }});

  var targetMs = {target_utc_ms};
  var loadedAt = Date.now();

  function tick() {{
    timeEl.textContent = fmt.format(new Date(targetMs + (Date.now() - loadedAt)));
  }}
  tick();
  setInterval(tick, 1000);
}})();
</script>"""


def _player_js() -> str:
    """Mute toggle + skip-news toggle + tab switching."""
    return """<script>
(function(){
  var audio    = document.getElementById('player') || document.querySelector('audio');
  if (!audio) return;

  var muteBtn  = document.getElementById('mute-btn');
  var muteIcon = document.getElementById('mute-icon');
  var muteLbl  = document.getElementById('mute-label');
  var skipBtn  = document.getElementById('skip-btn');
  var skipIcon = document.getElementById('skip-icon');
  var skipLbl  = document.getElementById('skip-label');

  // ── Mute ────────────────────────────────────────────────────────────────
  var wantsMuted = localStorage.getItem('muted') === '1';  // default: unmuted

  function setMuted(m) {
    audio.muted = m;
    muteIcon.textContent = m ? '🔇' : '🔊';
    muteLbl.textContent  = m ? 'Unmute' : 'Mute';
    muteBtn.classList.toggle('active', !m);
  }

  // ── Skip-news toggle ─────────────────────────────────────────────────────
  function streamSrc(slug, skip) {
    var base = '/stream/' + (slug === 'live' ? 'live' : slug);
    return skip ? base + '?skip=1' : base;
  }
  function currentSlug() {
    return window.location.pathname.replace(/^\\//, '') || 'et';
  }
  var skipOn = localStorage.getItem('skip-news') === '1';
  function applySkip(on, reconnect) {
    skipOn = on;
    localStorage.setItem('skip-news', on ? '1' : '0');
    if (skipBtn) {
      skipBtn.classList.toggle('skip-on', on);
      skipIcon.textContent = on ? '✅' : '📰';
      skipLbl.textContent  = on ? 'News skipped' : 'Skip news';
    }
    if (reconnect) {
      var wasMuted = audio.muted;
      audio.src = streamSrc(currentSlug(), on);
      audio.muted = wasMuted;
      audio.play().catch(function(){});
    }
  }
  applySkip(skipOn, false);

  // Set correct src (with ?skip if needed) before first play
  setMuted(wantsMuted);
  audio.src = streamSrc(currentSlug(), skipOn);
  audio.play().catch(function() {
    // Autoplay blocked — show tap-to-start
    muteIcon.textContent = '▶';
    muteLbl.textContent  = 'Tap to start';
    muteBtn.classList.remove('active');
  });

  if (muteBtn) muteBtn.addEventListener('click', function() {
    var blocked = muteLbl.textContent === 'Tap to start';
    if (blocked) {
      // First click after autoplay blocked — honor saved preference
      wantsMuted = localStorage.getItem('muted') === '1';
    } else {
      wantsMuted = !wantsMuted;
      localStorage.setItem('muted', wantsMuted ? '1' : '0');
    }
    setMuted(wantsMuted);
    audio.play().catch(function(){});
  });
  if (skipBtn) skipBtn.addEventListener('click', function() {
    applySkip(!skipOn, true);
  });

  // ── Tab switching ────────────────────────────────────────────────────────
  document.querySelectorAll('.tz-tabs a').forEach(function(a) {
    a.addEventListener('click', function(e) {
      e.preventDefault();
      var href = this.getAttribute('href');
      var slug = href.replace(/^\\//, '');
      var wasMuted = audio.muted;
      audio.src = streamSrc(slug, skipOn);
      audio.muted = wasMuted;
      audio.play().catch(function(){});
      document.querySelectorAll('.tz-tabs a').forEach(function(t) { t.classList.remove('active'); });
      this.classList.add('active');
      history.pushState({}, '', href);
    });
  });
})();
</script>"""


def _nowplaying_js(tz: str) -> str:
    return f"""<script>
(function(){{
  var box     = document.getElementById('nowplaying');
  var title   = document.getElementById('np-title');
  var artist  = document.getElementById('np-artist');
  var cover   = document.getElementById('np-cover');
  var spinner = document.getElementById('np-spinner');
  var slug    = '{tz}';

  function poll() {{
    fetch('/nowplaying/' + slug)
      .then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        if (d.status === 'ok' && d.title) {{
          title.textContent  = d.title;
          artist.textContent = d.artist || '';
          if (d.cover) {{ cover.src = d.cover; cover.style.display = 'block'; }}
          else          {{ cover.style.display = 'none'; }}
          spinner.style.display = 'none';
          box.style.display = 'flex';
        }} else {{
          box.style.display = 'none';
        }}
      }})
      .catch(function() {{}});
    setTimeout(poll, 30000);
  }}
  poll();
}})();
</script>"""


def _tz_tabs(active_tz: str) -> str:
    tabs = ""
    for label, iana in tz_routes().items():
        slug = label.lower()
        active = ' class="active"' if slug == active_tz else ""
        tabs += f'<a href="/{slug}"{active}>{label}</a>\n            '
    live_active = ' class="active live"' if active_tz == "live" else ' class="live"'
    tabs += f'<a href="/live"{live_active}>&#9679; Live</a>\n            '
    return tabs


def html_player(tz: str, iana: str, delay: float,
                now: datetime.datetime, target_dt: datetime.datetime) -> str:
    s = station()
    name       = s.get("name", "Radio")
    name_local = s.get("name_local", "")
    desc       = s.get("description", "")
    accent     = ACCENT()
    source_time  = fmt_source(target_dt)
    now_aware    = datetime.datetime.now(datetime.timezone.utc)
    src_abbr     = now_aware.astimezone(ZoneInfo(SOURCE_TZ())).strftime("%Z")
    local_abbr   = now_aware.astimezone(ZoneInfo(iana)).strftime("%Z")
    tz_pair      = f"{src_abbr} · {local_abbr}" if src_abbr != local_abbr else src_abbr
    tz_label     = tz.upper()
    tabs         = _tz_tabs(tz)
    display_name = name_local if name_local else name
    target_ms    = int((target_dt - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)

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
      background: #111; border-radius: 12px; padding: 16px 18px;
      margin-bottom: 20px; text-align: center;
    }}
    .times .clock {{ font-size: 28px; font-weight: 700; color: var(--accent); letter-spacing: -.5px; font-variant-numeric: tabular-nums; }}
    .times .tz-pair {{ font-size: 12px; color: #555; margin-top: 5px; letter-spacing: .05em; }}
    .delay-note {{
      font-size: 12px; color: #444; margin-top: 10px;
      padding-top: 10px; border-top: 1px solid #1e1e1e;
    }}
    .dot {{
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      background: var(--accent); margin-right: 5px;
      animation: pulse 1.6s ease-in-out infinite;
    }}
    @keyframes pulse {{ 0%,100%{{ opacity:1 }} 50%{{ opacity:.25 }} }}
    .nowplaying {{
      display: none; align-items: center; gap: 12px;
      background: #111; border-radius: 12px; padding: 12px 14px;
      margin-bottom: 16px; min-height: 56px;
    }}
    .np-cover {{
      width: 44px; height: 44px; border-radius: 8px; object-fit: cover; flex-shrink: 0;
      background: #222; display: none;
    }}
    .np-text {{ flex: 1; overflow: hidden; }}
    .np-title {{ font-size: 13px; font-weight: 600; color: #f0f0f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .np-artist {{ font-size: 11px; color: #666; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .np-spinner {{ width: 18px; height: 18px; border: 2px solid #333; border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; flex-shrink: 0; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .controls {{ display: flex; gap: 8px; margin-bottom: 16px; }}
    .ctrl-btn {{
      flex: 1; display: flex; align-items: center; justify-content: center;
      gap: 8px; background: #222; border: none; border-radius: 10px;
      color: #666; padding: 13px 0; font-size: 14px; font-weight: 600;
      cursor: pointer; transition: background .15s, color .15s; font-family: inherit;
    }}
    .ctrl-btn:hover {{ background: #2a2a2a; color: #999; }}
    .ctrl-btn.active {{ background: var(--accent); color: #fff; }}
    #skip-btn.skip-on {{ background: #1a3a1a; color: #4caf50; }}
    #skip-btn.skip-on:hover {{ background: #223a22; }}
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
    .tz-tabs a.active {{ background: var(--accent); color: #fff; }}
    .tz-tabs a:hover:not(.active) {{ background: #2a2a2a; color: #ccc; }}
    .tz-tabs a.live {{ color: #cc2200; }}
    .tz-tabs a.live.active, .tz-tabs a.live:hover {{ background: #cc2200; color: #fff; }}
    .car-trigger {{
      width: 100%; margin-top: 14px; padding: 11px 0;
      background: none; border: 1px solid #2a2a2a; border-radius: 10px;
      color: #555; font-size: 13px; font-weight: 600; cursor: pointer;
      transition: border-color .15s, color .15s; font-family: inherit;
    }}
    .car-trigger:hover {{ border-color: #444; color: #888; }}
    .car-modal {{
      display: none; position: fixed; inset: 0; z-index: 100;
      align-items: flex-end; justify-content: center;
      background: rgba(0,0,0,.6); backdrop-filter: blur(4px);
    }}
    .car-modal.open {{ display: flex; }}
    .car-sheet {{
      background: #1a1a1a; border-radius: 20px 20px 0 0;
      width: min(460px, 100vw); padding: 28px 28px 36px;
      border-top: 1px solid #2a2a2a;
    }}
    .car-sheet h3 {{ font-size: 16px; font-weight: 700; margin-bottom: 18px; color: #f0f0f0; }}
    .car-steps {{ list-style: none; display: flex; flex-direction: column; gap: 12px; }}
    .car-steps li {{ display: flex; gap: 12px; align-items: flex-start; font-size: 13px; color: #aaa; line-height: 1.5; }}
    .car-steps .num {{
      background: #2a2a2a; color: #fff; border-radius: 50%;
      width: 22px; height: 22px; display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 700; flex-shrink: 0; margin-top: 1px;
    }}
    .car-steps strong {{ color: #f0f0f0; }}
    .car-divider {{ border: none; border-top: 1px solid #2a2a2a; margin: 18px 0; }}
    .car-dl {{
      display: flex; align-items: center; justify-content: space-between;
      background: #222; border-radius: 10px; padding: 12px 16px;
    }}
    .car-dl-label {{ font-size: 12px; color: #777; }}
    .car-dl-btn {{
      background: var(--accent); color: #fff; border-radius: 7px;
      padding: 7px 14px; text-decoration: none; font-size: 12px; font-weight: 700;
    }}
    .car-close {{
      width: 100%; margin-top: 14px; padding: 12px 0;
      background: #222; border: none; border-radius: 10px;
      color: #888; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit;
    }}
    .car-close:hover {{ background: #2a2a2a; color: #fff; }}
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
      <div class="clock" id="shared-time">{source_time}</div>
      <div class="tz-pair">{tz_pair}</div>
      <div class="delay-note">
        <span class="dot"></span>Playing {delay:.0f}h behind live
      </div>
    </div>

    <audio autoplay src="/stream/{tz}" id="player"></audio>

    <div class="nowplaying" id="nowplaying">
      <img class="np-cover" id="np-cover" src="" alt="">
      <div class="np-text">
        <div class="np-title" id="np-title">Identifying song…</div>
        <div class="np-artist" id="np-artist"></div>
      </div>
      <div class="np-spinner" id="np-spinner"></div>
    </div>

    <div class="controls">
      <button class="ctrl-btn" id="mute-btn">
        <span id="mute-icon">🔇</span>
        <span id="mute-label">Tap to listen</span>
      </button>
      <button class="ctrl-btn" id="skip-btn" title="Replace news breaks with ambient music">
        <span id="skip-icon">📰</span>
        <span id="skip-label">Skip news</span>
      </button>
    </div>

    <div class="tz-tabs">
      {tabs}
    </div>

    <button class="car-trigger" onclick="document.getElementById('car-modal').classList.add('open')">
      &#128664; How to play in your car
    </button>
  </div>

  <div class="car-modal" id="car-modal" onclick="if(event.target===this)this.classList.remove('open')">
    <div class="car-sheet">
      <h3>&#128664; Play in your car</h3>
      <ol class="car-steps">
        <li>
          <span class="num">1</span>
          <span>Install <strong>VLC</strong> (free) from the App Store or Google Play.</span>
        </li>
        <li>
          <span class="num">2</span>
          <span>Tap <strong>Copy stream URL</strong> below, then open VLC &rarr; <strong>Network</strong> tab &rarr; tap the URL bar and paste.</span>
        </li>
        <li>
          <span class="num">3</span>
          <span>VLC will start playing. Connect your phone to your car and open VLC from <strong>CarPlay</strong> or <strong>Android Auto</strong>.</span>
        </li>
      </ol>
      <hr class="car-divider">
      <div class="car-dl">
        <span class="car-dl-label" id="vlc-url-label" style="font-size:11px;word-break:break-all;color:#555;flex:1;margin-right:12px;"></span>
        <button class="car-dl-btn" id="copy-url-btn" onclick="copyStreamUrl()">Copy URL</button>
      </div>
      <script>
        (function(){{
          var url = location.protocol + '//' + location.host + '/stream/{tz}';
          document.getElementById('vlc-url-label').textContent = url;
        }})();
        function copyStreamUrl() {{
          var url = location.protocol + '//' + location.host + '/stream/{tz}';
          navigator.clipboard.writeText(url).then(function() {{
            var btn = document.getElementById('copy-url-btn');
            btn.textContent = 'Copied ✓';
            setTimeout(function() {{ btn.textContent = 'Copy URL'; }}, 2000);
          }});
        }}
      </script>
      <button class="car-close" onclick="document.getElementById('car-modal').classList.remove('open')">Close</button>
    </div>
  </div>
{_tz_suggest_js(tz)}
{_clock_js(SOURCE_TZ(), iana, target_ms)}
{_player_js()}
{_nowplaying_js(tz)}
</body>
</html>"""


def html_live() -> str:
    s = station()
    name        = s.get("name", "Radio")
    name_local  = s.get("name_local", "")
    desc        = s.get("description", "")
    accent      = ACCENT()
    tabs        = _tz_tabs("live")
    display_name = name_local if name_local else name
    now         = utcnow()
    source_time = fmt_source(now)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{name} &mdash; Live</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{ --accent: {accent}; }}
    body {{
      background: #0d0d0d; color: #f0f0f0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      min-height: 100dvh; display: flex; align-items: center; justify-content: center;
    }}
    .card {{
      background: #181818; border-radius: 20px; padding: 40px 36px 32px;
      width: min(420px, 94vw); box-shadow: 0 20px 60px rgba(0,0,0,.6);
    }}
    .header {{ display: flex; align-items: center; gap: 16px; margin-bottom: 28px; }}
    .logo-wrap {{
      width: 64px; height: 64px;
      background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 60%, black), var(--accent));
      border-radius: 16px; display: flex; align-items: center; justify-content: center;
      font-size: 32px; flex-shrink: 0;
    }}
    .name {{ font-size: 26px; font-weight: 700; letter-spacing: -.5px; }}
    .sub  {{ font-size: 13px; color: #777; margin-top: 2px; }}
    .live-banner {{
      background: #1a0a0a; border: 1px solid #3a1010; border-radius: 12px;
      padding: 14px 18px; margin-bottom: 24px;
      display: flex; align-items: center; gap: 12px;
    }}
    .live-badge {{
      background: #cc2200; color: #fff; font-size: 11px; font-weight: 700;
      letter-spacing: .08em; padding: 3px 8px; border-radius: 5px; flex-shrink: 0;
    }}
    .live-time {{ font-size: 17px; font-weight: 600; }}
    .live-sub  {{ font-size: 12px; color: #555; margin-top: 2px; }}
    .controls {{ margin-bottom: 20px; }}
    .mute-btn {{
      width: 100%; display: flex; align-items: center; justify-content: center;
      gap: 10px; background: #222; border: none; border-radius: 10px;
      color: #666; padding: 13px 0; font-size: 15px; font-weight: 600;
      cursor: pointer; transition: background .15s, color .15s; font-family: inherit;
    }}
    .mute-btn:hover {{ background: #2a2a2a; color: #999; }}
    .mute-btn.active {{ background: #cc2200; color: #fff; }}
    .tz-tabs {{ display: flex; gap: 8px; }}
    .tz-tabs a {{
      flex: 1; text-align: center; padding: 10px 0; border-radius: 10px;
      background: #222; color: #888; text-decoration: none;
      font-size: 13px; font-weight: 600; letter-spacing: .03em;
      transition: background .15s, color .15s;
    }}
    .tz-tabs a.active {{ background: var(--accent); color: #fff; }}
    .tz-tabs a:hover:not(.active) {{ background: #2a2a2a; color: #ccc; }}
    .tz-tabs a.live {{ color: #cc2200; }}
    .tz-tabs a.live.active, .tz-tabs a.live:hover {{ background: #cc2200; color: #fff; }}
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

    <div class="live-banner">
      <span class="live-badge">LIVE</span>
      <div>
        <div class="live-time" id="source-time">{source_time}</div>
        <div class="live-sub">Broadcasting now</div>
      </div>
    </div>

    <audio autoplay src="/stream/live"></audio>

    <div class="controls">
      <button class="mute-btn" id="mute-btn">
        <span id="mute-icon">🔇</span>
        <span id="mute-label">Tap to listen</span>
      </button>
    </div>

    <div class="tz-tabs">
      {tabs}
    </div>
  </div>
{_clock_js(SOURCE_TZ(), SOURCE_TZ(), int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))}
{_player_js()}
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

        if path.startswith("/nowplaying/"):
            slug = path[len("/nowplaying/"):]
            iana = tz_routes().get(slug.upper())
            if iana:
                self._serve_nowplaying(slug, iana)
            else:
                self._text(404, "Unknown timezone.\n")
            return

        if path.startswith("/stream/"):
            slug = path[len("/stream/"):]
            if slug == "live":
                self._serve_live_stream()
                return
            if slug.endswith(".m3u"):
                slug = slug[:-4]
                iana = tz_routes().get(slug.upper())
                if iana:
                    self._serve_m3u(slug)
                else:
                    self._text(404, "Unknown timezone.\n")
                return
            if slug == "live.m3u":
                self._serve_m3u("live")
                return
            iana = tz_routes().get(slug.upper())
            if iana:
                self._serve_stream(slug, iana)
            else:
                self._text(404, "Unknown stream.\n")
            return

        if path == "/live":
            self._html(200, html_live())
            return

        if path == "/":
            self._html(200, html_autodetect())
            return

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
            chunks = valid_chunks()
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
        target_dt = now - datetime.timedelta(hours=delay) + datetime.timedelta(seconds=STREAM_LAG_S())
        target_chunk = chunk_path(target_dt)

        if not target_chunk.exists():
            self._text(503, "Stream not ready yet.\n")
            return

        seek_sec = int((target_dt - chunk_floor(target_dt)).total_seconds())
        seek_bytes = seek_sec * BYTES_PER_SEC()
        skip_requested = "skip=1" in self.path
        mc = mod_chunk(target_chunk)
        if (skip_requested or SKIP_NEWS()) and mc.exists():
            target_chunk = mc

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
                next_chunk = chunk_path(dt + datetime.timedelta(hours=1))
                if skip_requested or SKIP_NEWS():
                    next_mc = mod_chunk(next_chunk)
                    current = next_mc if next_mc.exists() else next_chunk
                else:
                    current = next_chunk
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_live_stream(self):
        try:
            req = urllib.request.urlopen(STREAM_URL(), timeout=10)
        except Exception as e:
            self._text(502, f"Could not connect to source stream: {e}\n")
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("icy-name", f"{station().get('name', 'Radio')} (Live)")
        self.send_header("icy-br", str(BITRATE_KBPS()))
        self.end_headers()
        try:
            while True:
                data = req.read(8192)
                if not data:
                    break
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            req.close()

    def _serve_m3u(self, slug: str):
        name = station().get("name", "Radio")
        host = self.headers.get("Host", f"localhost:{HTTP_PORT()}")
        scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        base = f"{scheme}://{host}"
        if slug == "live":
            title = f"{name} — Live"
            stream = f"{base}/stream/live"
        else:
            delay = delay_hours_for_tz(tz_routes()[slug.upper()])
            title = f"{name} ({slug.upper()} -{delay:.0f}h)"
            stream = f"{base}/stream/{slug}"
        body = f"#EXTM3U\n#EXTINF:-1,{title}\n{stream}\n"
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "audio/x-mpegurl")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _serve_nowplaying(self, tz: str, iana: str):
        delay = delay_hours_for_tz(iana)
        now = utcnow()
        target_dt = now - datetime.timedelta(hours=delay) + datetime.timedelta(seconds=STREAM_LAG_S())
        target_chunk = chunk_path(target_dt)
        if not target_chunk.exists():
            self._serve_json(200, {"status": "not_ready"})
            return
        seek_sec = int((target_dt - chunk_floor(target_dt)).total_seconds())
        result = recognize_track(target_chunk, seek_sec)
        self._serve_json(200, result)

    def _serve_json(self, code: int, data: dict):
        b = _json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

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


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

def run_http_server():
    server = _ThreadingHTTPServer(("127.0.0.1", HTTP_PORT()), TimeshiftHandler)
    log(f"HTTP server on 127.0.0.1:{HTTP_PORT()}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Recording loop
# ---------------------------------------------------------------------------

def recording_loop():
    global _active_ffmpeg
    CACHE_DIR().mkdir(parents=True, exist_ok=True)
    log(f"Recording {STREAM_URL()}")
    clean_old_chunks()

    prev_out: Optional[Path] = None

    while True:
        now = utcnow()
        next_hour = chunk_floor(now) + datetime.timedelta(hours=1)
        duration = max(1, int((next_hour - now).total_seconds()))
        out = chunk_path(now)

        # If we crossed a boundary (e.g. due to CDN crash retries), scan the
        # previous chunk now — it won't get another chance.
        if prev_out and prev_out != out and prev_out.exists() and FILL_TRACK.exists():
            if not mod_chunk(prev_out).exists():
                threading.Thread(
                    target=detect_and_save_news_skip,
                    args=(prev_out,), daemon=True,
                ).start()
        prev_out = out

        # Drop sub-1MB stubs from previous interrupted starts
        if out.exists() and out.stat().st_size < MIN_CHUNK_BYTES:
            out.unlink()

        action = "Resuming" if out.exists() else "Recording"
        log(f"{action} {out.name} for {duration}s")

        # Append mode: creates the file if new, appends if resuming after a gap
        out_fh = open(out, "ab")
        try:
            _active_ffmpeg = subprocess.Popen(
                ["ffmpeg", "-i", STREAM_URL(), "-t", str(duration),
                 "-c", "copy", "-f", "mp3", "pipe:1"],
                stdout=out_fh, stderr=subprocess.DEVNULL,
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
                    if FILL_TRACK.exists():
                        threading.Thread(
                            target=detect_and_save_news_skip,
                            args=(out,), daemon=True,
                        ).start()
                    break
                time.sleep(1)
        finally:
            out_fh.close()

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


def _scan_missing_mod_files():
    """On startup, kick off detection for completed chunks that have no mod file yet."""
    if not FILL_TRACK.exists():
        return
    current = chunk_path(utcnow())
    for chunk in valid_chunks():
        if chunk == current:
            continue  # still recording, skip
        if not mod_chunk(chunk).exists():
            log(f"Startup scan: queuing {chunk.name} for news detection")
            threading.Thread(
                target=detect_and_save_news_skip, args=(chunk,), daemon=True,
            ).start()


def daemon_main():
    signal.signal(signal.SIGTERM, _sigterm)
    CACHE_DIR().mkdir(parents=True, exist_ok=True)
    log(f"Daemon started (PID {os.getpid()})")
    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=_scan_missing_mod_files, daemon=True).start()
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

    chunks = valid_chunks()
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
