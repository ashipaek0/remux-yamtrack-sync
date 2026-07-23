# remux-yamtrack-sync

Two-way play-state sync between **Remux** (Jellyfin-compatible media server) and **Yamtrack** (media tracker).

Two components:

| Component | Runs on | Purpose |
|---|---|---|
| **Bridge** (`bridge/`) | Alongside Remux | Polls Remux API → posts Jellyfin webhook to Yamtrack (forward). Polls sync-server → writes Remux SQLite (reverse). |
| **Sync-server** (`sync-server/`) | Alongside Yamtrack | Read-only HTTP shim over Yamtrack's SQLite. Exposes `GET /changes` for the bridge to poll. |

## Architecture

```
┌──────────────┐     forward (every 30s)     ┌──────────────┐
│   Remux DB   │ ←────── POST webhook ──────→│   Yamtrack   │
│   (SQLite)   │                              │  (Django)    │
└──────┬───────┘                              └──────┬───────┘
       │                                             │
       │  reverse (every 60s)                        │
       │ ←──── SQLite write ─────┐                   │
       │                         │                   │
       │               ┌────────┴────────┐           │
       │               │  Bridge (Python) │           │
       │               └────────┬────────┘           │
       │                         │                   │
       │                  GET /changes?token=...&since=...
       │                         │                   │
       │               ┌────────┴────────┐           │
       │               │  Sync-server    │───────────┘
       │               │  (Python HTTP)  │  reads SQLite
       │               └─────────────────┘
```

## Quick Start

### Sync-server (on Yamtrack host)

Add to your Yamtrack `docker-compose.yml`:

```yaml
  yamtrack-sync:
    image: ashipaek0/yamtrack-sync:latest
    environment:
      BIND: "0.0.0.0:8001"
      DB_PATH: /yamtrack/db/db.sqlite3
    ports:
      - "8001:8001"
    volumes:
      - ./db:/yamtrack/db:ro
    restart: unless-stopped
```

### Bridge (on Remux host)

```yaml
  remux-bridge:
    image: ashipaek0/remux-bridge:latest
    environment:
      REMUX_URL: http://remux:3000
      REMUX_API_KEY: <remux-api-key>
      REMUX_DB_PATH: /remux-db/db.sqlite
      YAMTRACK_URL: <your_yamtrack_url>
      YAMTRACK_TOKEN: <yamtrack-user-token>
      YAMTRACK_SYNC_URL: http://yamtrack-sync:8001
      FORWARD_INTERVAL: "30"
      REVERSE_INTERVAL: "60"
    volumes:
      - ./remux/data:/remux-db:ro
      - ./bridge-data:/data
    restart: unless-stopped
```

## Environment Variables

### Bridge

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `REMUX_URL` | Yes | `http://remux:8096` | Remux API base URL |
| `REMUX_API_KEY` | Yes | — | Remux API key (`X-Emby-Token`) |
| `REMUX_DB_PATH` | Yes | `/remux-db/db.sqlite` | Path to Remux SQLite (for reverse sync writes) |
| `YAMTRACK_URL` | Yes | — | Yamtrack base URL (forward webhook target) |
| `YAMTRACK_TOKEN` | Yes | — | Yamtrack user token (from `users_user.token`) |
| `YAMTRACK_SYNC_URL` | No | — | Sync-server HTTP endpoint (enables reverse sync) |
| `YAMTRACK_DB_PATH` | No | — | Path to Yamtrack SQLite (co-located mode, alternative to sync-server) |
| `FORWARD_INTERVAL` | No | `30` | Seconds between forward polls |
| `REVERSE_INTERVAL` | No | `60` | Seconds between reverse polls |
| `LOG_LEVEL` | No | `info` | Set to `debug` for verbose logging |

### Sync-server

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BIND` | No | `0.0.0.0:8001` | Listen address |
| `DB_PATH` | No | `/yamtrack/db/db.sqlite3` | Path to Yamtrack SQLite (read-only) |

## Getting your credentials

Both the **Yamtrack token** and **Remux API key** can be found in their respective web UIs.
