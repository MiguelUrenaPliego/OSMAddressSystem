# Hosting the community address app

The app is one Python process (FastAPI + uvicorn) with a project folder (`input/`, `network/`) and a database (SQLite file or PostgreSQL).
It is meant for **2-3 trusted editors**; there are no accounts in the app, so protect it with the reverse proxy password.

## Quick start (Docker)

```bash
cd code/OSMAddressSystem
docker compose -f deploy/docker-compose.yml up -d --build       # http://127.0.0.1:8000, data in the volume "address-data"
```

The first start copies the Fotouni project (`projects/fotouni`) into the volume; later starts keep your data.

## On the internet

1. Point a domain at the server; edit `deploy/Caddyfile` (domain and the password hash from `caddy hash-password`).
2. `docker compose -f deploy/docker-compose.yml --profile public up -d --build` - Caddy gets the HTTPS certificate by itself and asks the editors for the shared password.
3. Only Caddy is published (80/443); the app listens on 127.0.0.1.

## PostgreSQL instead of SQLite

SQLite (write-ahead log) is fine for 2-3 editors. For a shared server database set `ADDRESS_DB_URL=postgresql+psycopg://user:password@host/dbname`
(uncomment the line in `docker-compose.yml` and start with `--profile postgres`). The tables are created on first start.

## Without Docker

`uv sync --extra app --extra planning --extra server`, then `deploy/address-app.service` (systemd) or
`uv run python -m osmaddresssystem.app serve --project PROJECT --host 127.0.0.1 --port 8000`, behind Caddy / nginx.

## Rules

* **One worker only** (the computed network is kept in memory). Do not start uvicorn with `--workers`.
* **Health**: `GET /api/health` returns the database, the revision and whether a computation runs (used by the Docker health check).
* **Backups**: `deploy/backup.sh PROJECT BACKUPS` from cron (SQLite copy through the app, or `pg_dump`). Restore = put the file back as `project.db` / `psql < dump`.
* **Uploads** (KML etc.) are limited to 60 MB by the app and by the proxy.
* The database of an older single-editor version is refused with a message; create the project again with `init`.
* To start a different area: `python -m osmaddresssystem.app init --project DIR --streets-osm ... --area ... --buildings ...` then `setup`.
