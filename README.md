# Highlight Cutter

Given a Zoom cloud recording, a YouTube link or an uploaded recording,
produces one zip per meeting: the highlights reel followed by the cleaned
meeting, vertical shorts, a video of everything removed, a PDF report,
transcripts and YouTube chapters (see "What a finished job delivers").
The platform's own transcript (Zoom's VTT, YouTube's captions) is reused when
there is one, else WhisperX transcribes; an LLM pass (Claude `claude-haiku-4-5`
by default, OpenAI or Gemini via `LLM_PROVIDER`) judges every sentence; ffmpeg does the cutting. Includes a small web
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
- `ANTHROPIC_API_KEY` — the AI pass (keep/remove, highlight scores, re-rank,
  chapters). `LLM_PROVIDER` picks the provider: `anthropic` (default), `openai`
  or `gemini`. `ANTHROPIC_MODEL` defaults to `claude-haiku-4-5`, Anthropic's
  cheapest model ($1 / 1M input, $5 / 1M output tokens: a 98-minute meeting is
  about 100k in + 15k out ≈ $0.18). The account needs credit — a key without
  it fails the job with `llm_quota_exhausted`.
- `LLM_PROVIDER=openai` uses `OPENAI_API_KEY` / `OPENAI_MODEL` (default
  `gpt-6-luna`, $0.10 / $0.50 per 1M tokens ≈ $0.02 per 98-minute meeting,
  `OPENAI_REASONING_EFFORT=none`); `LLM_PROVIDER=gemini` uses `GEMINI_API_KEY`,
  `GEMINI_MODEL`.
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

One zip per meeting (`GET /jobs/{id}/bundle`, resumable; it downloads as
`Final_<Meeting>_<YYYY-MM-DD>.zip`), with:

| File | What it is |
|---|---|
| `Final_<Meeting>_<YYYY-MM-DD>_Youtube.mp4` | the final video: your intro clip, the highlights reel (mostly things worth learning — at most a third funny), a 2 s "topic / Full meeting" card, the cleaned meeting, then your outro clip |
| `highlights.mp4` | the reel on its own (only when the meeting has ≥ 60 s of highlight material) |
| `shorts/short_NN.mp4` + `.srt`, `shorts/shorts.json` | 1080×1920 shorts with burned-in captions and the moment's title — self-contained moments people can learn from first (concepts, how things work, new architectures/features, insights), a genuinely funny one only to fill a slot |
| `removed.mp4` | every cut of 1 s or more (except pure silences), each labelled "Removed 01:10–02:30 · reason" (original-recording time) |
| `report.pdf` | what was removed, when and why (minutes by reason, every cut with its text), highlights, shorts, chapters |
| `transcript_removed.txt/.json` | the removed text with original-recording timestamps and reasons |
| `transcript_clean.txt/.json` | what the final video says, with its timestamps |
| `chapters.txt` | YouTube chapters for the final video (`00:00 Highlights` first when there is a reel; the intro is part of the first chapter) |
| `manifest.json` | size, sha256, duration and status of every file, warnings, timings, AI usage |

**The final video's name.** `<Meeting>` is the meeting's title (the Zoom
topic, the YouTube title, or the upload's title / file name) in CamelCase with
any date in it removed, letters and digits only, at most 60 characters
("All Tech Team Meeting | August 2, 2026" -> `AllTechTeamMeeting`; nothing
usable -> `Meeting`). `<YYYY-MM-DD>` is the day of the meeting: Zoom's start
time, the YouTube video's upload date, a date written in an upload's title,
else the day the job was created. The date is saved on the job
(`meeting_date`), so a re-render keeps the same name. In `job.artifacts`, the
API (`/jobs/{id}/artifacts/final.mp4`, `/jobs/{id}/video`) and n8n the video
is still called `final.mp4`; only its file name changed, and downloads get the
new name. Jobs made before this keep their `final.mp4` file and still work.

Only the final video, the two transcripts and the manifest are mandatory. Any
other file that fails is listed in the manifest as `failed` (with the reason)
and in the job's `warnings`, and the job still finishes. Individual files are
also served at `GET /jobs/{id}/artifacts/{name}` (e.g. `shorts/short_01.mp4`,
`report.pdf`; `?download=true` for an attachment). On a server set
`SERVE_INDIVIDUAL_ARTIFACTS=false` (only the zip + manifest stay on disk) and
`DELETE_SOURCE_WHEN_DONE=true`. `tools/deliver_7b93.py` builds the whole zip
for the regression meeting from saved AI decisions, with no AI calls.

**Intro and outro.** Put two videos in the `intro_outro/` folder of the
project (next to this README; any of .mp4 .mov .m4v .mkv .webm, any size or
frame rate). The file names decide which is which: a name with *intro*,
*opening*, *open* or *start* in it is the intro, *outro*, *ending*,
*closing* or *end* the outro (e.g. `CTD - Opening.mp4` and `FloLabs -
Widescreen outro.mp4`); if only one of two files is named that way, the other
one takes the other role. Every job then plays the intro at the very start of
`final.mp4` and the outro at the very end (`highlights.mp4`, the shorts and
`removed.mp4` stay as they are). Each clip is converted to the meeting's own
size and frame rate (fitted inside the frame with black bars if its shape
differs, its sound kept, silence if it has none) and joined with a hard cut.
The transcript, chapters (the intro belongs to the first chapter), report and
manifest all use the times of the finished `final.mp4`. A missing clip is
simply left out; one that can't be used is left out with a warning in the job
— it never fails the job. Swap a clip by replacing the file; the next job
uses it. Off for every job: `INTRO_OUTRO_ENABLED=false`; per job: the "Add
intro & outro" box (`options.intro_outro`, or the `intro_outro` form field
for uploads). `INTRO_OUTRO_DIR`, `INTRO_FILE` and `OUTRO_FILE` point
elsewhere (relative paths are inside the project folder).

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


You are taking over implementation of **Highlight Cutter v2** at `D:\Coding\Python\FloLabs-Automation` (Windows 11, Python 3.12 venv at `.venv`, run with `.venv/Scripts/python.exe`). Research and planning are DONE; your job is to build. Do not re-plan from scratch — read the plan, then implement milestone by milestone.

## What the product is
A FastAPI service that takes a meeting recording and returns a trimmed video. Today's pipeline (`pipeline.py`): ingest (upload or YouTube via yt-dlp; ffprobe A/V-sync check) → transcribe (reuse an uploaded .vtt/.srt or YouTube captions via `transcribe/native.py`, else WhisperX) → overlap heuristic → decide (one Gemini call per 40-segment chunk returning keep/remove + reason; prompts in `decide/gemini_client.py`) → edl (`edl/builder.py`: union of KEEP ranges → coalesce → snap to word boundaries → merge gaps < 0.3 s → drop < 0.5 s; falls back to the full video if empty) → slice (`slice/ffmpeg_wrapper.py`: keyframe probe; stream-copy if a keyframe is within 2 s else libx264 re-encode; concat demuxer) → remap transcript. Jobs run as FastAPI BackgroundTasks; job state = JSON on disk under `storage/jobs/{id}/`; web UI at `web/index.html` (landing) + `web/app.html` (tool) polls `GET /jobs/{id}`. ffmpeg/ffprobe 9.0.1 are on PATH. 75 pytest tests pass, `ruff check .` is clean. External calls are mocked in tests.

## What v2 must deliver (owner's request, 2026-09-25)
1. YouTube chapter timestamps for the finished video. 2. Zoom-only input: fetch the MP4 and Zoom's own VTT transcript via the Zoom API (YouTube-URL and local-file inputs leave the UI). 3. Smooth dissolve transitions between kept parts. 4. From ONE job: transcript of removed parts; a removed-parts video with source timestamps ("removed 01:10–02:30 · reason"); a PDF report; vertical shorts of fun moments (LLM decides; criteria configurable); a 3–5 min highlights reel (funny / new architecture / new feature / concept / decision); **final.mp4 = highlights reel, then the cleaned meeting**; everything bundled (zip) and delivered together. Target: small CPU-only EC2 started on demand by n8n, ~5 GB working disk cleared per job, delay acceptable.

## Read these first, in this order (do not skip)
1. `C:\Users\Ahsan\.claude\projects\D--Coding-Python-FloLabs-Automation\memory\project_v2_research_and_plan.md` — **the full reviewed spec (decisions D1–D20, rendering/EDL/decide specs, API, config, milestones, open questions).** Everything below is a summary of it.
2. `README.md`, `SESSION_HANDOFF.md` (why the EDL/segmentation code is the way it is — read before touching `decide/` or `transcribe/native.py`), `POSSIBLE_FEATURES.md`.
3. Code: `pipeline.py`, `core/models.py`, `core/config.py`, `decide/gemini_client.py`, `decide/segments.py`, `edl/builder.py`, `slice/ffmpeg_wrapper.py`, `slice/pipeline.py`, `slice/transcript.py`, `transcribe/native.py`, `transcribe/overlap.py`, `ingest/store.py`, `ingest/validate.py`, `ingest/youtube.py`, `api/main.py`, `api/jobs.py`, `web/app.html`, `web/index.html`, `tests/conftest.py`, `tests/test_pipeline.py`, `tests/test_edl.py`, `tests/test_slice.py`, `tests/test_native.py`, `tests/test_api.py`.
4. Research (read the ones relevant to the milestone you are on): `C:\Users\Ahsan\AppData\Local\Temp\claude\D--Coding-Python-FloLabs-Automation\a9343684-bc01-44f8-8127-69e4266908c7\scratchpad\research\ffmpeg.md` (benchmarks + exact working commands for xfade batches, seams, FLAC audio, shorts, drawtext, concat), `zoom.md` (endpoints, scopes, VTT format, webhooks), `specs.md` (YouTube chapter/Shorts rules, reportlab, zip), `codebase.md` (integration points with file:line), `gaps.md`, `review_tech.md`, `review_product.md`, `review_ops.md`. Reproducible ffmpeg lab scripts: `…\scratchpad\ffmpeg_lab\`. If that temp folder is gone, the same content is in the workflow journals under `C:\Users\Ahsan\.claude\projects\D--Coding-Python-FloLabs-Automation\a9343684-bc01-44f8-8127-69e4266908c7\subagents\workflows\`.

## Working rules
- Branch: work on **`flolabs`** (current checkout, pushed to origin). **Never modify `main` or `prod`.** Do not commit or push unless the owner asks; when asked, small commits per milestone.
- Secrets only in the gitignored `.env` (never in `.env.example`). Keep everything free/cheap.
- Backend `.py` changes need a server restart (`.claude/launch.json` config `highlight-cutter-api`, no `--reload`); HTML is re-read per request.
- Run `pytest` and `ruff check .` before declaring any milestone done. Keep tests green; delete the ~10 YouTube tests when the YouTube path goes.
- **Do not delete** `storage/jobs/7b93aa4cd7ef436895213e6fff9365a3/` or `storage/videos/50c55c4c8dd64542869bc4e6f30e69e5.mp4` — the 98-min regression fixture. `storage/video.mp4` is a damaged file; don't use it as a golden fixture (use a synthetic `testsrc2`+`sine` clip). Ignore the `n/` folder (unrelated PDFs).
- Report progress per milestone in plain language; ask before anything irreversible.

## The decisions (summary — the plan file has the full spec)
- **Retire stream-copy; re-encode every piece once** with ONE pinned profile: `-c:v libx264 -preset veryfast -crf 24 -profile:v high -pix_fmt yuv420p -r F -fps_mode cfr -maxrate 2500k -bufsize 5000k` (pieces `-an`), audio `-c:a aac -b:a 128k -ar 48000 -ac 2`. Input-side `-ss`. Assert extradata-md5/fps/size/audio equality before any concat; post-render asserts from MP4 headers only. Why: the current copy mode starts at the previous keyframe → the real 98-min job is 78 s longer than its edit list (removed content leaks back in, transcript drifts) and mixed copy/encode concat is broken.
- **Transitions:** batched xfade dissolves, `d_frames = 2·round(F/4)` (12 @25 fps, 16 @30 fps), `h = d/2`, all cut points integer frames on the source grid (`Fraction` from `r_frame_rate`), half-extension of `h` into the removed region, batches ≤20 inputs, seam pieces between batches, `concat -c copy` of video-only pieces; audio assembled once from `audio.flac` (`aresample=async=1:first_pts=0`, source channel count) with hard cuts + 20 ms micro-fades at the dissolve midpoint. Output timeline equals the hard-cut timeline (`out(t) = t − s_i + O_i`).
- **EDL is the single source of truth:** `build_edl(decisions, words, duration, fps, fade_frames, min_segment_s, full_video_fallback)` = coalesce → word snap → frame snap → coalesce → merge gaps < (d+1)/F → drop keeps < max(min_segment, 2d) → drop start ≥ duration → validate. Removed = `complement()`. Three tiers: gap < d+1 frame stays in the video (merged); [d+1 frame, 1.0 s) cut but only in transcript/PDF; ≥ 1.0 s also in `removed.mp4` with a label.
- **Single cleanup pass** (light-cleanup prompt). Remove `EditMode`, the mode stamps, `HIGHLIGHTS_SYSTEM_PROMPT`, the `mode` field. "Highlights" = the reel only. Decide = one multi-label Gemini call per chunk of 30 (`response_json_schema` — verify once against `gemini-3.5-flash-lite`, fall back to `response_mime_type` + TypeAdapter) returning `{index, decision, reason, removal_category, highlight_score, highlight_category}` with index-set validation; then one re-rank call (top ~60 → title, hook, calibrated score, `short_worthy`); then one chapters call. Persist `decisions.json` per chunk. `highlights_criteria` / `shorts_criteria` are config strings injected into prompts. Token-bucket limiter `gemini_rpm=14`, 429 `retryDelay` parsing, daily-quota fail-fast `llm_quota_exhausted`.
- **Highlights reel:** T=7 → moments (gaps ≤3 s, ≥8 s) → greedy by score under 300 s (min(300, 30 %) for short meetings; back off T to 6/5/4 below 180 s) → chronological → `build_edl(min_segment 8 s, no fallback)` → rendered from the SOURCE with dissolves; < 60 s qualifying → no reel.
- **Shorts:** funny-category candidates by score (fill from top), 4 per meeting, 20–60 s, cut from the SOURCE and `audio.flac`, captions = source-time cues in `[S, S+T]` re-timed by −S, 1080×1920 blurred background + `subtitles=` filter (`fontfile=`/installed font).
- **final.mp4** = video concat of highlights + 2 s title card (topic + "Full meeting", with `anullsrc` silent audio, same profiles) + cleaned, plus one audio concat, muxed once; `cleaned.mp4` only behind `keep_cleaned_separately`.
- **Chapters:** LLM topics over the cleaned transcript → sentence-start snap → offset by measured highlights+card VIDEO duration and prepend `00:00 Highlights` (no reel → D_pre 0, first topic at `00:00`) → validate on the final list (`00:00`, ≥3, ≥10 s apart, ascending, ≤5000 bytes, no `<>`, `H:MM:SS` over 1 h) → retry once → omit. YouTube upload is manual in v2 (paste `chapters.txt`).
- **Report:** reportlab (add to requirements): meeting header (topic, time, host, UUID, job, durations, speakers), stats (merged-back / cut-not-shown counts, minutes by category + bar chart), removed-ranges table, seams, chapters, highlights, shorts, warnings. `transcript_removed.txt`: `[MM:SS–MM:SS] Speaker: text (reason)`.
- **Zoom ingest:** Server-to-Server OAuth (`grant_type=account_credentials`, 1 h token, re-mint on 401, never `me`); `GET /users/{email}/recordings?from&to` (≤1 month); `GET /meetings/{double-URL-encoded uuid}/recordings`; MP4 by `recording_type` priority (speaker_view > active_speaker > gallery variants > shared_screen > host_video; ignore "(CC)"); TRANSCRIPT VTT paired to its MP4 segment by `recording_start/end`; multi-part → concat segments in order (`-c copy`) and offset VTTs by measured prior durations; download with `Authorization: Bearer` following redirects, validate `ftyp`/size, Range resume. Readiness checked synchronously in `POST /jobs/zoom` (425 `recording_not_ready`/`transcript_not_ready` + `retry_after_s`). `require_native_transcript=true` on EC2 (no WhisperX). Webhooks live in n8n. Remove the YouTube path entirely; keep a hidden `POST /jobs` upload for ops/regression. Zoom's VTT `Name: text` prefix already parses (`transcribe/native.py`); harden: name-shape/recurrence check, CRLF, cues past the MP4 end.
- **API/lifecycle:** `X-API-Key` on every route except `GET /health`; idempotent `POST /jobs/zoom` (same uuid + options → same job); `max_queue_depth=1` → 503 `busy`; `error_code` + `retryable` + `retry_after_s` on JobRecord; single-worker queue; startup reconciliation (non-terminal → `interrupted`, retryable); heartbeat `updated_at` every 30 s; `stale` flag; `job_max_wall_s=10800`; `DELETE /jobs/{id}` (queued → wipe; running → 409 unless `?force=true` → cooperative cancel); `GET /jobs`, `GET /jobs/{id}/log`; the service stops the instance itself via an idle watchdog (`systemctl poweroff`, InstanceInitiatedShutdownBehavior=stop).
- **Disk:** `min_free_disk_bytes=6 GiB` preflight; source + VTT inside `jobs/{id}/`; deletion order: batch pieces after each concat → cleaned intermediates after mux → source + FLAC after shorts → cleaned after final → constituents after zip (`serve_individual_artifacts=false` on EC2); startup sweep + 24 h retention; timeouts `max(120, 3·content_s)` capped 1800.
- **Delivery:** `bundle.zip` (ZIP_STORED mp4) + `manifest.json` (sha256/bytes/duration/status per artifact, warnings, stage timings, Gemini usage); `delivery_mode=s3` default (instance profile, multipart upload, presigned URL 24 h); `http` mode only with integrity check. Partial success: mandatory = final.mp4, clean/removed transcripts, manifest, bundle; optional artifacts fail soft (`warnings[]`, job still `done`).
- **`decide_only` dry-run** stops after decisions/selection so the owner can tune criteria without a 30-min render.

## Milestones — start at M0, then M1
- **M0 (<1 session):** fix `slice/ffmpeg_wrapper.py:35` (`float(line.split(",")[0])`); put the last 2 kB of ffmpeg stderr + the command into `job.error`; add `reportlab>=4.4,<6` to `requirements.txt`; `created_at/updated_at`; `GET /health`.
- **M1 Foundation (~2 sessions):** profile + frame math (`core/timeline.py`, `slice/profile.py`), FLAC audio, batched xfade renderer + seams + concat, RenderManifest + asserts, manifest-based remap, fps-aware `build_edl` + `complement()`, single-worker queue + reconciliation + heartbeat, API key, `DELETE`, deletion table, timeouts. **Gate: job 7b93 renders frame-exact (cleaned frames == Σ range frames) — this proves the 78 s drift is gone.** Unit-test the frame math at 24/25/29.97/30/60 fps.
- **M2 Zoom ingest (~1–2, needs the owner's Zoom admin/S2S app):** `ingest/zoom.py`, pairing/concat, readiness, idempotency/busy, error codes, remove YouTube path + mode picker, picker UI. Do it on a branch off `flolabs` rebased onto M1 (shared files).
- **M3a Decide v2 + selection (~1–2, can run parallel to M2):** first make ONE real `response_json_schema` call against the model with a 30-segment chunk from 7b93's `transcript.json`; then schema-parametrised client + limiter + incremental persist, multi-label pass, re-rank, highlights/shorts selection, `decide_only`. **M3b chapters** after M1.
- **M4 Outputs (~2–3):** removed/highlights/shorts/final/card renderers, chapters.txt, transcripts, report.pdf, manifest, zip, S3 delivery, partial-success contract, results UI.
- **M5 Ops:** systemd unit + SSM secrets, watchdog, EC2 image (Ubuntu 24.04, ffmpeg, fonts-dejavu-core, Python 3.12, no torch), instance profile, security group, n8n flows, cost alarm.

## Questions the owner still has to answer (use these defaults until they do)
Zoom plan tier / admin / "Create audio transcript" enabled (blocks M2). Multi-part recordings → concat in order. `require_native_transcript=true`. Shorts = funny, 4 × 20–60 s. Highlights 300 s, min(300, 30 %), skip < 60 s. Text-only cleanup on Zoom accepted. Delivery = S3; YouTube manual; instance c6a.xlarge; Gemini paid tier recommended. No separate cleaned.mp4; 720p; 0.5 s-target dissolve with hard-cut audio; 2 s title card; labels include the reason; hidden upload endpoint kept; PDF is greenfield (no existing template).

## First actions
1. Read the plan file and the code list above. 2. Run `pytest` and `ruff check .` to confirm the baseline (75 passed). 3. Do M0, run the tests, report. 4. Start M1 with the frame-math module and its tests, then the renderer, then the 7b93 gate.
# Highlight Cutter v2 — continuation prompt (state as of 2026-09-25, end of the build session)

You are continuing implementation of **Highlight Cutter v2** in `D:\Coding\Python\FloLabs-Automation` (Windows 11, Python 3.12 venv, run everything with `.venv/Scripts/python.exe`, ffmpeg/ffprobe 9.0.1 on PATH). Research, planning, M0, M1 and M3a (+ M3b chapters) are DONE and tested but **NOT committed** — everything is uncommitted work on branch **`flolabs`**. Do not re-plan; continue from "Next steps".

## 1. What the product is and what the owner wants
FastAPI service that turns a meeting recording + transcript into edited outputs. The owner (FloLabs, "Hareem", terse typo-heavy messages, wants plain-language progress reports, cost-conscious) confirmed the final deliverable on 2026-09-25:
> For each Zoom meeting: **one zip** containing (1) **one video = highlights reel first, then the cleaned (noise-removed) meeting**, (2) **the shorts, ready for posting**, (3) **a separate video of everything removed** (labelled with source timestamps + reason), (4) **a report of what text was removed and at which timestamps** (PDF + text), plus YouTube chapter timestamps — **while keeping LLM token usage optimised**.
Input will be Zoom only (Zoom API: MP4 + Zoom's own VTT transcript), orchestrated by n8n, running on a small on-demand CPU-only EC2 that is started per job and stops itself. Delay is acceptable.

## 2. Read these first, in this order
1. `C:\Users\Ahsan\.claude\projects\D--Coding-Python-FloLabs-Automation\memory\project_v2_research_and_plan.md` — the reviewed spec (decisions D1–D20, §6 rendering spec, §8 artifacts, §9 UI, §10 Zoom, §11 API, §13 config, §14 milestones, §16 open questions) **plus build logs §19 (M0+M1) and §20 (M3a)**. Where this prompt and the plan disagree, this prompt is newer.
2. `README.md` (updated "v2 service behaviour" section), `SESSION_HANDOFF.md` (why the EDL/segmentation code is shaped as it is — read before touching `decide/` or `transcribe/native.py`).
3. Code (all of these are new or rewritten in v2):
   - `core/`: `config.py` (all v2 settings with comments), `models.py` (EDLRange frames, RemovedRange, RenderManifest, SegmentJudgment, Moment{peak_index, raw_score}, ShortClip, Chapter, JobOptions, JobRecord{options, error_code, retryable, warnings, llm_usage, decisions/selection/chapters paths, stale}, statuses incl. `reporting`, `decided`, `cancelled`), `timeline.py` (Fraction frame math), `proc.py` (run_checked: stderr tail + poll hook), `errors.py` (PipelineError family, `classify()`), `version.py`.
   - `slice/`: `plan.py` (pure batch/seam/atom planner), `profile.py` (STANDARD encoder profile, probe_media/MediaInfo, header-only probe_header, concat asserts, timeouts), `ffmpeg_wrapper.py` (video part graphs, FLAC extraction, audio chunks, AAC, mux, concat demuxer), `pipeline.py` (`render_cleaned` → RenderManifest), `transcript.py` (midpoint remap via manifest).
   - `edl/`: `builder.py` (fps-aware build_edl, `_fix_short_keeps`, `complement()`, validate), `highlights.py` (candidate_moments, calibrate_by_rank, select_highlights with diversity cap + peak trimming, select_shorts).
   - `decide/`: `prompts.py` (cleanup rules + compact plain-text protocol; rerank; chapters; category code maps; REMOVAL_LABELS), `gemini_client.py` (GeminiCaller/LazyCaller, limiter, 429/daily-quota/5xx/network handling, stage deadlines, judge_segments with context lines + retry/split + collapsed-chunk re-score + decisions.json persistence, line parsers, rerank_moments with follow-up call + complete_window), `repair.py` (sentence-fragment repair), `chapters.py` (sentence-level chapters, retry on invalid/too-long/too-short, finalize_chapters with/without reel, format).
   - `api/`: `main.py` (routes), `queue.py` (single worker, cancel, heartbeat, job.log, reconcile, retention, delete_job_files), `auth.py` (pure ASGI X-API-Key middleware; cookie `hc_api_key`), `jobs.py` (JobStore + job-id validation).
   - `pipeline.py` (orchestration), `ingest/store.py` (uploads go to `jobs/{id}/source.*`), `ingest/youtube.py` (optional dest_dir), `web/app.html` (API-key fetch wrapper, "show me the AI's picks first" option, picks table + Render now, chapters copy box, warnings note), `web/index.html` (landing copy updated).
   - Tools: `tools/regress_7b93.py` (renderer gate), `tools/decide_7b93.py` (real-Gemini decide regression).
   - Tests: all of `tests/` (258 tests).
4. Research notes (only if needed): `C:\Users\Ahsan\AppData\Local\Temp\claude\D--Coding-Python-FloLabs-Automation\a9343684-bc01-44f8-8127-69e4266908c7\scratchpad\research\{ffmpeg,zoom,specs,codebase,gaps,review_tech,review_product,review_ops}.md` (ffmpeg.md has exact working commands for removed-video labels, drawtext `textfile=`/`fontfile=`, blurred 1080×1920 shorts with `subtitles=`, concat with `duration` lines, title card with `anullsrc`; NOTE: `-filter_complex_script` no longer exists in ffmpeg 9 → use `-/filter_complex <file>`). Lab scripts: `…\scratchpad\ffmpeg_lab\`, `…\scratchpad\m1\exact_check.py` (pixel-exact frame check at 24/25/29.97/30/60) and `m1\sync_check.py` (audio beep sync).

## 3. Working rules
- Branch `flolabs`. **Never modify `main` or `prod`. Do not commit or push unless the owner asks** (suggested when asked: one commit per milestone — M0, M1, M3a+fixes).
- Secrets only in the gitignored `.env` (has GEMINI_API_KEY, GEMINI_MODEL=gemini-3.5-flash-lite, FFMPEG_BIN/FFPROBE_BIN); `.env.example` has placeholders only. Keep everything free/cheap; **minimise Gemini tokens** (owner requirement).
- After backend `.py` changes restart the preview server (`.claude/launch.json` config `highlight-cutter-api`, uvicorn on 127.0.0.1:8000, no `--reload`); HTML is read per request.
- Before declaring anything done: `.venv/Scripts/python.exe -m pytest -q` and `.venv/Scripts/python.exe -m ruff check .` (currently **258 passed, ruff clean**). After any renderer change re-run `tools/regress_7b93.py --out <scratch dir>` (must print GATE PASSED) and the scratchpad `m1\exact_check.py` / `m1\sync_check.py`.
- **Never delete** `storage/jobs/7b93aa4cd7ef436895213e6fff9365a3/` (now has a `.keep` marker; DELETE and retention refuse it) or `storage/videos/50c55c4c8dd64542869bc4e6f30e69e5.mp4` (98.5-min 30 fps 750×480 regression source; its cached `transcript.json` is caption-cue level — regroup with `transcribe.native._group_into_sentences`). `storage/video.mp4` is damaged; don't use as a fixture. Ignore the `n/` folder.
- Shell gotcha on this machine: bash heredocs that contain `\n`/`\[` escapes or apostrophes get mangled by the tool — write edit scripts to a scratch `.py` file (Write tool) or use the Edit tool.
- Report progress in plain language, per milestone; ask before anything irreversible.

## 4. What exists now (done and verified)
**M0:** ffprobe csv trailing-comma crash fixed; all ffmpeg/ffprobe calls go through `core/proc.run_checked` (job.error carries the command + last 2 kB of stderr); `reportlab>=4.4,<6` in requirements; job `created_at/updated_at`; `GET /health`.

**M1 (renderer + service):**
- Every kept range re-encoded once with ONE pinned profile (libx264 veryfast crf24 high yuv420p, `-r F -fps_mode cfr`, maxrate 2500k; AAC 128k 48 kHz stereo). All cut points are integer source frames (`core/timeline.py`, Fractions). Dissolve `d = 2·round(F·0.25)` frames (12 @25, **14 @29.97**, 16 @30), half-extension into the removed gap so the output timeline equals the hard-cut timeline. Batches ≤20 inputs and ≤600 s content, seam pieces between batches, long ranges split into hard-joined "atoms", `concat -c copy` of video-only pieces with extradata/fps/size asserts; audio cut sample-exactly from a normalised 48 kHz FLAC (cumulative sample boundaries, unbounded `apad`, 20 ms micro-fades), one AAC encode (timeout scales with length), mux into work_dir then atomic move after header asserts (video nb_frames exact; audio ±2048 samples).
- Per-input video chain (after review fix): `-ss` a quarter frame early, `fps=F:start_time=0:round=down,scale,setsar,format,tpad=clone,trim=end_frame=N,setpts` — correct for VFR sources with held frames and late-starting video (verified 0 mismatches). Known tiny gap: a range that starts inside a static VFR stretch shows the next changed frame for <1 s.
- **Gate passed on the real 98-min meeting:** 131 ranges, 157,890 frames rendered == expected exactly, audio 5263.000 s, 24 spot checks show the predicted source frame (v1 output showed the wrong picture), 312–327 s render on the laptop, 184.5 MB. The 78 s timeline drift of v1 is gone.
- EDL (`build_edl(..., fps, fade_frames, min_segment_s, full_video_fallback, total_frames, min_gap_s)`): coalesce → word snap → frame snap → merge gaps < max(0.3 s, d+1 frames) → short keeps are **widened into their gaps**, else merged only across a gap ≤ 1 s, else dropped (the old "merge into next" brought 7 s of removed small talk back) → edge slivers < h kept → validate; a lone range needs no 2d minimum. `complement()` tiles [0,total] exactly; tiers `video` (≥1.0 s, goes into removed.mp4) / `transcript_only`.
- Service: single-worker queue (`api/queue.py`), `max_queue_depth=1` → 503 `busy` + Retry-After; heartbeat every 30 s during ffmpeg (bumps updated_at; `stale` flag after 600 s); cooperative cancel (DELETE `?force=true` kills the running ffmpeg within a heartbeat); per-job `job.log`; startup reconciliation (non-terminal → failed/`interrupted`/retryable); retention sweep (`JOB_RETENTION_HOURS`, 0 locally, 24 on EC2; `.keep` protected); disk preflight 6 GiB; error codes + `retryable` + `retry_after_s`; X-API-Key via pure ASGI middleware before the body is read (empty `HC_API_TOKEN` = open for local dev; /docs disabled); job-id validation `[0-9a-zA-Z_-]{1,64}`; YouTube downloads deleted with their job; upload endpoint runs in the threadpool.
- Routes: `GET /health`, `POST /jobs` (hidden upload: file, transcript, decide_only, transitions, highlights_target_s, shorts_count, highlights_criteria, shorts_criteria), `POST /jobs/youtube` (to be removed in M2), `GET /jobs`, `GET/DELETE /jobs/{id}`, `POST /jobs/{id}/render` (decided job, or failed+retryable job with saved decisions), `GET /jobs/{id}/{video,edl,removed,selection,chapters,transcript,log}`.

**M3a/M3b (AI decide stage), token-optimised after two review rounds:**
- ONE judge pass per chunk of **60 sentences** with **3 read-only context lines** each side (prefixed `~`). Compact plain-text protocol: input lines `[n] Speaker: text` (speaker only when it changes); answer lines `n k|r score removal-code highlight-code` (e.g. `312 r 0 fill -`), parser tolerant of dropped placeholders, stray repeated numbers, `[n]` brackets, code fences, and answers for context lines (ignored). JSON was tried first: pretty-printing made ~80% of output tokens whitespace.
- Prompt = the stress-tested light-cleanup rules + new rules: hand-offs / bare calls to speak / content-free praise → remove as housekeeping (but substantive questions are kept); bare "no" answers → remove Q and A; never cut a sentence in half. Scoring rubric with absolute anchors ("judge the whole thought"); categories: removal codes fill/greet/intro/house/xtalk/tang/rep/dead, highlight codes fun/arch/feat/idea/dec/ins (full names in `core/models.py`).
- Robustness: retry once, split on MAX_TOKENS or a second failure, exact coverage of the chunk's lines; collapsed chunk (≥12 kept lines all scored 0) re-scored once (max 4/job); `decisions.json` saved per chunk with a fingerprint (model+prompt+chunking+transcript) → a rerun or a decide_only render reuses it (0 calls). `GeminiCaller`: 14 RPM limiter, 120 s HTTP timeout, 429 waits for server retryDelay, **daily quota → `llm_quota_exhausted` with retry_after = seconds to midnight Pacific**, 5xx/network retried ×3, permanent 4xx/missing key → non-retryable `llm_error`, per-stage deadlines (decide 1800 s, chapters 300 s), usage (calls/tokens) accumulated into `job.llm_usage`.
- `decide/repair.py`: removed short fragments that finish or start a kept same-speaker sentence are put back.
- Highlights: candidates = sentences scoring ≥3 (funny ≥2), joined across ≤10 s pauses or ≤2 bridged lines (≤30 s), best 60 by peak → ONE re-rank call (pipe lines `id|score|code|a|b|y/n|title|why`, one follow-up call only for skipped ids, window completion so clips never start on a connective/lowercase or end mid-sentence) → **`calibrate_by_rank`** (re-rank scores only order the candidates; calibrated score = 10·(N−rank)/N; raw < 2 → never selected) → `select_highlights`: best first within budget min(300 s, 30 % of meeting), floor 5 (≈top half), diversity cap 35 % of budget per 10-min stretch (relaxed if reel < 60 % of budget), clips widened to ≥12 s / trimmed around the peak to ≤60 s, no reel if < 60 s of material. Saved: `rerank.json` (only reused when it succeeded), `selection.json`, `edl_highlights.json`.
- Shorts: short-worthy moments (re-rank `y`, strict: "a useful moment is not enough"), preferred category `funny` first, 20–60 s sentence-snapped windows trimmed away from the core, trailing filler dropped, non-overlapping; fallback to categories if the re-rank failed. Dry technical meetings may legitimately yield 0 shorts (warning added).
- Chapters (after render, cleaned timeline): sentence-level lines `[n mm:ss] text` → `n|title` lines; one retry on invalid lists, chapters > max(10 min, 2.5×median) or < 45 s; `finalize_chapters(reel_s=…)` prepends `00:00 Highlights` when a reel exists, forces first topic to 00:00 otherwise, enforces YouTube rules (≥3, ≥10 s apart, last ≥10 s, ≤5000 bytes, no `<>`, H:MM:SS past 1 h). Written to `chapters.json` + `chapters.txt` (cleaned timeline for now).
- `decide_only`: job stops at status `decided` with edl/removed/selection saved; UI shows the picks table; "Render now" (`POST /jobs/{id}/render`) renders with 0 new AI calls.
- **Real 98-min regression (latest, `tools/decide_7b93.py`):** 1061 sentences; judging 23 calls, ~51k input / ~13k output tokens (first JSON version: 38 calls, 123k / 68k); whole decide incl. re-rank + chapters ≈ 100k in / 15k out. Cleanup removes 25.7 min (v1-validated prompt removed 20.9–21.9; the extra is hand-offs/praise by the new rule — spot-checked mostly correct); opening intro ceremony 64/69 removed. Reel 6 moments / 322 s spread over the meeting (28:20, 35:37, 59:31, 1:06:19, 1:10:05, 1:22:52). Shorts 0 (meeting has ~no funny moments). Chapters 10, boundaries at real topic changes. Outputs in `…\scratchpad\decide7b93_v23\` (report.txt, judgments.tsv, selection.json, clean_transcript.txt); earlier runs in `decide7b93\`, `decide7b93_v21\`, `decide7b93_v22\`.

## 5. Next steps, in order
1. **Small fix first:** in `decide/gemini_client.py::parse_rerank_lines` strip a trailing `|` (and whitespace) from the title and hook — the last run produced hooks ending in `|` (e.g. "…for sharing.|"). Add a test. Also consider accepting a missing trailing field.
2. **Optional quality pass:** run `tools/decide_7b93.py --out <new dir>` and (if the owner wants) a read-only reviewer on `judgments.tsv` to confirm the new housekeeping rule doesn't cut substantive questions (the prompt now says keep them). Keep token cost in mind: one full run ≈ 100k in / 15k out tokens on the free tier.
3. **M4 Outputs — the owner's zip (main remaining work).** Build in this order, each with tests and a real render on the 7b93 fixture:
   a. `render_removed` (tier `video` ranges from `edl_removed.json`, hard cuts, `drawtext` via `textfile=` + `fontfile=` label "removed MM:SS–MM:SS · <reason label>", audio from the FLAC; skip when nothing qualifies). Font: auto-detect (Windows `C:/Windows/Fonts/arial.ttf`; Linux DejaVu) via `settings.font_file`.
   b. `render_highlights` from `edl_highlights.json` (reuse `render_cleaned` machinery with kind="highlights"; dissolves; from the SOURCE).
   c. Shorts renderer: 1080×1920 blurred background (blur at 270×480 then upscale), captions = source-time cues in [S, S+T] re-timed by −S → SRT → `subtitles=` with force_style font; audio from FLAC; `shorts/short_NN.mp4` + `shorts.json`.
   d. Title card (2 s, `lavfi color` + drawtext "<topic> — Full meeting", `anullsrc` stereo 48 kHz, STANDARD profile, integer frames).
   e. `final.mp4` = concat(highlights, card, cleaned) video pieces (concat demuxer with `duration` lines = video durations) + one audio concat, muxed once; asserts; measure `D_pre` = highlights+card VIDEO duration → re-run `finalize_chapters(chapters, final_duration_s, reel_s=D_pre)` → final `chapters.txt`. No reel → final = cleaned (card optional/skipped), chapters as now.
   f. `transcript_clean.txt/.json`, `transcript_removed.txt` (`[MM:SS–MM:SS] Speaker: text (reason)` grouped per removed range, source timestamps) and `.json`.
   g. `report/pdf.py` (reportlab, greenfield): header (topic, date, host, job id, version, durations), stats (kept/removed minutes, merged-back count, cut-not-shown count, minutes by removal category + bar chart), removed-ranges table (source time, duration, reason, text excerpt), highlights list, shorts list, chapters, warnings, LLM usage.
   h. `manifest.json` (per artifact: path, bytes, sha256, duration, status ok/failed/skipped + reason; warnings; stage timings; llm_usage) and `bundle.zip` (ZIP_STORED for mp4, DEFLATED for text/pdf). Partial-success contract: mandatory = final.mp4, transcripts, manifest, bundle; optional artifacts fail soft (warning, job still `done`).
   i. Deletion order for disk (plan §6.5): pieces after concat; source + FLAC after shorts; cleaned after final (unless `keep_cleaned_separately`); constituents after zip when `serve_individual_artifacts=false` (EC2). Add routes `GET /jobs/{id}/bundle` (FileResponse with Range) and `/jobs/{id}/artifacts/{name}`; UI results panel (final video player, shorts, removed video, report/zip download, chapters box).
   j. S3 delivery (`delivery_mode=s3`, boto3 multipart, presigned URL 24 h) can be M4-late or M5.
4. **M2 Zoom ingest** — blocked on the owner: Zoom plan tier, an admin to create a Server-to-Server OAuth app with `cloud_recording:read:*:admin` scopes, and "Create audio transcript" enabled. Then `ingest/zoom.py` per plan §10 (S2S token, list/get recordings, MP4 priority, TRANSCRIPT pairing, multi-part concat + VTT offset, readiness 425, idempotent `POST /jobs/zoom`), remove the YouTube path + tests, Zoom picker UI.
5. **M5 ops:** systemd unit, SSM secrets, idle watchdog (service powers the instance off), EC2 image (Ubuntu 24.04, ffmpeg, fonts-dejavu-core, Python 3.12, no torch), security group, n8n flows, cost alarm.

## 6. Owner questions still open (use these defaults)
Zoom tier/admin/transcripts (blocks M2); multi-part recordings → concat in order; `require_native_transcript=true` on EC2; shorts = funny, 4 × 20–60 s (0 is acceptable for dry meetings — ask the owner if a technical fallback is wanted); highlights 300 s target; text-only cleanup accepted; delivery S3; YouTube upload manual (paste chapters.txt); c6a.xlarge; Gemini paid tier recommended (free-tier data terms).

## 7. Local state to know
- Preview server may be running (`highlight-cutter-api`, port 8000); restart after backend edits.
- Test jobs from this session in `storage/jobs/` (d649729d…, c7e42222…, d21dc695…) are disposable; 6 old QA jobs were marked `interrupted` by reconciliation (expected).
- Workflow journals of the reviews: `C:\Users\Ahsan\.claude\projects\D--Coding-Python-FloLabs-Automation\a9343684-bc01-44f8-8127-69e4266908c7\subagents\workflows\` (wf_8bc75e0b-2c3 = code review with 24 confirmed findings, all fixed; wf_640d9fd3-812 = quality review of the first decide run; wf_8bb731ef-38a = renderer/service fix agents).

## 8. First actions
1. Read §2 files (plan §19–§20 first). 2. Run pytest + ruff (expect 258 passed, clean). 3. Do next step 1 (rerank trailing `|`), then report to the owner in plain language and start M4 (3a → 3i), re-running the render gate after renderer changes.