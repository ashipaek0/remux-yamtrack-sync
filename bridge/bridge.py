#!/usr/bin/env python3
"""
Remux ↔ Yamtrack bridge sidecar.

Two-way sync:
  Forward:  polls Remux items API → POST Jellyfin webhook to Yamtrack
  Reverse:  polls Yamtrack sync-server HTTP API → writes Remux DB via SQLite

Reverse sync data sources (env-var selectable):
  YAMTRACK_SYNC_URL    → HTTP endpoint (yamtrack-sync-server sidecar)
  YAMTRACK_DB_PATH     → local SQLite direct read (co-located)
  Neither              → reverse sync disabled
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ── Configuration ──────────────────────────────────────────────────────────

REMUX_URL: str       = os.environ.get("REMUX_URL", "http://remux:8096").rstrip("/")
REMUX_API_KEY: str   = os.environ["REMUX_API_KEY"]
REMUX_DB_PATH: str   = os.environ.get("REMUX_DB_PATH", "/remux-db/db.sqlite")

YAMTRACK_URL: str    = os.environ.get(
    "YAMTRACK_URL", "https://track.ashipaek0.com.ng"
).rstrip("/")
YAMTRACK_TOKEN: str  = os.environ["YAMTRACK_TOKEN"]

# Reverse sync — pick one
YAMTRACK_SYNC_URL: str | None = os.environ.get("YAMTRACK_SYNC_URL")
YAMTRACK_DB_PATH_DIRECT: str | None = os.environ.get("YAMTRACK_DB_PATH")

FORWARD_INTERVAL: int = int(os.environ.get("FORWARD_INTERVAL", "30"))
REVERSE_INTERVAL: int = int(os.environ.get("REVERSE_INTERVAL", "60"))

CURSOR_DIR: Path     = Path(os.environ.get("CURSOR_DIR", "/data"))
FORWARD_CURSOR: Path = CURSOR_DIR / "forward_cursor.txt"
REVERSE_CURSOR: Path = CURSOR_DIR / "reverse_cursor.txt"

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "info").lower()
DEBUG: bool    = LOG_LEVEL == "debug"


def log(msg: str, level: str = "info") -> None:
    if level == "debug" and not DEBUG:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── Cursor helpers ─────────────────────────────────────────────────────────

def load_cursor(path: Path) -> str | None:
    if path.exists():
        val = path.read_text().strip()
        if val:
            return val
    return None


def save_cursor(path: Path, timestamp: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(timestamp)


# ═══════════════════════════════════════════════════════════════════════════
# FORWARD — Remux → Yamtrack
# ═══════════════════════════════════════════════════════════════════════════

def api_get(path: str) -> dict | list:
    req = urllib.request.Request(
        f"{REMUX_URL}{path}",
        headers={"X-Emby-Token": REMUX_API_KEY, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_user_id() -> str | None:
    users = api_get("/Users")
    if not users or not isinstance(users, list):
        return None
    for u in users:
        if isinstance(u, dict) and u.get("Policy", {}).get("IsAdministrator"):
            return u.get("Id")
    return users[0].get("Id") if users else None


def send_to_yamtrack(payload: dict) -> bool:
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
        log(f"  X Yamtrack {e.code}", "info")
        return False
    except Exception as e:
        log(f"  X Yamtrack error: {e}", "info")
        return False


def build_payload(item: dict) -> dict:
    udata = item.get("UserData", {})
    block: dict[str, Any] = {
        "Name":        item.get("Name"),
        "Type":        item.get("Type"),
        "ProviderIds": item.get("ProviderIds", {}),
        "UserData": {
            "Played":           udata.get("Played", False),
            "PlayCount":        udata.get("PlayCount", 0),
            "PlaybackPosition": udata.get("PlaybackPosition", 0),
            "LastPlayedDate":   udata.get("LastPlayedDate"),
        },
    }
    if item.get("ProductionYear"):
        block["ProductionYear"] = item["ProductionYear"]
    if item.get("Type") == "Episode":
        block["SeriesName"]        = item.get("SeriesName")
        block["ParentIndexNumber"] = item.get("ParentIndexNumber")
        block["IndexNumber"]       = item.get("IndexNumber")
        if item.get("SeriesProviderIds"):
            block["SeriesProviderIds"] = item["SeriesProviderIds"]
    return {"Event": "Stop", "Item": block}


def poll_forward(user_id: str) -> str | None:
    cursor = load_cursor(FORWARD_CURSOR)
    params = (
        f"/Users/{user_id}/Items"
        f"?Recursive=true&SortBy=DatePlayed&SortOrder=Descending"
        f"&Limit=50&Fields=ProviderIds,SeriesProviderIds,UserData,Overview,ProductionYear"
    )
    data = api_get(params)
    items = data.get("Items", []) if isinstance(data, dict) else []
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    new_items: list[dict] = []
    for item in items:
        last_played = item.get("UserData", {}).get("LastPlayedDate")
        if not last_played:
            continue
        if cursor and last_played <= cursor:
            break
        new_items.append(item)

    if not new_items:
        if not cursor:
            save_cursor(FORWARD_CURSOR, now)
        return cursor

    log(f">> {len(new_items)} new forward item(s)")
    for item in reversed(new_items):
        name = item.get("Name", "?")
        if item.get("Type") == "Episode":
            name = f"{item.get('SeriesName','?')} S{item.get('ParentIndexNumber','?')}E{item.get('IndexNumber','?')}"
        log(f"  -> {item.get('Type','?')}: {name}")
        if send_to_yamtrack(build_payload(item)):
            log(f"     OK")

    newest = new_items[0]["UserData"]["LastPlayedDate"]
    save_cursor(FORWARD_CURSOR, newest)
    return newest


# ═══════════════════════════════════════════════════════════════════════════
# REVERSE — Yamtrack → Remux
# ═══════════════════════════════════════════════════════════════════════════

def fetch_reverse_changes(cursor: str) -> list[dict]:
    """Fetch items changed since cursor — HTTP (sync-server) or local SQLite."""
    if YAMTRACK_SYNC_URL:
        return _query_http(cursor)
    elif YAMTRACK_DB_PATH_DIRECT:
        return _query_local(cursor)
    return []


def _query_http(cursor: str) -> list[dict]:
    """Poll yamtrack-sync-server via HTTP."""
    if not YAMTRACK_SYNC_URL:
        return []
    from urllib.parse import quote
    url = f"{YAMTRACK_SYNC_URL.rstrip('/')}/changes?token={YAMTRACK_TOKEN}&since={quote(cursor)}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data.get("items", [])
    except urllib.error.HTTPError as e:
        log(f"reverse HTTP {e.code}", "info")
        return []
    except Exception as e:
        log(f"reverse HTTP error: {e}", "info")
        return []


def _query_local(cursor: str) -> list[dict]:
    """Read Yamtrack SQLite directly (co-located mode)."""
    if not YAMTRACK_DB_PATH_DIRECT or not Path(YAMTRACK_DB_PATH_DIRECT).exists():
        return []
    try:
        conn = sqlite3.connect(
            f"file:{YAMTRACK_DB_PATH_DIRECT}?mode=ro", uri=True, timeout=5
        )
        conn.row_factory = sqlite3.Row

        uid_row = conn.execute(
            "SELECT id FROM users_user WHERE token = ?", (YAMTRACK_TOKEN,)
        ).fetchone()
        if not uid_row:
            conn.close()
            return []
        uid = uid_row["id"]

        rows: list[dict] = []
        for row in conn.execute(
            """
            SELECT i.media_id, i.title, 'movie' AS media_type,
                   m.status, m.progress, m.progressed_at AS changed_at
            FROM app_movie m
            JOIN app_item i ON i.id = m.item_id
            WHERE m.user_id = ? AND m.progressed_at > ?
            ORDER BY m.progressed_at ASC
            """, (uid, cursor)
        ):
            rows.append(dict(row))

        conn.close()
        return rows
    except Exception as e:
        log(f"reverse local DB error: {e}", "info")
        return []


def update_remux_play_state(items: list[dict]) -> bool:
    """Find matching media in Remux DB by TMDB ID and mark as played.
    Returns True if items were written, False if DB was unwritable."""
    if not items or not Path(REMUX_DB_PATH).exists():
        return False

    try:
        conn = sqlite3.connect(f"file:{REMUX_DB_PATH}?mode=rw", uri=True, timeout=10)
    except Exception as e:
        log(f"reverse: cannot open Remux DB: {e}", "info")
        return False

    try:
        for item in items:
            media_id = item.get("media_id")
            status   = item.get("status", "")
            title    = item.get("title", "?")
            if not media_id:
                continue

            row = conn.execute(
                "SELECT id FROM media WHERE external_ids LIKE ? LIMIT 1",
                (f'%tmdb":{media_id}%',)
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT id FROM media WHERE external_ids LIKE ? LIMIT 1",
                    (f'%"imdb":"tt{media_id}%',)
                ).fetchone()
            if not row:
                # Try with 'tmdb' as integer in JSON (e.g. {"tmdb":1315772})
                row = conn.execute(
                    "SELECT id FROM media WHERE json_extract(external_ids, '$.tmdb') = ? LIMIT 1",
                    (int(media_id),)
                ).fetchone()
            if not row:
                continue

            remux_media_uuid = row[0]
            user_row = conn.execute(
                "SELECT id FROM users ORDER BY is_admin DESC LIMIT 1"
            ).fetchone()
            if not user_row:
                return False

            user_id = user_row[0]
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            if status == "Completed":
                conn.execute(
                    "INSERT INTO user_media_state "
                    "(user_id, media_id, play_count, played_at, last_played_at, playback_position) "
                    "VALUES (?, ?, 1, ?, ?, 0) "
                    "ON CONFLICT(user_id, media_id) DO UPDATE SET "
                    "play_count = play_count + 1, played_at=excluded.played_at, "
                    "last_played_at=excluded.last_played_at",
                    (user_id, remux_media_uuid, now_str, now_str)
                )
                log(f"  << {title} → played")
            elif status == "In progress":
                conn.execute(
                    "INSERT INTO user_media_state "
                    "(user_id, media_id, play_count, playback_position, last_played_at) "
                    "VALUES (?, ?, 0, 0, ?) "
                    "ON CONFLICT(user_id, media_id) DO UPDATE SET "
                    "last_played_at=excluded.last_played_at",
                    (user_id, remux_media_uuid, now_str)
                )
                log(f"  << {title} → in progress")

        conn.commit()
        if items:
            log(f"reverse: updated {len(items)} item(s) in Remux DB")
        return True
    except Exception as e:
        log(f"reverse: Remux DB error: {e}", "info")
        return False
    finally:
        conn.close()


def poll_reverse() -> str | None:
    cursor = load_cursor(REVERSE_CURSOR)
    if not cursor:
        cursor = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        save_cursor(REVERSE_CURSOR, cursor)

    changes = fetch_reverse_changes(cursor)
    if not changes:
        log("reverse: no changes", "info")
        return cursor

    log(f"<< {len(changes)} reverse change(s)")
    if not update_remux_play_state(changes):
        log("reverse: writes failed — cursor NOT advanced", "info")
        return cursor

    timestamps = [c["changed_at"] for c in changes if c.get("changed_at")]
    if timestamps:
        newest = str(max(timestamps))
        save_cursor(REVERSE_CURSOR, newest)
        return newest
    return cursor


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    log("Remux ↔ Yamtrack bridge")
    log(f"  Remux:    {REMUX_URL}")
    log(f"  Yamtrack: {YAMTRACK_URL}")
    log(f"  Forward:  every {FORWARD_INTERVAL}s")

    user_id = get_user_id()
    if not user_id:
        log("FATAL: no Remux users", "info")
        sys.exit(1)
    log(f"  User:     {user_id}")

    rev_enabled = bool(YAMTRACK_SYNC_URL or YAMTRACK_DB_PATH_DIRECT)
    if rev_enabled:
        src = YAMTRACK_SYNC_URL or YAMTRACK_DB_PATH_DIRECT
        log(f"  Reverse:  every {REVERSE_INTERVAL}s via {src}")
    else:
        log("  Reverse:  disabled (set YAMTRACK_SYNC_URL or YAMTRACK_DB_PATH)")

    fwd_cursor = load_cursor(FORWARD_CURSOR)
    log(f"  Cursor FWD: {fwd_cursor or '(new)'}")

    tick = 0
    while True:
        tick += 1
        try:
            poll_forward(user_id)
        except urllib.error.HTTPError as e:
            log(f"FWD HTTP {e.code}", "info")
        except urllib.error.URLError as e:
            log(f"FWD network: {e.reason}", "info")
        except Exception as e:
            log(f"FWD error: {e}", "info")

        if rev_enabled and tick % max(1, REVERSE_INTERVAL // FORWARD_INTERVAL) == 0:
            try:
                poll_reverse()
            except Exception as e:
                log(f"REV error: {e}", "info")

        time.sleep(FORWARD_INTERVAL)


if __name__ == "__main__":
    main()
