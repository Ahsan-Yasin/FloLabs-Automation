# Deploying Highlight Cutter

This guide takes a fresh Linux server to a running site at `https://your-domain`
with accounts, email, the API and automatic HTTPS. Allow about 30 minutes.

The whole product is one Docker image (the website, the API and the video
renderer) behind [Caddy](https://caddyserver.com), which gets and renews the
HTTPS certificate by itself.

## 1. What you need

| Thing | Notes |
|---|---|
| A server | Ubuntu 24.04, 2 vCPU, 4 GB RAM, 40 GB disk or more. AWS t3.medium / c6a.large, Hetzner CPX21 or similar. No GPU. |
| A domain | For example `cut.example.com`, pointing at the server (step 2). |
| An email sender | Resend (free tier) or any SMTP account. Without one, nobody can confirm their address. |
| An AI key | OpenAI (default) or Gemini. It only ever sees transcript text. |

Ports 80 and 443 must be open to the internet (80 is needed for the certificate).

## 2. Point the domain at the server

At your DNS provider, add an `A` record: name `cut` (or `@`), value = the
server's public IP. Wait until `ping cut.example.com` shows that IP.

## 3. Install

On the server:

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/prod/deploy/scripts/bootstrap_ec2.sh -o bootstrap.sh
sudo DOMAIN=cut.example.com REPO_URL=https://github.com/<you>/<repo>.git BRANCH=prod bash bootstrap.sh
```

This installs Docker, clones the code into `/opt/highlight-cutter`, and creates
`/opt/highlight-cutter/.env` with production defaults and a fresh `JWT_SECRET`.
It stops there the first time, because email still needs setting up.

## 4. Fill in `.env`

```bash
sudo nano /opt/highlight-cutter/.env
```

Required for production (`APP_ENV=prod` refuses to start without them, and
prints exactly what is missing):

| Setting | Value |
|---|---|
| `APP_BASE_URL` | `https://cut.example.com` (set by the script when you passed `DOMAIN`) |
| `DOMAIN` | `cut.example.com` (Caddy uses it for the certificate) |
| `JWT_SECRET` | already generated; never share it or commit it |
| `EMAIL_BACKEND`, `EMAIL_FROM` | see "Email" below |
| `OPENAI_API_KEY` | or `LLM_PROVIDER=gemini` with `GEMINI_API_KEY` |
| `ADMIN_EMAILS` | your address: it becomes an admin once confirmed |

Worth reviewing: `SIGNUP_ENABLED` (open sign-up) and `ALLOWED_SIGNUP_DOMAINS`
(only these email domains may sign up), `PLAN_JOBS_PER_MONTH` and
`PLAN_MAX_MINUTES` (free-plan limits; 0 = unlimited), `CONTACT_EMAIL`.

`.env` holds secrets. It stays on the server, readable only by root
(`chmod 600`), and is never committed to git.

### Email

The default, `EMAIL_BACKEND=console`, delivers nothing: it writes every email
to the server log. The site says so on its pages while it is set. Pick one:

**Resend (recommended; it has a free tier).**
1. Sign up at resend.com and add your domain under *Domains*.
2. Add the DNS records it shows (SPF and DKIM) at your DNS provider, and wait
   for *Verified*.
3. Create an API key with *Sending access*.
4. In `.env`:
   ```
   EMAIL_BACKEND=resend
   EMAIL_FROM=Highlight Cutter <no-reply@cut.example.com>
   RESEND_API_KEY=re_...
   ```
Until the domain is verified, Resend only delivers to your own account address.

**SMTP (Gmail, Brevo, Amazon SES, Postmark...).** For Gmail: turn on 2-step
verification, create an *App password*, then:
```
EMAIL_BACKEND=smtp
EMAIL_FROM=Highlight Cutter <you@gmail.com>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS=starttls
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<the 16-character app password>
```
Gmail is fine for testing and small volumes (about 500 emails a day); use a
domain-verified sender for real users, or mail lands in spam.

## 5. Start

```bash
cd /opt/highlight-cutter
sudo docker compose up -d --build
sudo docker compose ps
curl -s https://cut.example.com/health
```

`/health` answers with `"status": "ok"`, `"db": "ok"` and the email backend in use.
The first start creates the database (`storage/app.db`) and applies migrations.

## 6. First checks

```bash
# send yourself a test email through the configured backend
sudo docker compose exec app python -m services.email --test you@example.com

# no email yet? confirm an address by hand
sudo docker compose exec app python -m db.cli verify-email you@example.com

# or create an admin directly (asks for a password)
sudo docker compose exec app python -m db.cli create-admin you@example.com
```

Then open the site, sign up (or log in), and cut a short YouTube video.

Other admin commands: `python -m db.cli stats`, `promote`, `demote`,
`activate`, `deactivate`, `set-password`, `current`, `backup DEST`.

## 7. Connect n8n, Zapier or Make

1. Log in, open **API keys**, create a key with the scopes the automation needs.
2. Call `POST https://cut.example.com/api/v1/jobs/youtube` with the header
   `Authorization: Bearer hc_live_...` and `{"url": "...", "callback_url": "..."}`.
3. Your `callback_url` receives a signed `job.done` webhook; download
   `job.links.bundle` with the same header.

The site's `/docs` page has recipes and signature-checking code. Older flows that
use the unprefixed routes (`/jobs`) and `HC_API_TOKEN` keep working while
`LEGACY_API_ENABLED=true`; move them to `/api/v1` and a per-user key, then turn
it off and empty `HC_API_TOKEN`.

## 8. Backups

The accounts database is small and precious; videos are big and can be re-made.

```bash
# one backup now (safe while the app runs): storage/backups/app-<time>.db.gz
sudo deploy/scripts/backup_db.sh
# nightly, keeping 14 copies (and uploading to S3 if you set BACKUP_S3_URI)
sudo crontab -e
15 3 * * * cd /opt/highlight-cutter && deploy/scripts/backup_db.sh >> storage/backups/backup.log 2>&1
# restore (stops the app, keeps the old file next to it, starts again)
sudo deploy/scripts/restore_db.sh storage/backups/app-20261001T031500Z.db.gz
```

Copy backups off the server now and then (S3, another machine). A backup on the
same disk doesn't survive the disk.

## 9. Updating

```bash
cd /opt/highlight-cutter
sudo deploy/scripts/backup_db.sh
sudo git pull --ff-only
sudo docker compose up -d --build
```

Database migrations run by themselves on start. A job that was rendering during
the restart is marked "interrupted"; render it again from its page.

## 10. Disk, sizing and cost

- A job needs working space of roughly three times its recording. The app
  refuses new jobs below `MIN_FREE_DISK_BYTES` (6 GiB by default).
- `DELETE_SOURCE_WHEN_DONE=true` removes the original video once the zip exists.
  `JOB_RETENTION_HOURS=168` deletes finished jobs after a week. The bootstrap
  script sets both.
- One job renders at a time; others wait in a queue (`MAX_QUEUE_DEPTH`).
  Render time grows with the recording's length and the CPU: time one of your
  own meetings before settling on an instance size.
- AI cost is about $0.02 per 98-minute meeting with the default model. Server
  cost is the instance itself: about $30 a month for an always-on t3.medium.

## 11. Logs and troubleshooting

```bash
sudo docker compose logs -f app       # the app (JSON lines when LOG_JSON=true)
sudo docker compose logs -f caddy     # certificates and HTTP
```

Every response carries an `X-Request-ID` header, and every log line of that
request carries the same id, so a user's report can be matched to the log.

| Symptom | Cause and fix |
|---|---|
| The app exits at start with "refusing to start with APP_ENV=prod" | The message lists the missing settings; fix `.env`, then `docker compose up -d`. |
| Nobody receives emails | `EMAIL_BACKEND` is still `console`, or the sender domain isn't verified. Run the test-email command above and read its error. |
| `PermissionError` on `storage/` | `sudo chown -R 10001:10001 storage` (the container runs as uid 10001). |
| Upload stops at 8 GB | Caddy's `request_body max_size` in `deploy/caddy/Caddyfile`. |
| YouTube jobs fail to download | YouTube changes often: `docker compose build --pull --no-cache app` picks up the latest yt-dlp. |
| Jobs fail with `transcript_not_ready` | The video has no captions and the upload had no .vtt/.srt; this server doesn't transcribe (no WhisperX in the image). |
| No certificate | Ports 80/443 closed, or DNS doesn't point here yet. `docker compose logs caddy` says which. |

## 12. Without Docker (systemd)

For a server where Docker isn't wanted:

```bash
sudo apt install -y python3.12-venv ffmpeg fonts-dejavu-core git caddy
sudo useradd --system --create-home hc
sudo git clone -b prod https://github.com/<you>/<repo>.git /opt/highlight-cutter
cd /opt/highlight-cutter
sudo python3.12 -m venv .venv && sudo .venv/bin/pip install -r requirements-server.txt
sudo cp .env.example .env && sudo nano .env        # as in step 4, plus TRUST_PROXY=true
sudo mkdir -p storage && sudo chown -R hc:hc storage
sudo cp deploy/systemd/highlight-cutter.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now highlight-cutter
# Caddy: use deploy/caddy/Caddyfile with UPSTREAM=127.0.0.1:8000 and DOMAIN set
```

YouTube downloads also need a JavaScript runtime: install Deno
(`curl -fsSL https://deno.land/install.sh | sh`, then put it on the service's PATH).

Run exactly one app process per `storage/` folder: the job queue lives in
memory, so never add uvicorn `--workers`.

## 13. Try the stack on your own computer

With Docker Desktop running:

```bash
cp .env.example .env            # dev defaults, plus APP_BASE_URL=https://localhost
docker compose up -d --build
```

Open https://localhost and accept Caddy's local certificate. Emails appear in
`docker compose logs app`.
