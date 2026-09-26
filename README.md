# Highlight Cutter

Given a Zoom cloud recording, a YouTube link or an uploaded recording,
produces one zip per meeting: the highlights reel followed by the cleaned
meeting, vertical shorts, a video of everything removed, a PDF report,
transcripts and YouTube chapters (see "What a finished job delivers").
The platform's own transcript (Zoom's VTT, YouTube's captions) is reused when
there is one, else WhisperX transcribes; an LLM pass (OpenAI `gpt-6-luna` by
default) judges every sentence; ffmpeg does the cutting. Includes a small web
UI.

## Pipeline

```
Zoom recording / YouTube URL / uploaded file -> [ingest]
      -> [transcribe: reuse Zoom VTT / YouTube captions / uploaded .vtt, else WhisperX]
      -> [decide: LLM keep/remove + highlight score per sentence, re-rank, chapters]
      -> [edl: snap to sentences and the source frame grid -> EDL + removed ranges]
      -> [deliver: final.mp4, removed.mp4, shorts, report, transcripts, bundle.zip]
```

Module layout matches the spec's suggested repo structure: `ingest/`,
`transcribe/`, `decide/`, `edl/`, `slice/`, `api/`, plus `core/` for shared
config/models, `pipeline.py` tying the stages together, and `web/` for the UI.

### Reusing an existing transcript

Running WhisperX is the slowest, most compute-heavy stage in the pipeline. If
a platform-provided transcript is already available, the pipeline uses it
instead and skips WhisperX (ASR + alignment + diarization) entirely:

- **Zoom** — automatic: a Zoom job downloads the recording's own audio
  transcript (`Name: text` cues) next to the MP4 through the Zoom API.
- **YouTube** — automatic, no setup needed. The pipeline checks the video for
  its own captions (creator-uploaded preferred over auto-generated, original
  English only — never a machine translation) via `transcribe/native.py`,
  fetched through `yt-dlp`. If a usable English track exists, it's parsed and
  used directly; otherwise the pipeline transcribes the video itself.
- **Direct uploads** — attach a transcript when creating the job:
  `POST /jobs` accepts an optional `transcript` file field (`.vtt` or `.srt`
  — this is exactly what Zoom's cloud recordings export as
  `audio_transcript.vtt`). If provided and it parses, it's used instead of
  WhisperX. The web UI's upload panel has a matching optional drop zone.

Either way, if no transcript is available — or it fails to parse — the
pipeline transparently falls back to self-hosted WhisperX transcription. A
job's `transcript_source` field (`asr` | `youtube_captions` |
`uploaded_transcript` | `zoom_transcript`) reports which path was actually
used; the web UI shows this once a job passes the transcribing stage.
`REQUIRE_NATIVE_TRANSCRIPT=true` (the EC2 setting) never runs WhisperX: a job
without a platform transcript fails `transcript_not_ready` instead.

**Caveat for crosstalk/"Light cleanup" mode:** platform transcripts are a
single serialized caption stream — by the time captions exist, simultaneous
speech has already been collapsed into whichever speaker was picked up, so
there's no overlap information left to detect. Crosstalk mode still runs
against a native transcript, but it will typically find no overlaps and the
EDL builder's safe-fallback (section 3.4) keeps the full video unchanged. Use
a direct WhisperX transcription (no attached transcript) for real crosstalk
removal; native transcripts are best suited to highlights mode.

## Setup

WhisperX's dependency chain (torch, faster-whisper, pyannote-audio) is not
yet published for very new Python releases — use **Python 3.10–3.12** for the
full install. A separate, lighter venv without WhisperX (just FastAPI/tests)
works fine on newer Python if you only need to run the test suite.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements-dev.txt
cp .env.example .env
```

Fill in `.env`:

- `HF_TOKEN` — a Hugging Face read token, from an account that has accepted
  the terms on all three gated model pages (WhisperX's diarization pipeline
  chains through all of them — accepting only the first isn't enough):
  - https://huggingface.co/pyannote/speaker-diarization-3.1
  - https://huggingface.co/pyannote/segmentation-3.0
  - https://huggingface.co/pyannote/speaker-diarization-community-1 (pulled
    in internally by 3.1's own pipeline config for PLDA weights)

  WhisperX itself runs self-hosted and free; this token is only for
  downloading the gated model weights.
- `OPENAI_API_KEY` — the AI pass (keep/remove, highlight scores, re-rank,
  chapters). `OPENAI_MODEL` defaults to `gpt-6-luna`, OpenAI's budget model
  ($0.10 / 1M input, $0.50 / 1M output tokens: a 98-minute meeting is about
  100k in + 15k out ≈ $0.02), with `OPENAI_REASONING_EFFORT=none`. The account
  needs billing credit — a key without it fails the job with
  `llm_quota_exhausted`. Set `LLM_PROVIDER=gemini` (plus `GEMINI_API_KEY`,
  `GEMINI_MODEL`) to use Gemini instead.
- `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` — a Zoom
  Server-to-Server OAuth app (Zoom Marketplace → Develop → Build app). It
  must be **activated** and have the scopes
  `cloud_recording:read:list_user_recordings:admin`,
  `cloud_recording:read:list_recording_files:admin` and
  `cloud_recording:read:recording:admin`. `ZOOM_HOST_EMAIL` is whose cloud
  recordings the UI lists by default. Restart the server after editing
  `.env` (settings are read once per process).
- YouTube downloads use `yt-dlp` with its `default` and `curl-cffi` extras and
  a JavaScript runtime on `PATH` (`node` or `deno`) — without one YouTube
  formats can go missing.
- ffmpeg/ffprobe must be on `PATH` (or set `FFMPEG_BIN`/`FFPROBE_BIN` to
  absolute paths — useful if you just installed ffmpeg and the running
  process's PATH hasn't picked it up yet; `core.config` also injects that
  directory onto `PATH` for the current process so libraries like WhisperX
  that shell out to a bare `ffmpeg` still find it).

## Running

```bash
uvicorn api.main:app --reload
```

Open `http://localhost:8000/app` for the web UI (paste a YouTube URL, pick a
Zoom recording, or drop a file; watch it process; play/download the results).
Or drive it directly:

- `GET /zoom/status` — whether Zoom credentials are set and the default host
  email (never the credentials).
- `GET /zoom/recordings?host=&from=&to=` — a host's cloud recordings
  (default `ZOOM_HOST_EMAIL`, the last 30 days) with topic, duration, parts
  and whether the transcript is ready.
- `POST /jobs/zoom` — JSON `{"meeting_uuid": "...", "options": {...}}`.
  Answers `425` (`recording_not_ready` / `transcript_not_ready`, with
  `retry_after_s`) while Zoom is still processing; the same meeting with the
  same options returns the existing job with `"deduplicated": true`; Zoom
  errors come back as `zoom_auth` (credentials / app not activated),
  `zoom_not_found` or `zoom_unavailable`. The job downloads the MP4 and
  Zoom's transcript (multi-part recordings are joined) and runs the pipeline.
- `POST /jobs/youtube` — JSON body `{"url": "...", "options": {...}}`,
  downloads via yt-dlp (H.264 up to 720p when available) then runs the same
  pipeline; the video's title goes on the title card and report.
- `POST /jobs` — hidden multipart upload for ops/regression (`file`, optional
  `transcript` `.vtt`/`.srt` to skip WhisperX, `title`, and the same options
  as form fields).
- `GET /jobs/{job_id}` — status/progress/paths.
- `GET /jobs/{job_id}/video` — the trimmed output (once `status == done`).
- `GET /jobs/{job_id}/edl` — the Edit Decision List used to produce it.
- `GET /jobs/{job_id}/transcript?clean=true` — transcript remapped onto the
  trimmed timeline (`clean=false` for the original, untrimmed transcript).

n8n (or any orchestrator) calls this API over HTTP per section 6 of the spec
— it should not run ffmpeg itself.

### v2 service behaviour (M0/M1)

- **One job at a time.** Jobs run on a single worker thread. While one is
  queued or running, new `POST /jobs*` calls get `503` with
  `{"error_code": "busy", "retryable": true, "retry_after_s": 60}` and a
  `Retry-After` header (`MAX_QUEUE_DEPTH`, default 1).
- **API key.** Set `HC_API_TOKEN` in `.env` and every route except `/health`
  and the UI pages requires `X-API-Key: <token>` (the web UI prompts once and
  keeps it in the browser). Empty = open, for local development only.
- `GET /health` — no auth: version, ffmpeg version, free disk, busy flag,
  running job id, queue depth.
- `GET /jobs` — job history, newest first. `GET /jobs/{id}/log` — that job's
  own log. `GET /jobs/{id}/removed` — the removed ranges (the EDL's exact
  complement, each tagged `video` or `transcript_only`).
- `DELETE /jobs/{id}` — queued or finished: wiped. Running: `409` unless
  `?force=true`, which cancels it at the next checkpoint and wipes it. A job
  folder containing a `.keep` file cannot be deleted.
- Failed jobs carry `error_code` (e.g. `source_unsupported`, `llm_error`,
  `render_assert_failed`, `timeout`, `interrupted`, `cancelled`,
  `insufficient_disk`) plus `retryable`, and `error` includes the tail of
  ffmpeg's stderr and the command. Jobs left unfinished by a restart are
  marked `failed/interrupted` at startup. A running job whose heartbeat stops
  for `STALE_AFTER_S` shows `stale: true`.
- **Rendering.** Every kept range is re-encoded once with one pinned profile
  (libx264 veryfast crf 24, AAC 128k 48 kHz) and joined with ~0.5 s
  dissolves centred on each cut. All cut points are whole source frames, so
  the output has exactly the frames the EDL keeps and the clean transcript
  maps with `out = t - range_start + range_offset`. Audio is cut
  sample-exactly from a normalised FLAC with a hard cut (20 ms micro-fades)
  at the middle of each dissolve. `TRANSITIONS_ENABLED=false` gives hard cuts.
  Every piece is checked (frame count, identical encoder parameters) before
  it is joined; a mismatch fails the job with `render_assert_failed` rather
  than shipping a silently broken file. `tools/regress_7b93.py` re-renders
  the 98-minute regression meeting and checks it frame by frame.
- **Silences.** Caption timings can't show pauses (a YouTube or Zoom caption
  stays on screen into the next line, so a kept sentence used to keep its
  dead air), so the source audio itself is measured: 50 ms windows of 16 kHz
  mono, streamed (a few seconds per hour of audio), cached per job in
  `silences.json`. What counts as silence adapts to each recording — room
  tone vs speech level over the part the transcript covers, never within
  18 dB of loud speech or 10 dB of typical speech, so a quiet talker is not
  cut (a room too noisy to tell them apart gets no silence cuts and a job
  warning). Every stretch quieter than that for ≥ `SILENCE_MIN_S` (1 s) is
  taken out of the cleaned meeting and the highlights reel, leaving 0.30 s
  after speech and 0.25 s before it (a natural ~0.55 s beat) where that
  speech stays in; a short only loses the silence at its start and end.
  These cuts are listed in `transcript_removed` and `report.pdf` as "silence
  (no one speaking)" but not put in `removed.mp4` (nothing to see or hear); a
  pause cut next to a removed sentence is part of that sentence's cut and
  label. Off for every job:
  `SILENCE_CUT_ENABLED=false`; per job: the "Cut silences" box
  (`options.cut_silence`, or the `cut_silence` form field for uploads).

### What a finished job delivers (M4)

One `bundle.zip` per meeting (`GET /jobs/{id}/bundle`, resumable), with:

| File | What it is |
|---|---|
| `final.mp4` | the highlights reel (mostly things worth learning — at most a third funny), a 2 s "topic / Full meeting" card, then the cleaned meeting |
| `highlights.mp4` | the reel on its own (only when the meeting has ≥ 60 s of highlight material) |
| `shorts/short_NN.mp4` + `.srt`, `shorts/shorts.json` | 1080×1920 shorts with burned-in captions and the moment's title — self-contained moments people can learn from first (concepts, how things work, new architectures/features, insights), a genuinely funny one only to fill a slot |
| `removed.mp4` | every cut of 1 s or more (except pure silences), each labelled "Removed 01:10–02:30 · reason" (original-recording time) |
| `report.pdf` | what was removed, when and why (minutes by reason, every cut with its text), highlights, shorts, chapters |
| `transcript_removed.txt/.json` | the removed text with original-recording timestamps and reasons |
| `transcript_clean.txt/.json` | what `final.mp4` says, with `final.mp4` timestamps |
| `chapters.txt` | YouTube chapters for `final.mp4` (`00:00 Highlights` first when there is a reel) |
| `manifest.json` | size, sha256, duration and status of every file, warnings, timings, AI usage |

Only `final.mp4`, the two transcripts and the manifest are mandatory. Any
other file that fails is listed in the manifest as `failed` (with the reason)
and in the job's `warnings`, and the job still finishes. Individual files are
also served at `GET /jobs/{id}/artifacts/{name}` (e.g. `shorts/short_01.mp4`,
`report.pdf`; `?download=true` for an attachment). On a server set
`SERVE_INDIVIDUAL_ARTIFACTS=false` (only the zip + manifest stay on disk) and
`DELETE_SOURCE_WHEN_DONE=true`. `tools/deliver_7b93.py` builds the whole zip
for the regression meeting from saved AI decisions, with no AI calls.

## Tests

```bash
pytest
```

The test suite covers the deterministic logic (overlap detection, EDL
sorting/de-overlap/snapping/merging, transcript remapping, native-transcript
parsing (VTT/SRT/json3), the transcript-source pipeline branching, API
wiring) with WhisperX, yt-dlp, Zoom and LLM calls mocked out — it does not
require ffmpeg,
torch, or network access. The full pipeline has also been verified end to
end against a real YouTube video with real WhisperX transcription/diarization
and a real Gemini call. The golden-recording tests from spec section 9
(clean / crosstalk-heavy / silence-only) still need real audio fixtures to be
added separately once sample recordings are available.
