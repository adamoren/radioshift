#!/usr/bin/env python3.11
"""
Shazam recognition helper — called as a subprocess by radioshift.
Reads MP3 bytes from stdin, prints JSON result to stdout.
"""
import sys
import json
import asyncio

async def run():
    from shazamio import Shazam
    data = sys.stdin.buffer.read()
    if not data:
        print(json.dumps({"status": "error", "reason": "no audio"}))
        return
    try:
        result = await Shazam().recognize(data)
        track = result.get("track") or {}
        if track:
            print(json.dumps({
                "status": "ok",
                "title":  track.get("title", ""),
                "artist": track.get("subtitle", ""),
                "cover":  (track.get("images") or {}).get("coverart", ""),
            }))
        else:
            print(json.dumps({"status": "unknown"}))
    except Exception as e:
        print(json.dumps({"status": "error", "reason": str(e)}))

asyncio.run(run())
