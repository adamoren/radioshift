#!/usr/bin/env python3
"""Backfill .mod.mp3 files for all existing chunks that don't have one."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Bootstrap config so radioshift module can load
import importlib.util
spec = importlib.util.spec_from_file_location("radioshift", os.path.join(os.path.dirname(__file__), "radioshift.py"))
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)

config_path = rs.Path(os.path.join(os.path.dirname(__file__), "config.toml"))
rs.CFG = rs.load_config(config_path)

from pathlib import Path

fill = rs.FILL_TRACK
if not fill.exists():
    print(f"ERROR: fill track not found at {fill}")
    sys.exit(1)

chunks = [c for c in rs.valid_chunks() if not rs.mod_chunk(c).exists()]
print(f"Scanning {len(chunks)} chunks for news segments...")

for c in chunks:
    print(f"  {c.name}...", end=" ", flush=True)
    rs.detect_and_save_news_skip(c)
    mc = rs.mod_chunk(c)
    if mc.exists():
        print(f"OK ({mc.stat().st_size // 1024}KB)")
    else:
        print("no segments found")

print("Done.")
