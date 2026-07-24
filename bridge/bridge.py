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
import uuid
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

# TMDb API key for fetching metadata when items are missing from Remux.
# Get a free key at https://www.themoviedb.org/settings/api
# Leave empty to disable TMDb auto-import.
TMDB_API_KEY: str = os.environ.get("TMDB_API_KEY", "")

# Per-item dedup: track last-forwarded playback position.
# Prevents re-forwarding every 30s while user is actively watching.
_forward_dedup: dict[str, int] = {}

# Remux UUID v5 namespace (DNS namespace — same as Rust's Uuid::new_v5)
_REMUX_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


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
            "PlaybackPositionTicks": udata.get("PlaybackPositionTicks", 0),
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
        item_type = item.get("Type", "?")
        if item_type == "Episode":
            name = f"{item.get('SeriesName','?')} S{item.get('ParentIndexNumber','?')}E{item.get('IndexNumber','?')}"

        udata = item.get("UserData", {})
        pos_ticks = udata.get("PlaybackPositionTicks", 0)
        pos_sec = pos_ticks / 10_000_000
        played = udata.get("Played", False)
        last_played = udata.get("LastPlayedDate", "?")

        # Skip if we already forwarded this item at the same position.
        item_id = item.get("Id", "")
        prev_pos = _forward_dedup.get(item_id)
        if prev_pos is not None and abs(pos_ticks - prev_pos) < 300_000_000:  # <30s
            continue
        _forward_dedup[item_id] = pos_ticks

        log(f"  -> {item_type}: {name}  pos={pos_sec:.0f}s  played={played}  ts={last_played}")
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


# ═══════════════════════════════════════════════════════════════════════════
# TMDb — fetch metadata for items missing from Remux
# ═══════════════════════════════════════════════════════════════════════════

def _tmdb_get(path: str) -> dict:
    """Call the TMDb v3 API."""
    sep = "&" if "?" in path else "?"
    url = f"https://api.themoviedb.org/3{path}{sep}api_key={TMDB_API_KEY}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _remux_uuid(kind: str, canonical: str) -> str:
    """Replicate Remux's stable UUID: uuid5(DNS_NS, f'{kind}:{canonical}')."""
    return str(uuid.uuid5(_REMUX_NS, f"{kind}:{canonical}"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_media(conn: sqlite3.Connection, **fields) -> str:
    """Insert a row into the media table. Returns the UUID."""
    media_id = fields.pop("id")
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    values = list(fields.values())
    conn.execute(
        f"INSERT OR IGNORE INTO media (id, {columns}) VALUES (?, {placeholders})",
        [media_id] + values,
    )
    return media_id


def _ensure_media_exists(
    conn: sqlite3.Connection,
    media_type: str,
    media_id: str,   # TMDb ID from Yamtrack
    season: int | None = None,
    episode: int | None = None,
) -> str | None:
    """Fetch metadata from TMDb and insert into Remux media table.
    Returns the Remux media UUID, or None if the item couldn't be resolved.
    """
    if not TMDB_API_KEY:
        return None
    try:
        if media_type == "movie":
            return _ensure_movie(conn, int(media_id))
        elif media_type == "episode" and season is not None and episode is not None:
            return _ensure_episode(conn, int(media_id), season, episode)
        elif media_type == "season" and season is not None:
            return _ensure_season(conn, int(media_id), season)
        elif media_type == "tv":
            return _ensure_series(conn, int(media_id))
        else:
            log(f"  TMDb: unsupported media_type={media_type}", "info")
            return None
    except Exception as e:
        log(f"  TMDb: error for {media_type} {media_id}: {e}", "info")
        return None


def _ensure_movie(conn: sqlite3.Connection, tmdb_id: int) -> str | None:
    """Fetch movie from TMDb and insert into Remux."""
    details = _tmdb_get(f"/movie/{tmdb_id}")
    imdb_id = details.get("imdb_id", "")
    if not imdb_id:
        log(f"  TMDb: movie {tmdb_id} has no IMDB ID", "info")
        return None

    title = details.get("title", "Unknown")
    runtime = details.get("runtime") or 0  # minutes → seconds
    runtime_sec = runtime * 60
    year = details.get("release_date", "")[:4]

    media_uuid = _remux_uuid("movie", imdb_id)
    external_ids = json.dumps({"tmdb": tmdb_id, "imdb": imdb_id})

    _insert_media(
        conn,
        id=media_uuid,
        title=title,
        kind="movie",
        runtime=runtime_sec,
        external_ids=external_ids,
        released_at=f"{year}-01-01" if year else None,
        created_at=_now_iso(),
        updated_at=_now_iso(),
        enabled=1,
    )
    log(f"  + movie: {title} ({year})  tmdb={tmdb_id}")
    return media_uuid


def _ensure_series(conn: sqlite3.Connection, tmdb_id: int) -> str | None:
    """Fetch TV series from TMDb and insert into Remux."""
    details = _tmdb_get(f"/tv/{tmdb_id}")
    external = _tmdb_get(f"/tv/{tmdb_id}/external_ids")
    imdb_id = external.get("imdb_id", "")
    if not imdb_id:
        log(f"  TMDb: series {tmdb_id} has no IMDB ID", "info")
        return None

    title = details.get("name", "Unknown")
    year = details.get("first_air_date", "")[:4]
    external_ids = json.dumps({"tmdb": tmdb_id, "imdb": imdb_id})

    media_uuid = _remux_uuid("series", imdb_id)
    _insert_media(
        conn,
        id=media_uuid,
        title=title,
        kind="series",
        external_ids=external_ids,
        released_at=f"{year}-01-01" if year else None,
        created_at=_now_iso(),
        updated_at=_now_iso(),
        enabled=1,
    )
    log(f"  + series: {title} ({year})  tmdb={tmdb_id}")
    return media_uuid


def _ensure_season(
    conn: sqlite3.Connection, series_tmdb_id: int, season_num: int
) -> str | None:
    """Ensure series exists, then insert season. Returns season UUID."""
    series_uuid = _ensure_series(conn, series_tmdb_id)
    if not series_uuid:
        return None

    # Get series IMDB ID for the canonical key
    row = conn.execute(
        "SELECT json_extract(external_ids, '$.imdb') FROM media WHERE id=?",
        (series_uuid,),
    ).fetchone()
    series_imdb = row[0] if row else ""
    if not series_imdb:
        return None

    canonic = f"{series_imdb}:{season_num}"
    season_uuid = _remux_uuid("season", canonic)

    # Get series title for season name
    row = conn.execute("SELECT title FROM media WHERE id=?", (series_uuid,)).fetchone()
    series_title = row[0] if row else "Unknown"

    external_ids = json.dumps({"tmdb": series_tmdb_id, "imdb": series_imdb})
    _insert_media(
        conn,
        id=season_uuid,
        title=f"{series_title} Season {season_num}",
        kind="season",
        parent_id=series_uuid,
        grandparent_id=series_uuid,
        idx=season_num,
        external_ids=external_ids,
        created_at=_now_iso(),
        updated_at=_now_iso(),
        enabled=1,
    )
    log(f"  + season: {series_title} S{season_num}")
    return season_uuid


def _ensure_episode(
    conn: sqlite3.Connection,
    episode_tmdb_id: int,
    season_num: int,
    episode_num: int,
) -> str | None:
    """Fetch episode from TMDb, ensure series/season exist, insert episode.
    Returns the episode's Remux media UUID.

    We need the series TMDb ID, which we get from the TV season endpoint.
    """
    # Episode TMDb ID alone doesn't tell us the series. We use the TMDB
    # episode details which requires series_id. Since we only have the
    # episode TMDb ID from Yamtrack, we try the /tv/episode/{id} endpoint
    # with the episode's TMDB ID. If that doesn't work, we try to find
    # the series via the /find endpoint.

    # Try direct episode lookup
    try:
        ep_details = _tmdb_get(f"/tv/episode/{episode_tmdb_id}")
    except urllib.error.HTTPError:
        log(f"  TMDb: episode {episode_tmdb_id} not found directly", "info")
        return None

    series_id = ep_details.get("show_id")
    if not series_id:
        log(f"  TMDb: episode {episode_tmdb_id} has no show_id", "info")
        return None

    # Ensure series exists
    series_uuid = _ensure_series(conn, series_id)
    if not series_uuid:
        return None

    # Ensure season exists
    season_uuid = _ensure_season(conn, series_id, season_num)
    if not season_uuid:
        return None

    # Get series IMDB ID for canonical key
    row = conn.execute(
        "SELECT json_extract(external_ids, '$.imdb') FROM media WHERE id=?",
        (series_uuid,),
    ).fetchone()
    series_imdb = row[0] if row else ""
    if not series_imdb:
        return None

    canonic = f"{series_imdb}:{season_num}:{episode_num}"
    episode_uuid = _remux_uuid("episode", canonic)

    title = ep_details.get("name", f"Episode {episode_num}")
    runtime = ep_details.get("runtime") or 0  # minutes
    runtime_sec = runtime * 60 if runtime else None

    external_ids = json.dumps({
        "tmdb": episode_tmdb_id,
        "imdb": series_imdb,  # episode doesn't have its own IMDB ID
    })

    _insert_media(
        conn,
        id=episode_uuid,
        title=title,
        kind="episode",
        parent_id=season_uuid,
        grandparent_id=series_uuid,
        idx=episode_num,
        parent_idx=season_num,
        runtime=runtime_sec,
        external_ids=external_ids,
        created_at=_now_iso(),
        updated_at=_now_iso(),
        enabled=1,
    )
    return episode_uuid


# ═══════════════════════════════════════════════════════════════════════════
# REVERSE — apply changes
# ═══════════════════════════════════════════════════════════════════════════

def update_remux_play_state(items: list[dict]) -> str | None:
    """Find matching media in Remux DB by TMDB ID and mark as played.
    Returns the newest written timestamp (ISO format) for cursor sync,
    or None if nothing was written."""
    if not items or not Path(REMUX_DB_PATH).exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{REMUX_DB_PATH}?mode=rw", uri=True, timeout=10)
    except Exception as e:
        log(f"reverse: cannot open Remux DB: {e}", "info")
        return None

    newest_ts: str | None = None
    try:
        for item in items:
            media_id   = item.get("media_id")
            media_type = item.get("media_type", "")
            status     = item.get("status", "")
            title      = item.get("title", "?")
            season     = item.get("season_number")
            episode    = item.get("episode_number")

            # Build display name with season/episode for TV content
            if media_type == "episode" and season is not None and episode is not None:
                display_name = f"{title} S{season}E{episode}"
            elif media_type == "season" and season is not None:
                display_name = f"{title} S{season}"
            else:
                display_name = f"{media_type}: {title}" if media_type else title
            if not media_id:
                continue

            row = conn.execute(
                "SELECT id, runtime FROM media WHERE external_ids LIKE ? LIMIT 1",
                (f'%tmdb":{media_id}%',)
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT id, runtime FROM media WHERE external_ids LIKE ? LIMIT 1",
                    (f'%"imdb":"tt{media_id}%',)
                ).fetchone()
            if not row:
                # Try with 'tmdb' as integer in JSON (e.g. {"tmdb":1315772})
                row = conn.execute(
                    "SELECT id, runtime FROM media WHERE json_extract(external_ids, '$.tmdb') = ? LIMIT 1",
                    (int(media_id),)
                ).fetchone()
            if not row:
                # Item not in Remux — fetch from TMDb and insert
                new_uuid = _ensure_media_exists(conn, media_type, media_id, season, episode)
                if new_uuid:
                    # Re-read runtime from the newly inserted row
                    row = conn.execute(
                        "SELECT id, runtime FROM media WHERE id=?", (new_uuid,)
                    ).fetchone()
                if not row:
                    continue

            remux_media_uuid = row[0]
            runtime = row[1]  # seconds, may be NULL
            user_row = conn.execute(
                "SELECT id FROM users ORDER BY is_admin DESC LIMIT 1"
            ).fetchone()
            if not user_row:
                return None

            user_id = user_row[0]

            # Parse Yamtrack's changed_at timestamp
            changed_at = item.get("changed_at", "")
            yamtrack_ts = None
            if changed_at:
                try:
                    yamtrack_ts = datetime.fromisoformat(changed_at.replace("Z", "+00:00"))
                    item_time = yamtrack_ts.strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    item_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            else:
                item_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Yamtrack is source of truth: only sync if Remux is behind.
            # Check if Remux already has a last_played_at >= Yamtrack's changed_at.
            existing = conn.execute(
                "SELECT last_played_at FROM user_media_state "
                "WHERE user_id = ? AND media_id = ?",
                (user_id, remux_media_uuid)
            ).fetchone()
            if existing and existing[0] and yamtrack_ts:
                try:
                    remux_ts = datetime.strptime(
                        existing[0], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                    if remux_ts >= yamtrack_ts:
                        continue  # Remux already in sync or ahead
                except (ValueError, TypeError):
                    pass

            if status == "Completed":
                conn.execute(
                    "INSERT INTO user_media_state "
                    "(user_id, media_id, play_count, played_at, last_played_at, playback_position) "
                    "VALUES (?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(user_id, media_id) DO UPDATE SET "
                    "play_count=1, played_at=excluded.played_at, "
                    "last_played_at=excluded.last_played_at, playback_position=excluded.playback_position",
                    (user_id, remux_media_uuid, item_time, item_time, runtime or 0)
                )
                log(f"  << {display_name} → played")
            elif status == "In progress":
                progress = item.get("progress", 0)
                position_sec = int(float(progress) * (runtime or 0)) if runtime else 0
                conn.execute(
                    "INSERT INTO user_media_state "
                    "(user_id, media_id, play_count, playback_position, last_played_at) "
                    "VALUES (?, ?, 0, ?, ?) "
                    "ON CONFLICT(user_id, media_id) DO UPDATE SET "
                    "playback_position=excluded.playback_position, "
                    "last_played_at=excluded.last_played_at",
                    (user_id, remux_media_uuid, position_sec, item_time)
                )
                log(f"  << {display_name} → {progress:.0%} ({position_sec}s)")

            # Track newest written timestamp for cursor sync
            if changed_at and (newest_ts is None or changed_at > newest_ts):
                newest_ts = changed_at

        conn.commit()
        if items:
            log(f"reverse: updated {len(items)} item(s) in Remux DB")
        return newest_ts
    except Exception as e:
        log(f"reverse: Remux DB error: {e}", "info")
        return None
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
    newest_written = update_remux_play_state(changes)

    # Always advance cursor past the batch, even if nothing matched.
    # Otherwise unmatched items loop forever.
    timestamps = [c["changed_at"] for c in changes if c.get("changed_at")]
    if timestamps:
        newest = str(max(timestamps))
        save_cursor(REVERSE_CURSOR, newest)
        # Also bump forward cursor so forward sync skips items we just imported.
        save_cursor(FORWARD_CURSOR, newest)
        if newest_written:
            log(f"reverse: synced, cursor → {newest}")
        else:
            log(f"reverse: {len(changes)} items unmatched, cursor → {newest}")
        return newest

    if not newest_written:
        log("reverse: no writes and no timestamps — cursor NOT advanced", "info")
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
