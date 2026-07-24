#!/usr/bin/env python3
"""One-shot: seed Yamtrack with movies and series from Remux.

Reads Remux SQLite directly, posts each item to Yamtrack's
Jellyfin webhook. Skips episodes (they sync naturally via forward sync).

Usage:
  REMUX_DB_PATH=/path/to/db.sqlite \\
  REMUX_URL=http://localhost:8000 \\
  REMUX_API_KEY=*** \\
  YAMTRACK_URL=https://track.ashipaek0.com.ng \\
  YAMTRACK_TOKEN=*** \\
  python3 seed_yamtrack.py [--dry-run]
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REMUX_DB_PATH  = os.environ.get("REMUX_DB_PATH", "/remux-db/db.sqlite")
REMUX_URL      = os.environ.get("REMUX_URL", "http://localhost:8000").rstrip("/")
YAMTRACK_URL   = os.environ["YAMTRACK_URL"].rstrip("/")
YAMTRACK_TOKEN = os.environ["YAMTRACK_TOKEN"]
DRY_RUN        = "--dry-run" in sys.argv


def post_webhook(payload: dict) -> bool:
    url = f"{YAMTRACK_URL}/webhook/jellyfin/{YAMTRACK_TOKEN}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        print(f"  X HTTP {e.code}: {body}")
        return False


def build_movie_payload(row: sqlite3.Row) -> dict:
    ext = json.loads(row["external_ids"])
    pid = {}
    if "imdb" in ext:
        pid["Imdb"] = ext["imdb"]
    if "tmdb" in ext:
        pid["Tmdb"] = str(ext["tmdb"])
    year = row["released_at"][:4] if row["released_at"] else None
    return {
        "Event": "Stop",
        "Item": {
            "Name": row["title"],
            "Type": "Movie",
            "ProviderIds": pid,
            "ProductionYear": year,
            "UserData": {"Played": False, "PlayCount": 0, "PlaybackPosition": 0},
        },
    }


def build_series_payload(row: sqlite3.Row) -> dict:
    ext = json.loads(row["external_ids"])
    pid = {}
    if "imdb" in ext:
        pid["Imdb"] = ext["imdb"]
    if "tmdb" in ext:
        pid["Tmdb"] = str(ext["tmdb"])
    year = row["released_at"][:4] if row["released_at"] else None
    return {
        "Event": "Stop",
        "Item": {
            "Name": row["title"],
            "Type": "Series",
            "ProviderIds": pid,
            "ProductionYear": year,
            "UserData": {"Played": False, "PlayCount": 0, "PlaybackPosition": 0},
        },
    }


def seed(kind: str, builder) -> tuple[int, int]:
    conn = sqlite3.connect(f"file:{REMUX_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT title, external_ids, released_at FROM media "
        "WHERE kind = ? AND (external_ids LIKE '%tmdb%' OR external_ids LIKE '%imdb%') "
        "ORDER BY title",
        (kind,),
    ).fetchall()
    conn.close()

    sent = errors = 0
    for r in rows:
        if DRY_RUN:
            print(f"  [dry] {kind}: {r['title']}")
            sent += 1
        else:
            if post_webhook(builder(r)):
                sent += 1
            else:
                errors += 1
            time.sleep(0.05)
        if (sent + errors) % 500 == 0:
            print(f"  ... {sent + errors}/{len(rows)}  sent={sent} errors={errors}")

    return sent, errors


# ── Main ──────────────────────────────────────────────────────────────────

if not Path(REMUX_DB_PATH).exists():
    print(f"ERROR: Remux DB not found at {REMUX_DB_PATH}")
    sys.exit(1)

print(f"Remux DB: {REMUX_DB_PATH}")
print(f"Yamtrack: {YAMTRACK_URL}")
if DRY_RUN:
    print("(dry run — nothing posted)")

for kind, builder in [("movie", build_movie_payload), ("series", build_series_payload)]:
    print(f"\n--- {kind}s ---")
    sent, errors = seed(kind, builder)
    print(f"  {kind}s: {sent} sent, {errors} errors")

print("\nDone.")
if DRY_RUN:
    print("(dry run — nothing was actually posted)")
