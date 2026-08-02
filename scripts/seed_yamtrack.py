#!/usr/bin/env python3
"""One-shot: seed Yamtrack with movies and series from Remux.

Reads Remux SQLite directly, posts each item to Yamtrack's
Jellyfin webhook. Skips episodes (they sync naturally via forward sync).
Uses ThreadPoolExecutor for parallel POSTs.

Usage:
  REMUX_DB_PATH=/path/to/db.sqlite \\
  YAMTRACK_URL=https://track.ashipaek0.com.ng \\
  YAMTRACK_TOKEN=*** \\
  WORKERS=10 \\
  python3 -u seed_yamtrack.py [--dry-run]
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REMUX_DB_PATH  = os.environ.get("REMUX_DB_PATH", "/remux-db/db.sqlite")
REMUX_URL      = os.environ.get("REMUX_URL", "http://localhost:8000").rstrip("/")
YAMTRACK_URL   = os.environ["YAMTRACK_URL"].rstrip("/")
YAMTRACK_TOKEN = os.environ["YAMTRACK_TOKEN"]
WORKERS        = int(os.environ.get("WORKERS", "10"))
DRY_RUN        = "--dry-run" in sys.argv

WEBHOOK_URL = f"{YAMTRACK_URL}/webhook/jellyfin/{YAMTRACK_TOKEN}"


def post_webhook(payload: dict) -> tuple[bool, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        WEBHOOK_URL, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)[:200]


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


def seed(kind: str, build_payload) -> tuple[int, int]:
    conn = sqlite3.connect(f"file:{REMUX_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT title, external_ids, released_at FROM media "
        "WHERE kind = ? AND (external_ids LIKE '%tmdb%' OR external_ids LIKE '%imdb%') "
        "ORDER BY title",
        (kind,),
    ).fetchall()
    conn.close()

    total = len(rows)
    sent = errors = 0
    t0 = time.time()

    if DRY_RUN:
        return total, 0

    # Build all payloads first (CPU-bound, fast)
    payloads = [build_payload(r) for r in rows]

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(post_webhook, p): i for i, p in enumerate(payloads)}
        for future in as_completed(futures):
            ok, err = future.result()
            if ok:
                sent += 1
            else:
                errors += 1
                if errors <= 5:
                    print(f"  X [{kind}] {err}", flush=True)

            done = sent + errors
            if done % 500 == 0:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                print(f"  {kind}s: {done}/{total}  sent={sent} errors={errors}  "
                      f"{rate:.1f}/s  ETA {eta/60:.0f}m", flush=True)

    return sent, errors


# ── Main ──────────────────────────────────────────────────────────────────

if not Path(REMUX_DB_PATH).exists():
    print(f"ERROR: Remux DB not found at {REMUX_DB_PATH}")
    sys.exit(1)

print(f"Remux DB: {REMUX_DB_PATH}", flush=True)
print(f"Yamtrack: {YAMTRACK_URL}", flush=True)
print(f"Workers: {WORKERS}", flush=True)
if DRY_RUN:
    print("(dry run — nothing posted)", flush=True)

grand_sent = grand_errors = 0
t_start = time.time()

for kind, builder in [("movie", build_movie_payload), ("series", build_series_payload)]:
    print(f"\n--- {kind}s ---", flush=True)
    sent, errors = seed(kind, builder)
    grand_sent += sent
    grand_errors += errors
    print(f"  {kind}s: {sent} sent, {errors} errors", flush=True)

elapsed = time.time() - t_start
print(f"\nDone.  {grand_sent} sent, {grand_errors} errors  ({elapsed/60:.1f}m)", flush=True)
if DRY_RUN:
    print("(dry run — nothing was actually posted)", flush=True)
