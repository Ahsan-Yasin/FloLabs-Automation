#!/usr/bin/env bash
# First-time setup of a fresh Ubuntu 24.04 server (EC2 or any VPS).
#
#   curl -fsSL <raw URL of this file> -o bootstrap.sh
#   sudo DOMAIN=cut.example.com REPO_URL=https://github.com/<you>/<repo>.git BRANCH=prod bash bootstrap.sh
#
# What it does, idempotently: installs Docker, clones (or updates) the repo in
# /opt/highlight-cutter, creates .env from .env.example with a fresh
# JWT_SECRET (only if .env doesn't exist yet), prepares storage/, and starts
# everything with docker compose. It never overwrites an existing .env.
set -euo pipefail

: "${REPO_URL:?set REPO_URL to the git URL of this repository}"
BRANCH="${BRANCH:-prod}"
DOMAIN="${DOMAIN:-}"
APP_DIR="${APP_DIR:-/opt/highlight-cutter}"

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root (sudo)" >&2
    exit 1
fi

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 git curl sqlite3
systemctl enable --now docker

echo "== code in ${APP_DIR} (branch ${BRANCH})"
if [ -d "${APP_DIR}/.git" ]; then
    git -C "${APP_DIR}" fetch --quiet origin "${BRANCH}"
    git -C "${APP_DIR}" checkout --quiet "${BRANCH}"
    git -C "${APP_DIR}" pull --quiet --ff-only origin "${BRANCH}"
else
    git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi
cd "${APP_DIR}"

echo "== .env"
if [ ! -f .env ]; then
    cp .env.example .env
    secret="$(openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-64)"
    sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${secret}|" .env
    sed -i "s|^APP_ENV=.*|APP_ENV=prod|" .env
    sed -i "s|^REQUIRE_NATIVE_TRANSCRIPT=.*|REQUIRE_NATIVE_TRANSCRIPT=true|" .env
    # production defaults: JSON logs, drop the source video once the zip exists,
    # delete finished jobs after a week (see DEPLOY.md, "Disk")
    sed -i "s|^LOG_JSON=.*|LOG_JSON=true|" .env
    sed -i "s|^DELETE_SOURCE_WHEN_DONE=.*|DELETE_SOURCE_WHEN_DONE=true|" .env
    sed -i "s|^JOB_RETENTION_HOURS=.*|JOB_RETENTION_HOURS=168|" .env
    if [ -n "${DOMAIN}" ]; then
        sed -i "s|^APP_BASE_URL=.*|APP_BASE_URL=https://${DOMAIN}|" .env
        sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" .env
    fi
    chmod 600 .env
    echo "created .env: fill in EMAIL_BACKEND/EMAIL_FROM/SMTP_* or RESEND_API_KEY and the AI key, then run:"
    echo "  cd ${APP_DIR} && docker compose up -d --build"
    created_env=true
else
    echo ".env exists: left as it is"
    created_env=false
fi

echo "== storage"
mkdir -p storage/backups
chown -R 10001:10001 storage

if [ "${created_env}" = true ]; then
    echo "== stopping here: the app refuses to start in prod until email is configured (see DEPLOY.md)"
    exit 0
fi

echo "== start"
docker compose up -d --build
docker compose ps
echo "done: https://${DOMAIN:-localhost}"
