#!/bin/sh
# Daily backup (cron):  0 3 * * * /srv/address/code/OSMAddressSystem/deploy/backup.sh /srv/address/project /srv/address/backups
# SQLite: consistent copy through the app's own backup command. PostgreSQL: set ADDRESS_DB_URL and use pg_dump instead.
set -e
PROJECT=${1:?project folder}; OUT=${2:?backup folder}; mkdir -p "$OUT"
if [ -n "$ADDRESS_DB_URL" ] && [ "${ADDRESS_DB_URL#postgres}" != "$ADDRESS_DB_URL" ]; then
  pg_dump "$(echo "$ADDRESS_DB_URL" | sed 's/+psycopg//')" | gzip > "$OUT/address-$(date +%F).sql.gz"
else
  python -m osmaddresssystem.app backup --project "$PROJECT" --to "$OUT/project-$(date +%F).db"
fi
find "$OUT" -type f -mtime +30 -delete
