#!/usr/bin/env bash
# Restore the accounts database from a backup made by backup_db.sh.
#
#   deploy/scripts/restore_db.sh storage/backups/app-20261001T031500Z.db.gz
#
# Stops the app, keeps the current database as app.db.before-restore-<time>,
# puts the backup in place, starts the app again (which applies any newer
# migrations on start).
set -euo pipefail

if [ $# -ne 1 ] || [ ! -f "$1" ]; then
    echo "usage: $0 path/to/app-<time>.db.gz" >&2
    exit 2
fi
BACKUP="$(realpath "$1")"
cd "$(dirname "$0")/../.."

compose=false
if [ -f docker-compose.yml ] && command -v docker > /dev/null 2>&1; then
    compose=true
fi

echo "stopping the app"
if $compose; then docker compose stop app; else sudo systemctl stop highlight-cutter; fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -f storage/app.db ]; then
    mv storage/app.db "storage/app.db.before-restore-${STAMP}"
    rm -f storage/app.db-wal storage/app.db-shm
    echo "kept the old database as storage/app.db.before-restore-${STAMP}"
fi
case "$BACKUP" in
    *.gz) gunzip -c "$BACKUP" > storage/app.db ;;
    *) cp "$BACKUP" storage/app.db ;;
esac
if $compose; then sudo chown 10001:10001 storage/app.db 2>/dev/null || true; fi

echo "starting the app"
if $compose; then docker compose start app; else sudo systemctl start highlight-cutter; fi
echo "restored from $BACKUP"
