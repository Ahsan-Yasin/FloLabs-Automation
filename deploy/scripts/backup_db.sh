#!/usr/bin/env bash
# Back up the accounts database (SQLite) while the app keeps running.
#
#   deploy/scripts/backup_db.sh                     # keeps the last 14 in storage/backups
#   BACKUP_S3_URI=s3://my-bucket/hc deploy/scripts/backup_db.sh   # also uploads (needs the aws CLI)
#
# Nightly at 03:15 (crontab -e as the deploy user):
#   15 3 * * * cd /opt/highlight-cutter && deploy/scripts/backup_db.sh >> storage/backups/backup.log 2>&1
#
# Job folders (videos) are not backed up: they are large and can be re-made.
set -euo pipefail

cd "$(dirname "$0")/../.."
KEEP="${BACKUP_KEEP:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="app-${STAMP}.db"
mkdir -p storage/backups

if [ -f docker-compose.yml ] && docker compose ps --status running app > /dev/null 2>&1 \
    && [ -n "$(docker compose ps --status running -q app)" ]; then
    # inside the container storage/ is /app/storage
    docker compose exec -T app python -m db.cli backup "/app/storage/backups/${NAME}"
else
    .venv/bin/python -m db.cli backup "storage/backups/${NAME}"
fi

gzip -f "storage/backups/${NAME}"
echo "wrote storage/backups/${NAME}.gz"

if [ -n "${BACKUP_S3_URI:-}" ]; then
    aws s3 cp "storage/backups/${NAME}.gz" "${BACKUP_S3_URI%/}/${NAME}.gz" --only-show-errors
    echo "uploaded to ${BACKUP_S3_URI%/}/${NAME}.gz"
fi

# keep the newest $KEEP local copies
ls -1t storage/backups/app-*.db.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
