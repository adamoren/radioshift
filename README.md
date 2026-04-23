# radioshift

Time-shifted internet radio for diaspora listeners.

Records a live radio stream and serves it with a delay so that listeners in different timezones hear the station at the **same local time of day** as listeners back home. A morning show that airs at 8 AM in Israel plays at 8 AM in New York, Chicago, Denver, and Los Angeles — each with the appropriate delay.

## How it works

- A background daemon records the live stream into hourly `.mp3` chunks
- A built-in HTTP server serves each chunk at the right seek offset for each timezone
- The delay per timezone is computed dynamically and is DST-aware
- Old chunks are deleted automatically; only enough history to serve all configured timezones is kept

## Requirements

- Python 3.11+
- `ffmpeg` (must be in `$PATH`)

## Quick start

```bash
cp config.example.toml config.toml
# Edit config.toml: set stream_url, source_timezone, timezones, accent_color
python radioshift.py start
python radioshift.py status
```

Then point a reverse proxy (Caddy, nginx) at `http://127.0.0.1:<port>`.

## Commands

```
python radioshift.py [--config config.toml] start   # start background daemon
python radioshift.py [--config config.toml] stop    # stop daemon
python radioshift.py [--config config.toml] status  # show buffer and stream status
```

`--config` defaults to `config.toml` in the same directory as the script.

## URL structure

| Path | Description |
|---|---|
| `/` | Redirects to the first configured timezone |
| `/<tz>` | Web player for that timezone (e.g. `/et`, `/pt`) |
| `/live` | Web player for the live stream |
| `/stream/<tz>` | Raw MP3 stream — use in any audio player or `ffplay` |
| `/stream/live` | Raw MP3 live stream |
| `/stream/<tz>.m3u` | M3U playlist — use with CarPlay / Android Auto apps |
| `/stream/live.m3u` | M3U playlist for the live stream |

## CarPlay and Android Auto

The web player doesn't run inside CarPlay or Android Auto, but the M3U playlist links make it easy to add the stream to a compatible radio app that does.

**iOS / CarPlay and Android / Android Auto**

[VLC](https://www.videolan.org/vlc/) is free and works on both platforms with CarPlay and Android Auto support.

1. Install VLC from the App Store or Google Play.
2. On the player page, tap **"How to play in your car"** and then **"Get stream link"** — open the downloaded file with VLC.
3. Connect your phone to your car. VLC will appear in CarPlay or Android Auto with the station ready to play.

Alternatively, add the stream URL directly in VLC's network stream dialog:
```
https://your-domain.com/stream/et.m3u
```
(replace `et` with your timezone slug)

The player page also has a **"Get stream link"** button that downloads the `.m3u` file directly — tap it on your phone, open it with Triode or VLC, and save it as a station.

## Configuration

See [`config.example.toml`](config.example.toml) for a fully annotated example with multiple station templates (BBC, Antena 1, and others).

Key fields:

| Field | Description |
|---|---|
| `station.stream_url` | Direct URL to the source MP3/AAC stream |
| `station.source_timezone` | IANA name of the station's timezone (e.g. `Asia/Jerusalem`) |
| `station.bitrate_kbps` | Stream bitrate — used for accurate in-chunk seeking |
| `station.accent_color` | UI accent color (any CSS color) |
| `server.port` | Local HTTP port |
| `server.cache_dir` | Where to store recorded chunks |
| `server.max_age_hours` | How many hours of audio to retain (must be ≥ max delay + 1) |
| `timezones` | Map of label → IANA timezone for each listener timezone |

## Running multiple stations

Each instance reads one config file and runs on its own port. Use your reverse proxy to route subdomains:

```bash
python radioshift.py --config galgalatz.toml start   # port 8765
python radioshift.py --config bbc.toml start          # port 8766
```

## Caddy example

```
http://radio.example.com {
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1
    }
}
```

## Buffering time

After starting, each timezone stream becomes available after its delay has elapsed:

- The stream for a timezone with a 7h delay is ready ~7 hours after first start
- The buffering page auto-refreshes every minute and shows an ETA
- No data is lost during this time — recording starts immediately

## Self-hosting

You need a Linux server with Python 3.11+ and ffmpeg. A $6/month VPS is plenty for a family-sized audience.

- **Hetzner** (recommended — fast, cheap, European) → [hetzner.com](https://hetzner.com/cloud)
- **DigitalOcean** → [get $200 credit for 60 days](https://m.do.co/c/ca7aa9ddeb9a)

## Support

radioshift is free and open source. If it's useful to you:

- ⭐ Star the repo
- ☕ [Buy me a coffee](https://buymeacoffee.com/adamoren)
- [GitHub Sponsors](https://github.com/sponsors/adamoren)

## License

MIT

News-break fill music: "Local Forecast - Elevator" by Kevin MacLeod (incompetech.com), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
