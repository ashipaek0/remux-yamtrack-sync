#!/usr/bin/env python3
"""
yamtrack-sync-server — read-only HTTP shim over Yamtrack SQLite.

Exposes GET /changes?token=<TOKEN>&since=<ISO_TIMESTAMP>
Returns items with status changes since the cursor.

Designed to sit alongside Yamtrack's compose, mounting the same ./db volume.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB_PATH = os.environ.get("DB_PATH", "/yamtrack/db/db.sqlite3")
BIND   = os.environ.get("BIND", "0.0.0.0:8001")
HOST, PORT = BIND.split(":", 1)
PORT = int(PORT)


def query_changes(cursor_iso: str, user_token: str) -> list[dict]:
    """Return movies and episodes whose play state changed after cursor_iso."""
    # Normalize ISO cursor to SQLite format (space instead of T, no Z suffix)
    cursor = cursor_iso.replace("T", " ").replace("Z", "")

    rows: list[dict] = []
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    cur = conn.execute

    try:
        user_row = cur(
            "SELECT id FROM users_user WHERE token = ?", (user_token,)
        ).fetchone()
        if not user_row:
            return []
        uid = user_row["id"]

        # Movies: status, progress, progressed_at
        try:
            for row in cur(
                """
                SELECT i.media_id, i.title, m.status, m.progress,
                       m.progressed_at AS changed_at
                FROM app_movie m
                JOIN app_item i ON i.id = m.item_id
                WHERE m.user_id = ? AND m.progressed_at > ?
                ORDER BY m.progressed_at ASC
                """, (uid, cursor)
            ):
                rows.append({
                    "media_type": "movie",
                    "media_id": row["media_id"],
                    "title": row["title"],
                    "status": row["status"],
                    "progress": row["progress"],
                    "changed_at": row["changed_at"].replace(" ", "T") + "Z" if row["changed_at"] else None,
                })
        except Exception:
            pass

        # TV episodes: end_date marks completion
        try:
            for row in cur(
                """
                SELECT i.media_id, i.title, i.season_number, i.episode_number,
                       e.end_date AS changed_at
                FROM app_episode e
                JOIN app_item i ON i.id = e.item_id
                JOIN app_season s ON s.id = e.related_season_id
                JOIN app_tv t ON t.item_id = s.item_id AND t.user_id = ?
                WHERE t.user_id = ? AND e.end_date IS NOT NULL AND e.end_date > ?
                ORDER BY e.end_date ASC
                """, (uid, uid, cursor)
            ):
                rows.append({
                    "media_type": "episode",
                    "media_id": row["media_id"],
                    "title": row["title"],
                    "season_number": row["season_number"],
                    "episode_number": row["episode_number"],
                    "status": "Completed",
                    "changed_at": row["changed_at"].replace(" ", "T") + "Z" if row["changed_at"] else None,
                })
        except Exception:
            pass

        # TV series-level status changes
        try:
            for row in cur(
                """
                SELECT i.media_id, i.title, t.status, t.created_at AS changed_at
                FROM app_tv t
                JOIN app_item i ON i.id = t.item_id
                WHERE t.user_id = ? AND t.created_at > ?
                ORDER BY t.created_at ASC
                """, (uid, cursor)
            ):
                rows.append({
                    "media_type": "tv",
                    "media_id": row["media_id"],
                    "title": row["title"],
                    "status": row["status"],
                    "changed_at": row["changed_at"].replace(" ", "T") + "Z" if row["changed_at"] else None,
                })
        except Exception:
            pass

    finally:
        conn.close()
    return rows


class SyncHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)

        if parsed.path == "/health":
            self._json({"ok": True})
            return

        if parsed.path != "/changes":
            self._json({"error": "not found"}, 404)
            return

        token = (qs.get("token") or [None])[0]
        since = (qs.get("since") or [None])[0]

        if not token:
            self._json({"error": "token required"}, 400)
            return
        if not since:
            since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            data = query_changes(since, token)
            cursor = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._json({"items": data, "cursor": cursor})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {args[0]} {args[1]} {args[2]}", flush=True)


if __name__ == "__main__":
    print(f"yamtrack-sync-server listening on {HOST}:{PORT}", flush=True)
    httpd = HTTPServer((HOST, PORT), SyncHandler)
    httpd.serve_forever()
