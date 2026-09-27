# Highlight Cutter

Meeting recordings, cut to the parts worth watching.

Give it a YouTube link or upload a recording (.mp4, .mkv, .mov, optionally with
its .vtt/.srt transcript). It reads the transcript, has an AI editor mark every
sentence keep or cut and score what's worth learning from, cuts the video
frame-exact with ffmpeg, and hands back one zip:

| File | What it is |
|---|---|
| `final.mp4` | a highlights reel, a short title card, then the cleaned meeting |
| `highlights.mp4` | the reel on its own |
| `shorts/short_NN.mp4` + `.srt` | 1080×1920 clips with burned-in captions and a title |
| `removed.mp4` | every cut of a second or more, labelled with its time and reason |
| `report.pdf` | what was removed and why, highlights, shorts, chapters |
| `transcript_clean.*`, `transcript_removed.*` | what the final video says; what was cut |
| `chapters.txt` | YouTube chapters for `final.mp4` |
| `manifest.json` | every file's size, checksum, duration and status |

Around the pipeline sits a product: accounts with email verification, a website
and dashboard, API keys with scopes, signed webhooks, per-user limits and an
admin page, so n8n, Zapier, Make or any script can drive it.

**Documentation:** [ARCHITECTURE.md](ARCHITECTURE.md) explains how every part
works. [DEPLOY.md](DEPLOY.md) takes a fresh server to HTTPS. [DESIGN.md](DESIGN.md)
covers the website's design system. The running site's `/docs` page and
`/api/docs` document the API.

## Run it locally

Python 3.12, ffmpeg and ffprobe on `PATH`, and Node or Deno on `PATH` for YouTube
downloads.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; source .venv/bin/activate elsewhere
pip install -r requirements-dev-server.txt
cp .env.example .env              # then set OPENAI_API_KEY
uvicorn api.main:app --reload
```

Open http://127.0.0.1:8000, sign up, and cut a YouTube video. Locally the email
backend is `console`: the confirmation link is printed in the server's log (the
site says so), or confirm yourself with `python -m db.cli verify-email you@example.com`.
Make yourself an admin with `python -m db.cli promote you@example.com`.

**Local transcription (optional).** Recordings without captions or a transcript
file need WhisperX, which pulls in torch: `pip install -r requirements-dev.txt`
instead, and set `HF_TOKEN` to a Hugging Face read token from an account that
accepted the terms of pyannote's `speaker-diarization-3.1`, `segmentation-3.0`
and `speaker-diarization-community-1` models. Servers normally skip it and set
`REQUIRE_NATIVE_TRANSCRIPT=true`.

**The AI.** `OPENAI_API_KEY` with billing credit (the default model costs about
$0.02 per 98-minute meeting), or `LLM_PROVIDER=gemini` with `GEMINI_API_KEY`. It
only ever receives transcript text.

Every setting is documented in `.env.example`. Settings are read once per
process, so restart after editing `.env`.

## Use the API

Create a key on the **API keys** page, then:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs/youtube \
  -H "Authorization: Bearer $HC_API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID", "callback_url": "https://example.com/hook"}'
```

Poll `GET /api/v1/jobs/{job_id}` or wait for the signed webhook, then download
`GET /api/v1/jobs/{job_id}/bundle`. Uploads go to `POST /api/v1/jobs` as
multipart. Errors always carry `error_code`, `retryable` and `retry_after_s`.

## Deploy

```bash
sudo DOMAIN=cut.example.com REPO_URL=https://github.com/<you>/<repo>.git bash deploy/scripts/bootstrap_ec2.sh
```

Then fill in email and the AI key in `.env` and `docker compose up -d --build`.
The full walk-through, including email setup, backups and updates, is in
[DEPLOY.md](DEPLOY.md).

## Develop

```bash
pytest -q                 # about 570 tests, no network; ffmpeg runs for real on tiny clips
ruff check .              # pinned in requirements-dev-tools.txt
bandit -q -c bandit.yaml -r api services db core ingest decide edl slice transcribe bundle report
```

CI (`.github/workflows/ci.yml`) runs these, pip-audit, and builds and smoke-tests
the Docker image.

Regression tools for the 98-minute reference meeting (job folder `7b93...`,
protected by a `.keep` file): `tools/regress_7b93.py` re-renders and checks it
frame by frame; `tools/decide_7b93.py` re-runs the AI pass; `tools/deliver_7b93.py`
builds the whole zip from saved decisions with no AI calls.

## Project history

- v1: the pipeline and a single-page tool.
- v2 (2026-09): frame-exact rendering, highlights, shorts, the zip, silence
  cutting, OpenAI. Old hand-off notes: [docs/history/](docs/history/).
- Product layer (2026-09-27, `prod` branch): accounts, API keys, webhooks, the
  website, deployment. The plan is in [plan.MD](plan.MD). Zoom ingest was removed
  from `prod`: jobs come from YouTube links and uploads only (a transcript exported
  from Zoom can still be attached to an upload).
