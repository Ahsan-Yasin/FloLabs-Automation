# Highlight Cutter architecture

How the whole product works, from an HTTP request to the finished zip. It is
written for someone changing the code: every part names the module and function
to open. Function names are used instead of line numbers because lines move.

Companion documents: [DEPLOY.md](DEPLOY.md) (running it), [DESIGN.md](DESIGN.md)
(the website's design system), [plan.MD](plan.MD) (why the product layer looks
the way it does), [README.md](README.md) (quick start).

## 1. The product in one paragraph

A person or an automation hands over a meeting recording: a YouTube link, or an
uploaded .mp4/.mkv/.mov, optionally with its .vtt/.srt transcript. The service
reads the transcript (YouTube captions, the uploaded file, or local speech
recognition as a last resort), asks an LLM to mark every sentence keep or cut and
to score how much someone could learn from it, turns those marks into a
frame-exact edit list, and renders: `final.mp4` (a highlights reel, a title
card, then the cleaned meeting), vertical shorts, `removed.mp4` (every cut with
its reason), transcripts, YouTube chapters and a PDF report, all packed into
`bundle.zip`. Accounts, API keys, signed webhooks and a website wrap it into a
product.

## 2. One process, four kinds of work

```
                        ┌────────────────────────── one Python process (uvicorn) ──────────────────────────┐
 browser / n8n ──HTTPS──► Caddy ──► SecurityHeadersMiddleware ─► AuthMiddleware ─► FastAPI routers           │
                        │           (CSP nonce, request id,        (who is calling,   ├ /api/v1/...  JSON API  │
                        │            security headers)              CSRF, 401 early)  ├ /jobs/...    legacy    │
                        │                                                             └ /, /app, ... pages     │
                        │                                                                   │                  │
                        │   job-worker thread ◄── JobQueue (FIFO, one job at a time) ◄──────┘ submit         │
                        │        │  run_pipeline / run_youtube_pipeline                                       │
                        │        ├─► ffmpeg / ffprobe subprocesses (render)                                   │
                        │        ├─► LLM over HTTPS (OpenAI or Gemini; transcript text only)                  │
                        │        └─► job.json + files in storage/jobs/<id>/                                   │
                        │   webhook thread  ─► POST signed events to callback URLs (retries)                  │
                        │   email pool      ─► SMTP / Resend (or the log)                                     │
                        └──────────────────────────────────┬──────────────────────────────────────────────────┘
                                                            │
                                storage/  app.db (SQLite: accounts, keys, sessions, jobs index, deliveries)
                                          jobs/<id>/ (job.json, sources, intermediate JSON, outputs, bundle.zip)
                                          videos/   (YouTube downloads)
```

- **One process per storage folder.** The job queue lives in memory and renders
  one job at a time, so the app never runs with several uvicorn workers.
- **Two kinds of state.** Job state is JSON on disk (`storage/jobs/<id>/job.json`),
  which the pipeline has always used. The product layer (users, keys, sessions,
  webhook deliveries) lives in a SQL database, SQLite by default. A `jobs_index`
  table mirrors the job files so listing and ownership checks don't read every
  `job.json`.
- **Background threads.** The job worker (`api/queue.py`), the webhook sender
  (`services/webhooks.py`) and a small thread pool for email (`services/email.py`)
  all live inside the same process.

## 3. Repository map

| Folder or file | What lives there |
|---|---|
| `api/main.py` | The FastAPI app: startup (`lifespan`), middleware, the job routes, router mounting |
| `api/auth.py` | `AuthMiddleware` (pure ASGI), CSRF rule, cookie helpers, `require()` scope dependencies, `client_ip` |
| `api/security.py` | `SecurityHeadersMiddleware`: CSP with a per-request nonce, request id, security and cache headers |
| `api/routers/` | `auth.py` (accounts, sessions), `keys.py`, `account.py`, `admin.py`, `pages.py` (the website) |
| `api/queue.py`, `api/jobs.py` | `JobQueue` (the worker thread) and `JobStore` (job.json persistence and cache) |
| `api/ratelimit.py`, `api/errors.py`, `api/openapi.py` | Token-bucket limits, the error shape, the published schema |
| `services/` | Product logic with no HTTP in it: `auth`, `sessions`, `tokens`, `passwords`, `api_keys`, `scopes`, `users`, `email`, `notifications`, `webhooks`, `jobs_index`, `usercache` |
| `db/` | SQLAlchemy models, engine/session, Alembic migrations, the admin CLI (`python -m db.cli`) |
| `core/` | Settings (`config.py`), job models (`models.py`), errors, subprocess runner (`proc.py`), frame maths (`timeline.py`), logging |
| `pipeline.py` | The job pipeline: pre-flight, transcribe, decide, edit list, then `deliver` |
| `ingest/` | Uploads (`store.py`), YouTube (`youtube.py`), source validation (`validate.py`) |
| `transcribe/` | Native transcripts (`native.py`: VTT/SRT/YouTube captions), WhisperX fallback, overlap flags |
| `decide/` | Prompts, LLM clients, judging, re-rank, chapters, fragment repair |
| `edl/` | Edit decision list builder, highlight and short selection |
| `slice/` | Rendering: encoder profile, render plans, ffmpeg graphs, tracks, captions, silence detection |
| `outputs.py`, `deliver.py` | Render every deliverable; transcripts, chapters, report, manifest, zip, cleanup |
| `report/`, `bundle/` | Text transcripts and the PDF report; manifest and zip |
| `web/templates/`, `web/static/` | Jinja2 pages and emails; one stylesheet, small vanilla scripts, vendored Motion |
| `tests/` | pytest suite (external services are faked) |
| `deploy/`, `Dockerfile`, `docker-compose.yml` | Deployment (see DEPLOY.md) |
| `tools/` | Regression scripts for the 98-minute reference meeting (job `7b93...`) |

## 4. A request's path

### 4.1 Middleware (outermost first)

1. **`SecurityHeadersMiddleware`** (`api/security.py`) runs for every response.
   - It takes the incoming `X-Request-ID` when it looks sane (8-64 characters of
     `[A-Za-z0-9._-]`), otherwise makes one, echoes it on the response, and sets
     it in a context variable so every log line of the request carries it.
   - It makes a fresh CSP nonce per request. HTML pages get a Content-Security-Policy
     that allows scripts only from this site plus inline scripts with that nonce;
     the Swagger page gets its own policy for the CDN it uses.
   - It adds `nosniff`, `DENY` framing, a strict referrer policy, a permissions
     policy, HSTS when the site is https, and cache headers: versioned static files
     (`?v=<hash>`) are immutable for a year, JSON is `no-store`, HTML `no-cache`.
2. **`AuthMiddleware`** (`api/auth.py`) runs before routing and before anything
   reads the body.
   - It turns the request's credentials into a `Principal` (below) and stores it in
     `request.state.principal`.
   - API paths (`/api/...` and the legacy `/jobs...`) without a caller get 401 here,
     except a short public list (health, login, sign-up, verify, forgot, reset,
     public config, the schema). An anonymous multi-GB upload is refused before a
     byte of it is written.
   - **CSRF:** an unsafe method (POST, PUT, PATCH, DELETE) authenticated by cookies
     must carry `X-Requested-With: hc-web`. The site's fetch wrapper sends it; a form
     on another site can't. Together with `SameSite=Lax` cookies this blocks
     cross-site requests. API keys and bearer tokens are exempt (no ambient cookie).

### 4.2 Who is calling (`services/auth.py`, `resolve`)

The first credential present decides. An invalid one does **not** fall back to
the next, so a bad key is always a clear 401.

| Order | Credential | Becomes |
|---|---|---|
| 1 | `Authorization: Bearer hc_live_...` | an API key: its owner, with the key's scopes |
| 2 | `Authorization: Bearer <JWT>` | an access token from `/auth/login` (all scopes) |
| 3 | `X-API-Key: hc_live_...` | an API key |
| 4 | `X-API-Key: <HC_API_TOKEN>` | the operator: admin, all scopes (for older n8n flows) |
| 5 | cookie `hc_api_key` = `HC_API_TOKEN` | the operator, from the old single-page UI |
| 6 | cookie `hc_access` | a browser session |
| 7 | cookie `hc_refresh`, GET/HEAD only | a browser session whose 15-minute access cookie just expired (video range requests, downloads) |

With no operator key configured, `DEV_OPEN_API=true` and `APP_ENV=dev`, requests
without credentials act as the operator (local development only; the tests use it
for the pre-accounts suite).

User details (role, verified, active) come from `services/usercache.py`, a short
in-process cache invalidated whenever a user changes, so every request doesn't
hit the database.

### 4.3 Scopes, ownership and limits

- **Scopes** (`services/scopes.py`): `jobs:read` and `jobs:write`. Routes declare
  what they need with `Depends(require(JOBS_READ))` etc. Sessions, access tokens
  and the operator have every scope.
- **Ownership** (`api/main.py`, `_can_see` and `_owner_for`): a caller sees only
  jobs whose `owner_id` is theirs; admins and the operator see everything. A job
  that isn't yours answers 404, never 403, so ids can't be probed.
- **Verified email** is required to create jobs and API keys (`email_not_verified`).
- **Admission** (`_admission`): per-user jobs waiting or running at once
  (`MAX_QUEUED_PER_USER`, 429 `too_many_jobs`), monthly jobs and recording length
  for non-admins (`PLAN_JOBS_PER_MONTH`, `PLAN_MAX_MINUTES`, 402 `plan_limit`),
  and the global queue (`MAX_QUEUE_DEPTH`, 503 `busy` with `Retry-After`).
- **Rate limits** (`api/ratelimit.py`): in-process token buckets keyed by (limit,
  key). Examples: 20 logins per 15 minutes per address, 8 failed logins per 15
  minutes per account, 5 sign-ups per hour per address, 3 reset emails per hour
  per address, 120 requests a minute per API key, 600 per signed-in user.
  Over the limit: 429 `rate_limited` with `Retry-After`.

### 4.4 Errors (`api/errors.py`)

Every API error has one shape, which automations branch on:

```json
{"error_code": "too_many_jobs", "detail": "...", "retryable": true, "retry_after_s": 60}
```

`Retry-After` accompanies `retry_after_s`. Validation errors list the fields in
`detail`. HTML requests (a browser asking for a page) get the site's error page
instead (`render_error_page` in `api/routers/pages.py`).

## 5. Accounts, sessions and email

### 5.1 Data model (`db/models.py`, migrations in `db/migrations/versions/`)

| Table | Holds |
|---|---|
| `users` | email (normalised), argon2id password hash, name, role (`user`/`admin`), active flag, `email_verified_at`, per-user `webhook_secret`, `notify_on_done`, `tokens_valid_after` (access tokens issued before it are rejected) |
| `email_tokens` | verification and reset tokens: only a SHA-256 hash is stored, with expiry and `used_at` |
| `refresh_tokens` | one row per refresh token: hash, family id, `replaced_at`, `revoked_at`, expiry, user agent and address (the "signed-in browsers" list) |
| `api_keys` | prefix, SHA-256 of the secret, name, scopes (JSON), last used, expiry, `revoked_at` |
| `jobs_index` | job id, owner, status, title, source kind, created via, timestamps, duration, bundle size: a mirror of job.json for listing |
| `webhook_deliveries` | per event: URL, payload, attempt count, next attempt, status code, error, delivered time |

`db/session.py` keeps one engine per database URL. SQLite runs in WAL mode with
foreign keys on and a busy timeout, so the worker thread's writes never block
readers. `DB_SCHEMA_MODE` chooses `migrate` (Alembic, the default), `create_all`
(tests) or `none`. Timestamps are stored as UTC (`db/types.py`, `UTCDateTime`,
which also refuses naive datetimes).

Migrations: `0001` created everything; `0002` removed the Zoom leftovers (the
user's Zoom host email column and the `zoom:read` key scope). On SQLite, 0002 drops
the column in place instead of rebuilding the table, because the app migrates with
foreign keys on and a rebuild of `users` would cascade-delete every session and key.

### 5.2 Sign-up, login and sessions

- **Sign-up** (`services/users.signup`): the email is normalised and validated,
  the password checked (at least 10 characters, not a common or site-themed
  password, `services/passwords.py`), hashed with argon2id, and a verification
  email sent. `ALLOWED_SIGNUP_DOMAINS` can restrict who may sign up;
  `SIGNUP_ENABLED=false` closes it. Addresses in `ADMIN_EMAILS` become admins once
  verified.
- **Login** returns a 15-minute JWT access token (HS256, `JWT_SECRET`,
  `services/tokens.py`, with `sub`, `type=access`, `iat`, `exp`) and an opaque
  30-day refresh token. Browsers get both as `HttpOnly`, `SameSite=Lax` cookies
  (`hc_access`, `hc_refresh`), `Secure` when the site is https. Scripts can use
  the access token as a bearer token.
- **Refresh rotation** (`services/sessions.rotate`): every refresh replaces the
  refresh token. Presenting an already-replaced token more than 20 seconds after
  its replacement (the grace absorbs two tabs refreshing at once) is treated as
  theft: the whole family, every token descending from that login, is revoked.
- **Pages top up sessions.** A page loaded with only a valid refresh cookie gets
  fresh cookies in its response (`_top_up_session`), so its API calls don't all
  start with a 401. The site's fetch wrapper also refreshes once on a 401, with a
  single shared refresh for parallel calls.
- **Password change or reset** signs out every other browser (revokes refresh
  families) and moves `tokens_valid_after`, so old access tokens die too.
- **Verification and reset links** carry a random token; only its hash is stored.
  Reset links last an hour and work once. Opening an old verification link of an
  already verified account shows success (mail scanners open links first).
- **Account deletion** removes keys, sessions, the user and every job with its files.

### 5.3 Email (`services/email.py`, templates in `web/templates/email/`)

- Every message has an HTML and a text twin rendered with Jinja2 (HTML
  autoescaped, `StrictUndefined` so a missing variable fails loudly).
- Backends (`EMAIL_BACKEND`): `console` writes the message to the log and delivers
  nothing (the default, for development); `memory` collects messages for tests;
  `smtp` (STARTTLS, SSL or plain); `resend` (Resend's HTTP API).
- Real backends send from a small thread pool with retries, so a slow mail
  server never holds up a request.
- While the backend is `console`, the app logs a startup warning, and the pages
  say the link went to the server log instead of promising an email.
  `python -m services.email --test ADDRESS` sends a test message;
  `python -m db.cli verify-email ADDRESS` confirms an account by hand.
- Messages: verification, welcome, password reset, password changed, job
  finished (`services/notifications.py`; users can turn job emails off).
- `APP_ENV=prod` refuses to start with `console` or `memory`.

### 5.4 API keys (`services/api_keys.py`)

- Format `hc_live_<8-character prefix>_<32-character secret>`. The prefix finds
  the row; the SHA-256 of the whole key is compared in constant time. The full key
  is shown once, at creation.
- Keys belong to a user, carry scopes, can expire, be revoked (effective at once)
  or rotated (a new secret with the same name and scopes; a key left with no valid
  scope is refused rather than silently widened). At most `MAX_API_KEYS_PER_USER`.
- Creating and managing keys needs a signed-in person, not another key.

### 5.5 Webhooks (`services/webhooks.py`)

- A job created with `callback_url` gets one POST when it reaches `done`,
  `failed`, `decided` or `cancelled`: headers `X-HC-Event`, `X-HC-Delivery` (unique
  per delivery), `X-HC-Signature: t=<unix>,v1=<hex>`, where `v1` is
  HMAC-SHA256(user's webhook secret, `"<t>." + raw body`). The body carries the job
  with its links.
- **URL rules** (`validate_callback_url`): https only (http only with
  `WEBHOOK_ALLOW_HTTP` in development), no credentials in the URL, and the host
  must resolve to public addresses only (no loopback, private, link-local,
  multicast or reserved ranges, IPv4 or IPv6). The address is checked again at
  send time and redirects are not followed. The HTTP client resolves the name once
  more when it connects, so a DNS answer that changes within that instant is the
  remaining gap.
- **Delivery:** rows in `webhook_deliveries`, sent by one background thread.
  Anything but a 2xx within `WEBHOOK_TIMEOUT_S` is retried after 1, 5 and 30
  minutes, then given up. Each job page lists its deliveries; `/health` reports how
  many are pending. `POST /api/v1/webhooks/test` sends a signed `ping`.

### 5.6 The jobs index (`services/jobs_index.py`)

`JobStore` calls hooks on every change and delete; the index row is updated only
when something it mirrors changed (a fingerprint of status, title, error, sizes,
owner), so progress ticks don't write the database. At startup, job folders the
database doesn't know yet (older jobs) are indexed.

## 6. The website

- **Pages** (`api/routers/pages.py`): public (`/`, `/about`, `/pricing`, `/docs`,
  `/privacy`, `/terms`), auth (`/login`, `/signup`, `/forgot`, `/reset`, `/verify`),
  app (`/app`, `/app/jobs/{id}`, `/app/keys`, `/app/account`, `/app/admin`), plus
  `robots.txt`, `sitemap.xml`, `favicon.ico` and HTML error pages.
- App pages redirect anonymous visitors to `/login?next=<path and query>`; `next`
  only accepts same-site paths. The admin page is a 404 for non-admins.
- Every template gets a context (`_context`): the current user, the CSP nonce,
  settings the pages show (app name, sign-up open, limits, whether email is
  delivered) and `asset()`, which appends a content hash to static URLs.
- `/docs` renders the job statuses, error codes and endpoint table from the code
  (`STATUS_DOCS`, `ERROR_DOCS`, the live OpenAPI schema), so the docs can't drift;
  an import-time check makes sure every job status is documented.
- **Browser code** (`web/static/js/`): `core.js` (the fetch wrapper with CSRF
  header and single-flight refresh, upload with progress, toasts, tabs, copy
  buttons, theme, dialogs, `window.HC`), one small script per app page. No build
  step, no framework. Motion (vendored) animates toasts only.
- **Design:** see DESIGN.md. One stylesheet, `web/static/css/site.css`.

## 7. A job's life

### 7.1 Submission (`api/main.py`)

| Route | Body | Runner |
|---|---|---|
| `POST /api/v1/jobs` | multipart: `file` (.mp4/.mkv/.mov), optional `transcript` (.vtt/.srt), `title`, `callback_url`, any option | `run_pipeline` |
| `POST /api/v1/jobs/youtube` | JSON: `url`, `options`, `callback_url` | `run_youtube_pipeline` |
| `POST /api/v1/jobs/{id}/render` | none | the job's own runner, reusing saved picks |

The upload route is a plain `def`, so FastAPI streams the multi-GB copy in its
threadpool instead of the event loop. Files go straight into
`storage/jobs/<id>/source.<ext>` (`ingest/store.py`). A failure while storing
deletes the half-made folder. `_enqueue` writes `job.json` and submits to the queue.

Job options (`core/models.py`, `JobOptions`): `decide_only`, `cut_silence`,
`transitions`, `highlights_target_s` (0-900), `shorts_count` (0-10),
`highlights_criteria`, `shorts_criteria`.

### 7.2 The queue and the worker (`api/queue.py`)

- One daemon thread, `job-worker`, runs jobs FIFO, one at a time, restarted if it
  dies. `submit` raises `QueueFull` at `MAX_QUEUE_DEPTH` (running plus waiting).
- For each job it attaches `job.log` (the worker thread's INFO+ lines), installs a
  heartbeat and starts the job's wall clock (`JOB_MAX_WALL_S`, 3 hours).
- **Every `update(job)` is a checkpoint.** It saves `job.json` and, unless the job
  is finished, raises if the job was cancelled or ran out of time. The heartbeat
  (`core/proc.py`, a thread-local poll hook) does the same between slices of any
  long ffmpeg/ffprobe run, so a cancel stops an encode within about 30 seconds.
- **Cancel and delete:** a waiting job is removed from the queue; a running one is
  signalled and stops at its next checkpoint. `DELETE ?force=true` deletes the
  folder afterwards. A `.keep` file in a job folder protects it from deletion.
- **Stale:** a running job whose `updated_at` is older than `STALE_AFTER_S` (10
  minutes) is reported `stale` by the API.
- **Startup reconciliation** (`reconcile_on_startup`): every job that wasn't
  finished (the in-memory queue is gone) becomes `failed` / `interrupted`
  (retryable), its `tmp/` is wiped, and finished jobs older than
  `JOB_RETENTION_HOURS` are deleted (with their YouTube download).
- **`JobStore`** (`api/jobs.py`) caches `JobRecord`s and writes `job.json`
  atomically (temp file, then rename). The cached object is the same one the
  worker mutates. Old records written while Zoom ingest existed still load
  (`JobRecord._load_zoom_era_records`).

### 7.3 Statuses

`queued` → `downloading` (YouTube) → `transcribing` → `deciding` →
`building_edl` → [`decided`, when `decide_only`] → `slicing` →
`rendering_highlights` → `assembling` → `rendering_removed` →
`rendering_shorts` → `reporting` → `bundling` → `done`.
Ends: `done`, `failed`, `cancelled`, `decided`, `skipped_desync` (audio and video
lengths differ by more than a second). Progress (`progress_current/total`) is set
where work is countable: MB downloaded, sentences judged, render steps, shorts.

### 7.4 Pre-flight (`pipeline.run_pipeline`)

Each run first clears warnings, errors and every output pointer, so a failed
re-render never serves the previous files. Then:
1. `check_disk`: at least `MIN_FREE_DISK_BYTES` free (6 GiB), else
   `insufficient_disk` (retryable).
2. `validate_video` (`ingest/validate.py`): one video and one audio stream whose
   durations (trying stream duration, `duration_ts × time_base`, the Matroska tag,
   then the container) differ by at most 1.0 s; otherwise `skipped_desync`.
3. `probe_media` (`slice/profile.py`): frame rate 1-120 fps (exact `Fraction`),
   size, colour tags, audio layout; otherwise `source_unsupported`.

## 8. Ingest and transcripts

### 8.1 YouTube (`ingest/youtube.py`)

- `run_youtube_pipeline` downloads only when the source is missing (first run, or
  a re-render after the file was deleted), sets the job title from the video's
  title, then runs the pipeline.
- `download_youtube` uses yt-dlp with shared options (`ydl_base_opts`, also used
  for captions): single video (`noplaylist`), H.264 ≤ 720p preferred (everything
  is re-encoded anyway), output `storage/videos/<random hex>.mp4`, every installed
  JavaScript runtime enabled (Deno or Node; YouTube extraction needs one), yt-dlp
  warnings sent to our log.
- The download lives outside the job folder; `delete_job_files` removes it with the
  job unless another job points at the same file.

### 8.2 Which transcript (`pipeline._transcribe`)

The first that works:
1. The job's saved `transcript.json` + `segments.json` (re-renders never
   re-transcribe).
2. An uploaded .vtt/.srt (`transcript_source = uploaded_transcript`).
3. YouTube captions (`fetch_youtube_transcript`, `youtube_captions`): an original
   English track, creator captions before automatic ones, json3 before VTT,
   machine translations and HLS tracks rejected, fetched through yt-dlp's own
   downloader (browser impersonation), one retry on the metadata call.
4. WhisperX speech recognition with speaker diarization (`asr`), only when the
   server has it and `REQUIRE_NATIVE_TRANSCRIPT` is false. Otherwise the job fails
   `transcript_not_ready` (not retryable: captions that aren't there won't appear).

### 8.3 From captions to sentences (`transcribe/native.py`)

- **Parsing** is forgiving: VTT and SRT, BOMs, CRLF, missing blank lines, optional
  hours, 1-3 digit fractions, cue settings, `<v Name>` voice tags, entities, and
  cues past the end of the recording are dropped or clamped.
- **Speakers:** a `Name: text` prefix (the shape many meeting tools export, Zoom
  included) counts as a speaker when it recurs, or when it looks like a display
  name and isn't a stop word ("Note:", "Thanks everyone:"). Otherwise the line
  keeps its words under the placeholder `SPEAKER`. YouTube captions have no speakers.
- **Sentences:** cues are display units, not sentences. `_group_into_sentences`
  splits at `.!?`, always at a speaker change, and caps at 40 words or 20 s.
  `_interpolate` gives sentences cut from one cue their own proportional times;
  identical times once made the edit list merge a whole meeting into one block.
- For native sources each sentence also becomes one "word" (`segments_to_words`),
  so cuts snap to sentence edges.

### 8.4 Repairs

- `flag_overlaps` marks adjacent words from different speakers that overlap
  (meaningful on WhisperX output; shown to the model as "(overlap)").
- `repair_fragments` (`decide/repair.py`), after the AI pass: a short removed
  fragment that completes or begins a kept sentence by the same speaker, within a
  second, is put back. It fixed audibly half-cut sentences.

## 9. The AI pass (`decide/`)

Three kinds of call per job; everything around them is deterministic Python.

| Call | Stage | The model returns |
|---|---|---|
| Judge | `deciding`, one call per 60 sentences | per sentence: keep/cut, a removal reason code, a 0-10 learning score, a highlight category |
| Re-rank | `deciding`, one call for up to 60 candidate moments | comparative score, category, a stand-alone clip window, short-worthy, title, one-line hook |
| Chapters | `reporting`, after rendering | the line where each chapter starts, and its title |

- **Clients:** OpenAI's Responses API over plain httpx (`store: false`, reasoning
  effort `none` by default, budget model), or Gemini (`LLM_PROVIDER`). A lazy
  caller builds the client only when a call is really needed.
- **Compact line format, not JSON:** `312 r 0 fill -` / `313 k 6 - arch`. About a
  quarter of JSON's output tokens. Answers are validated line by line against the
  exact set of indices asked; context lines are shown but not judged.
- **Retries:** an invalid answer is retried once, then the chunk is split; a
  truncated answer is split at once; a chunk where every kept line scored 0 is
  re-scored (at most 4 per run). Rate limits, 5xx and network errors back off;
  running out of credit fails fast (`llm_quota_exhausted`).
- **Deadlines:** judging plus re-rank must finish in `DECIDE_MAX_WALL_S` (30
  min); chapters in `CHAPTERS_MAX_WALL_S` (5 min).
- **Saved answers:** `decisions.json` is written after every chunk with a
  fingerprint of the prompt, model, chunk settings and every sentence; a rerun
  with the same fingerprint makes no calls. `rerank.json` likewise. Changing the
  prompts, criteria or model re-judges (a paid call).
- **Cost:** about 100k input + 15k output tokens for a 98-minute meeting,
  about $0.02 with the default model. The model only ever sees transcript text.
- **Criteria:** `HIGHLIGHTS_CRITERIA` and `SHORTS_CRITERIA` (or per-job overrides)
  are inserted into the prompts. The default is learning-first: funny moments get
  no head start.

### 9.1 From scores to picks (`edl/highlights.py`)

- `candidate_moments`: sentences scoring 3 or more are grouped into moments
  (joined across pauses up to 10 s, or 30 s bridging two lines); the best 60 go to
  the re-rank.
- `calibrate_by_rank`: the re-rank orders well, but its absolute scores swing
  between runs, so scores become ranks (10 for the best, down to 0).
- `select_highlights`: fills a budget of `min(HIGHLIGHTS_TARGET_S, 30% of the
  source)` with the best moments of at least 12 s and at most 60 s, trimmed around
  their peak, never starting or ending on removed junk, capped per 10-minute
  stretch (diversity) and at 34% funny. Under 60 s of material means no reel.
- `select_shorts`: short-worthy moments, learning categories first, windows of
  20-60 s, no overlaps. A short may include material the cleanup removed.
- `selection.json` records every moment and what used it; `GET /jobs/{id}/selection`
  serves it, and the job page shows it as "What the AI picked".

## 10. The edit decision list (`edl/builder.py`)

- **Only keeps are unioned;** removes are never subtracted. Keeps are coalesced,
  then snapped outward to word (for native sources: sentence) edges.
- **Silences** (`slice/silence.py`): 50 ms RMS windows at 16 kHz, an adaptive
  threshold between room tone and speech, runs of at least 1 s. Each pause inside
  kept speech shrinks to about a 0.55 s beat (0.30 s after, 0.25 s before speech).
  A noisy room with too little range cuts nothing and says so in a warning.
- **The frame grid:** every boundary becomes an integer source frame. Gaps shorter
  than `max(0.3 s, d+1 frames)` are merged back (never shown as a cut). Keeps
  shorter than `max(0.5 s, 2d frames)` are widened into the gaps, merged across a
  gap under 1 s, or dropped (never merged into distant content). `d` is the even
  dissolve length nearest 0.5 s (12 frames at 25 fps, 16 at 30).
- Nothing kept means the full video with a warning (the reel gets nothing instead).
- `complement` gives the removed ranges: under 1 s they are listed in the
  transcript and report only; 1 s or more also go into `removed.mp4`. Each gets a
  reason (the removal category that covers most of it, "silence", or "trimmed at a
  cut"). Pure silences stay out of `removed.mp4`.
- Files: `edl.json`, `edl_removed.json`, `edl_highlights.json`, `silences.json`.
- `decide_only` stops here with status `decided`. "Render now" re-runs the
  pipeline, which reuses the saved transcript, decisions, re-rank and silences and
  recomputes the edit lists with the settings in force.

## 11. Rendering (`outputs.py`, `slice/`)

### 11.1 The rules that make it frame-exact

- **One pinned encoder profile** for every frame of every deliverable (`libx264
  veryfast crf 24`, High profile, yuv420p, constant frame rate at the source's exact
  rate; AAC 128k 48 kHz stereo). Pieces are joined with `concat -c copy`, which
  only works (and otherwise silently corrupts) when every piece matches, so every
  join is asserted first: identical SPS/PPS hash, fps, size and pixel format.
- **Stream copy of the source was retired.** A copy starts at the previous
  keyframe: on the 98-minute reference job it brought back 78 s of removed
  content. Every piece is now re-encoded from an exact frame range.
- **The exact-frame input chain:** each input seeks a quarter frame early, then
  `fps=F:round=down`, scale, `tpad` (never a frame short), `trim=end_frame=N`. A
  source frame therefore lands in exactly one output slot.
- **Dissolves centred on the cut:** each kept range is extended `d/2` frames into
  the removed material on both sides and joined with an xfade of `d` frames, so the
  output length is exactly the sum of the kept frames.
- **Batches:** at most 20 inputs and 600 s of content per ffmpeg graph (memory,
  and so every command fits its timeout). Where batches meet, a separate "seam"
  part renders that one dissolve.
- **Audio from one FLAC:** the source audio is decoded once to 48 kHz FLAC with
  gaps filled, then cut by sample (cumulative, so 29.97 fps never drifts) with 20
  ms micro-fades at every cut, and encoded to AAC once per deliverable.
- **Asserts after every step:** frame counts of every part, the joined video, and
  the final audio duration within 2048 samples. A mismatch is
  `render_assert_failed`, never a silently wrong file.
- **Timeouts** per ffmpeg command scale with the content (3 s per content second,
  between 2 and 30 minutes); graphs are passed as files (Windows' command-line
  limit, and ffmpeg 9 removed `-filter_complex_script`).

### 11.2 What gets rendered, mandatory first

1. The cleaned meeting track (**mandatory**).
2. The highlights reel track and the title card (optional).
3. `final.mp4` = reel + card + cleaned meeting (**mandatory**; rebuilt without the
   card if the card doesn't match), plus optional `highlights.mp4` and
   `cleaned.mp4`. Track files are deleted right after, to save disk.
4. `removed.mp4`: hard cuts, each clip labelled "Removed MM:SS–MM:SS · reason" in
   source time (optional).
5. Shorts: 1080×1920, the frame over a blurred copy of itself, the title at the
   top and burned-in captions (libass), plus `.srt` files (each optional).

An optional output that fails becomes a warning and a `failed`/`skipped` entry in
the manifest; the job still finishes. Cancellation, the job's wall clock and a full
disk are never softened.

## 12. Delivery (`deliver.py`, `report/`, `bundle/`)

- **Transcript remap:** each word lands where its midpoint lands in the output.
  A sentence whose midpoint fell in a shortened pause still counts as said.
- **Files:** `transcript_clean.txt/.json` on final.mp4's timeline (with
  "HIGHLIGHTS REEL" and "FULL MEETING" sections), `transcript_removed.txt/.json` on
  the source timeline with reasons and `removed.mp4` positions.
- **Chapters:** the model chapters the cleaned transcript; the result is shifted by
  the reel's measured length, "00:00 Highlights" is added, and YouTube's rules are
  enforced (at least 3, first at 0, 10 s apart, 60-character titles). An invalid
  list is dropped rather than published.
- **report.pdf** (reportlab, the same font as the videos): facts, what's in the
  bundle, what was removed (chart and table by reason), highlights, shorts,
  chapters, removed parts in detail, warnings, AI usage and time per stage.
- **manifest.json:** every file with size, SHA-256, duration, status and reason,
  plus warnings, timings and AI usage.
- **bundle.zip:** every `ok` file plus the manifest; videos stored, text deflated;
  written to a temp name then renamed. A disk check (the files' size plus 100 MB)
  runs first.
- **Cleanup:** `DELETE_SOURCE_WHEN_DONE` removes the uploaded source;
  `SERVE_INDIVIDUAL_ARTIFACTS=false` keeps only the zip and manifest (single files
  then answer 410).
- **Notifications:** the webhook (if any) and the "job finished" email are queued
  when the job reaches a final status.

### 12.1 A job folder

| File | Written by | Holds |
|---|---|---|
| `job.json`, `job.log` | JobStore, the worker | the record; the job's log |
| `source.*` | upload | the recording (YouTube downloads live in `storage/videos/`) |
| `transcript.json`, `segments.json` | transcribing | words and sentences on the source timeline |
| `decisions.json`, `rerank.json` | deciding | the model's answers with fingerprints |
| `silences.json`, `edl*.json`, `selection.json` | building_edl | pauses, edit lists, picks |
| `tmp/` | rendering | FLAC, parts, graphs; deleted as soon as possible |
| `final.mp4`, `highlights.mp4`, `removed.mp4`, `shorts/` | rendering | deliverables |
| `transcript_*`, `chapters.txt`, `report.pdf`, `manifest.json`, `bundle.zip` | delivery | deliverables |
| `render_manifest.json`, `clean_transcript.json`, `chapters.json` | delivery | internal timelines |

## 13. Error codes

| Code | Meaning | Retryable |
|---|---|---|
| `busy`, `too_many_jobs`, `rate_limited` | queue full, per-user limit, request limit | yes |
| `plan_limit` | monthly jobs or recording length | no |
| `unauthorized`, `insufficient_scope`, `csrf`, `session_required`, `email_not_verified` | access | no |
| `validation_error`, `not_found`, `webhook_url_invalid` | request problems | no |
| `transcript_not_ready` | no captions or transcript and no local transcription | no |
| `source_unsupported` | unreadable source, no audio/video, odd frame rate | no |
| `insufficient_disk` | below `MIN_FREE_DISK_BYTES`, or disk full mid-job | yes |
| `llm_quota_exhausted`, `llm_error` | the AI provider | depends |
| `render_assert_failed` | a render didn't match the edit list (a bug) | no |
| `timeout` | a step or the job took too long | yes |
| `interrupted` | the server restarted during the job | yes |
| `cancelled` | cancelled by a user | no |
| `internal` | anything else | no |

`core/errors.py` (`classify`) maps exceptions to codes; `PipelineError` carries its
own code, `retryable` and `retry_after_s`.

## 14. Configuration

All settings are environment variables (or `.env`), read by `core/config.py`
(`Settings`, cached; a change needs a restart). `.env.example` documents every one.
The main groups:

- **Site and accounts:** `APP_ENV`, `APP_BASE_URL`, `JWT_SECRET`, token lifetimes,
  `COOKIE_SECURE`, `TRUST_PROXY`, `SIGNUP_ENABLED`, `ALLOWED_SIGNUP_DOMAINS`,
  `ADMIN_EMAILS`, `DATABASE_URL`, `DB_SCHEMA_MODE`.
- **Email:** `EMAIL_BACKEND`, `EMAIL_FROM`, `SMTP_*`, `RESEND_API_KEY`.
- **Limits:** `MAX_QUEUE_DEPTH`, `MAX_QUEUED_PER_USER`, `MAX_API_KEYS_PER_USER`,
  `PLAN_JOBS_PER_MONTH`, `PLAN_MAX_MINUTES`, `RATE_LIMIT_ENABLED`.
- **Webhooks:** `WEBHOOKS_ENABLED`, `WEBHOOK_TIMEOUT_S`, `WEBHOOK_ALLOW_HTTP`,
  `WEBHOOK_ALLOW_PRIVATE` (development only).
- **AI:** `LLM_PROVIDER`, `OPENAI_*`, `GEMINI_*`, highlight and shorts criteria and
  targets, chapters.
- **Pipeline:** transcription (`REQUIRE_NATIVE_TRANSCRIPT`, `WHISPERX_*`,
  `HF_TOKEN`), silence cutting, transitions, render batching and timeouts, outputs
  (`SERVE_INDIVIDUAL_ARTIFACTS`, `DELETE_SOURCE_WHEN_DONE`), disk and retention.
- **Operations:** `LOG_JSON`, `LEGACY_API_ENABLED`, `HC_API_TOKEN`, `DEV_OPEN_API`.

`settings_problems` lists what is unsafe for production; `APP_ENV=prod` refuses to
start with any of them: a short `JWT_SECRET`, a non-https `APP_BASE_URL`, an email
backend that delivers nothing, a missing `EMAIL_FROM`.

## 15. Operations

- **Health:** `GET /health` (and `/api/v1/health`) reports version, ffmpeg, free
  disk, queue, database, email backend, pending webhooks and uptime.
- **Logs:** plain lines locally, JSON lines with `LOG_JSON=true`; every line of a
  request carries its `X-Request-ID`. Each job also has its own `job.log`.
- **Admin CLI** (`python -m db.cli`): `upgrade`, `current`, `stats`,
  `create-admin`, `promote`, `demote`, `activate`, `deactivate`, `set-password`,
  `verify-email`, `index-jobs`, `backup DEST`.
- **Deployment:** Docker image + Caddy, or systemd; backups; CI. See DEPLOY.md.

## 16. Tests

`pytest -q` runs the whole suite (about 570 tests) in under two minutes, with no
network: the LLM, YouTube and email are faked, ffmpeg runs for real on small
generated clips. `tests/conftest.py` pins settings so the developer's `.env` can't
change results and points storage at a temporary folder per test. The `product`
fixture turns on real authentication; `tests/product_helpers.py` signs users up,
verifies them and creates keys. Page tests render every template and check the CSP
nonce; `test_zoom_removal.py` proves old records and the migration keep data.
`ruff check .` (pinned version), bandit and pip-audit run in CI.

## 17. Design decisions worth knowing

- **The transcript is the edit.** Decisions are made on text; the renderer only
  obeys the edit list. The model never sees video or audio.
- **Integer frames everywhere.** Any fractional boundary makes ffmpeg round per
  graph and the timeline drifts; v1 drifted 78 s on a 98-minute meeting.
- **Mandatory outputs first, optional ones soft.** A failed short or report never
  costs the user the main video.
- **Saved, fingerprinted AI answers.** A crash resumes chunk by chunk; a re-render
  makes no judging calls.
- **Security by construction:** credentials are resolved before the body is read;
  cookie requests need the CSRF header; keys, tokens and reset links are stored as
  hashes; refresh tokens rotate with theft detection; webhooks refuse private
  addresses; pages run under a nonce CSP.
- **One process** keeps the stack simple (no Redis, no broker). Scaling out means
  one app per storage folder behind a router, or moving the queue out.

## 18. Known limitations

- YouTube download failures are classified `internal` (not retryable), even when
  the cause was a passing network problem.
- The heartbeat only runs inside subprocesses: during a long WhisperX run or
  YouTube download the job can look `stale` for a while.
- "Render now" recomputes the edit lists with the settings in force at render time,
  and always makes the chapters call, so it isn't a byte-exact replay of what was
  reviewed.
- Shorts can end slightly under 20 s after their edges are pulled out of silences.
- Cancelling after bundling has started lets the job finish.
- Retention runs only at startup, not periodically.
- Legacy unprefixed routes and the operator key (`HC_API_TOKEN`) remain for older
  n8n flows until they move to `/api/v1` and per-user keys.
