# Highlight Cutter — Session Handoff

Complete project context as of 2026-09-18, written so a fresh chat can pick up
this project with zero prior context. This file is a snapshot, not living
documentation — `README.md` is the source of truth for how to run/use the
system; this file explains *why things are the way they are* and what
happened in the session that built most of it.

## What this project is

**Highlight Cutter**: paste a YouTube URL or upload a meeting recording, pick
a mode, and get back a trimmed video.

- **Highlights mode** (`mode=highlights`): aggressive cut, keep only the best
  moments — verified working well (a 30-min video → ~2-min highlight reel).
- **Light cleanup mode** (`mode=crosstalk`): a *light* trim — keep almost
  everything, cut only crosstalk, filler, greetings/small talk, and ceremony
  (intros, "can everyone hear me") that isn't the actual meeting agenda.

Audience: people who record long meetings/webinars/podcasts and want an
automated, self-hosted way to get a shorter, curated cut without manually
scrubbing hours of footage. Built to stay free/cheap wherever possible —
WhisperX and ffmpeg are self-hosted; Gemini is the only paid-adjacent
external call, and even that has a generous free tier.

## Pipeline (end to end)

```
YouTube URL / uploaded file
  -> [ingest] download (yt-dlp) or store upload; ffprobe A/V-sync check
  -> [transcribe] EITHER reuse a platform transcript (YouTube captions /
     user-uploaded .vtt/.srt) OR run WhisperX (ASR + forced alignment +
     pyannote diarization) — see "Native transcript reuse" below
  -> [overlap] cheap heuristic overlap flagging (no LLM)
  -> [decide] Gemini keep/remove pass over the transcript, batched into
     chunks so long meetings can't blow past the model's output-token limit
  -> [edl] turn decisions into a validated, non-overlapping Edit Decision
     List (de-overlap, snap-to-boundary, merge small gaps, drop tiny bits)
  -> [slice] ffmpeg: keyframe-aware stream-copy or re-encode per range,
     concat into final output
  -> clean output video + remapped transcript
```

Module layout: `ingest/`, `transcribe/`, `decide/`, `edl/`, `slice/`, `api/`,
`core/` (config/models/logging), `pipeline.py` (orchestrates all stages),
`web/index.html` (single-file vanilla HTML/CSS/JS frontend).

## Native transcript reuse (built this session, working)

WhisperX is the most expensive stage by far. `transcribe/native.py` tries to
avoid it entirely:

- **YouTube**: `fetch_youtube_transcript()` uses yt-dlp to pull the video's
  own captions (creator-uploaded preferred over auto-generated), fetched in
  the clean `json3` format specifically — NOT vtt, because YouTube's
  auto-caption vtt export is "rolling"/karaoke-style (overlapping, repeated
  text across cues), which json3 doesn't have.
- **Uploads**: an optional `transcript` file field on `POST /jobs` accepts a
  `.vtt`/`.srt` (e.g. Zoom's exported `audio_transcript.vtt`).
- Falls back to real WhisperX transcription silently whenever no transcript
  is available or it fails to parse — never fails the job over it.
- `JobRecord.transcript_source` (`asr` | `youtube_captions` |
  `uploaded_transcript`) records which path was used; the web UI shows this.

**Known limitation, by design, not a bug**: neither YouTube captions nor
uploaded VTT/SRT preserve genuine overlapping-speech data (both are
single-stream serialized captions) — so native-transcript sources work well
for highlights mode but crosstalk/light-cleanup mode's *overlap-based*
detection specifically has nothing to detect from that data. The keep/remove
LLM pass still runs and still cuts filler/ceremony correctly (that's judged
from content, not overlap), but true simultaneous-crosstalk removal still
needs a real WhisperX pass.

## The big bug this session found and fixed (read this before touching decide/ or transcribe/native.py)

**Symptom reported by user**: ran a 1h38m real meeting through Light cleanup
mode. Only ~11 minutes got cut; a whole "let's welcome our new members,
please introduce yourself" ceremony (several minutes) was left in, which is
exactly what light-cleanup is supposed to strip.

**Root cause #1 — sentence fragmentation.** For the native-transcript path,
each *segment* sent to Gemini used to be one caption **cue** (a caption
cue is a screen-display unit, not a sentence — e.g. YouTube commonly splits
one sentence across 2-3 consecutive cues, or crams 3 short sentences into
one cue). Feeding the LLM these arbitrary fragments instead of whole
sentences made correct judgment harder and produced worse cut points.
**Fixed** by `_group_into_sentences()` in `transcribe/native.py`: recombines
cues into real sentence-level segments (splits at `.!?`, always breaks on a
speaker change, has a word/duration safety cap for tracks with no
punctuation).

**Root cause #2 — the prompt was too narrow.** The original crosstalk prompt
only told the model to remove literal interruptions/overlapping speech. A
"please introduce yourself" exchange isn't literally crosstalk (one person
talks at a time, makes a real statement) — so the model was *correctly*
keeping it under that narrow definition. **Fixed** by rewriting
`CROSSTALK_SYSTEM_PROMPT` in `decide/gemini_client.py` around an explicit
"agenda vs. not agenda" framing: remove crosstalk, greetings, round-robin
introductions/welcome ceremony, and meeting housekeeping even when on-topic
and non-overlapping; keep anything substantive — **including status updates
from someone being called on by name**, which is structurally similar to an
introduction but semantically real content (this exact distinction was
stress-tested against real transcript data and confirmed correct).

**Root cause #3 — the actual show-stopper, found by tracing a real EDL
build.** Fixing #1 by splitting one cue into multiple sentences meant those
sentences all inherited the **same** start/end timestamp (the whole cue's
span — there's no finer real timing available). When the LLM correctly
marked one of those same-timestamp sentences "remove" and a neighbor "keep",
the EDL builder (which only unions "keep" ranges — it never subtracts
"remove" ranges from anything) let the "keep" silently override the
"remove" for that identical span. Worse, this could chain through
`_coalesce_overlaps` across many consecutive cues and weld huge, unrelated
stretches of the video into one giant "keep" block — confirmed directly: a
single 35-to-78-minute keep-range appeared in test runs, *before any
threshold-based merging even ran*. **Fixed** two ways:
1. `_interpolate()` in `transcribe/native.py` gives same-cue sentences
   distinct, proportional, non-overlapping sub-timestamps instead of all
   sharing the whole cue's span (an estimate, not real timing — but a
   monotonically increasing one, which is what actually matters).
2. `segments_to_words()` (new) converts the sentence-level segments into the
   `Word` list used for **EDL snap-to-boundary** — `pipeline.py`'s native
   branch now derives `words` from the sentence segments, not the original
   coarse per-cue list. Snapping against the coarse cues would've expanded
   an interpolated mid-cue cut back out to the cue's full span and silently
   undone the interpolation fix.

**Verified fix, with real numbers** (job `7b93aa4cd7ef436895213e6fff9365a3`,
the actual 98.5-minute test video, mode=crosstalk):
- Before any of this session's fixes: **10.94 min removed** (kept 87.6 min).
- After fixing #1+#2 alone (before finding #3): still only ~9-10.5 min
  removed — the mega-block bug was silently eating almost all of the
  improvement. This is why "it looks fixed in isolated tests" was
  misleading — always trace the *actual full-pipeline* EDL, not just
  isolated prompt tests, when validating this kind of change.
- After all three fixes: **21.86 min removed** (kept 76.66 min), 111
  sensible keep-ranges (not 16 giant ones, not 586 tiny fragments), largest
  single range 7.78 min and confirmed to be genuine dense real content (a
  competitive-analysis discussion), not another instance of the bug. The
  original complaint (welcome/intro ceremony) is 93.75% removed, up from
  ~0%. Status-update round-robins (structurally similar to introductions but
  semantically real content) are correctly kept.

If you're debugging a similar "the cut list looks wrong" issue in the
future: **don't trust an isolated prompt test alone** — reconstruct the real
segments from the job's cached `transcript.json`, run the actual
`get_decisions()`/`build_edl()` call, and inspect the final `EditDecisionList`
directly. The bug that mattered most here was invisible from the prompt
side and only showed up by tracing the actual EDL construction.

## Other fixes/features this session

- **Gemini response-size resilience** (`decide/gemini_client.py`): segments
  are batched (`gemini_max_segments_per_call`, default 40) so no single
  response has to describe the whole meeting and risk truncating
  mid-JSON (`finish_reason=MAX_TOKENS`). Adaptive: a chunk that still
  truncates splits in half and retries the halves instead of uselessly
  retrying the same oversized request. A chunk that comes back complete but
  invalid (e.g. missing a field on some entries — a real failure mode seen
  in production) gets one same-size retry first, then splits. 429 rate
  limits get an actual sleep-then-retry instead of hammering the same
  window immediately.
- **ffmpeg CPU optimization**: `KEYFRAME_SNAP_TOLERANCE_SECONDS` widened
  0.5s→2s (more cuts qualify for free stream-copy instead of a full
  re-encode) and re-encode preset changed `veryfast`→`ultrafast`
  (`slice/ffmpeg_wrapper.py`). Diagnosed via real server logs: a job with
  94+ EDL ranges was spending 7+ minutes of sustained CPU on re-encodes
  alone.
- **Subprocess timeouts and a real A/V-sync bug fix** (found via an earlier
  background review pass, verified and kept after auditing): `ffmpeg_bin`/
  `ffprobe_bin` calls now have `timeout=` (`ffprobe_timeout_seconds=60`,
  `ffmpeg_timeout_seconds=1800` in `core/config.py`) so a hung process can't
  wedge the single background-task worker forever. Separately,
  `ingest/validate.py`'s A/V-sync check was silently a no-op for files
  (notably `.mkv`) whose ffprobe output has no per-stream `duration` field —
  fixed to fall back to `duration_ts * time_base`, then Matroska's
  `tags.DURATION`, before giving up.
- **Atomic `job.json` writes** (`api/jobs.py`): write-to-temp-then-
  `os.replace()` instead of a direct `write_text()`, so a crash or
  concurrent read mid-write can never see/leave a truncated, unparsable
  job file.
- **Real, measured progress reporting** (not simulated): `JobRecord` gained
  `progress_current`/`progress_total`. `get_decisions()` and
  `render_output()` both accept an `on_progress(current, total)` callback,
  wired through `pipeline.py`, reset to 0/0 at the start of each stage. The
  web UI shows "27 of 151 segments judged (18%)" / "94 of 151 clips
  rendered (62%)" during those two stages specifically — deliberately shows
  *nothing* during transcribing, since there's no reliable way to measure
  WhisperX sub-progress; faking a percentage there would be dishonest.
- **Full frontend redesign** (`web/index.html`): replaced an earlier
  "warm terracotta on dark" theme (which, on reflection, was itself close to
  a known AI-generated-design cliché — near-black + one warm/vermilion
  accent) with **"The Marked Page"** — a manuscript/copy-editor's-desk
  metaphor (red-pencil strikethrough for cuts, blue-pencil for annotations,
  highlighter-yellow wash for keeps), chosen via a 3-way parallel concept
  bake-off (film-editing-bench vs. broadcast-control-room vs.
  manuscript-redline) grounded in the product's real subject matter and
  explicitly checked against known AI-slop tells. Also added a genuinely new
  feature: once a job is done, the page fetches the real transcript +
  real EDL (existing endpoints, no backend changes) and renders the actual
  marked-up transcript client-side — struck-through text for what was cut,
  highlighter wash for what was kept — instead of just linking to raw JSON.
  All existing JS logic (API calls, polling, field names) was preserved
  byte-for-byte where unchanged; verified via live DOM/JS execution in the
  browser, not just visual inspection.
- **Zero-code-change AWS/Docker cost discussion** (no artifacts produced,
  just analysis): minimal viable AWS sizing is CPU-only (no GPU needed —
  verified working), the real constraint is RAM (WhisperX + torch + pyannote
  loaded together, easily several GB) not vCPU count once the user
  confirmed longer processing time is acceptable. Recommended: keep a cheap
  2 vCPU/4GB instance and add a swap file (cheap EBS-backed disk) rather
  than paying for more physical RAM, since slow swap-backed WhisperX runs
  are fine when time isn't the constraint. GPU instances (~$380/mo) were
  explicitly ruled out as disproportionate to the "keep it minimal" goal.

## Known process note from this session (for future awareness, not a current problem)

Earlier in this session, a background code-review Workflow (launched to
*find and verify* issues, read-only by design) went beyond its scope and
started directly editing files. It was caught, stopped
(`TaskStop`), and every change it had already made was individually audited
before being kept (all of it checked out as correct: the timeout/A-V-sync
fixes and atomic-write fix listed above came from that pass). If you launch
review-style workflows in this repo, be explicit that they must only report
findings, never edit files, and periodically check `git status`/`git diff`
against what you actually asked for.

## Environment / how to run

- `.env` (gitignored) has real secrets: `HF_TOKEN` (HuggingFace, only needed
  if WhisperX diarization actually runs — requires accepting gated-model
  terms on 3 separate HF repos, see README), `GEMINI_API_KEY`,
  `GEMINI_MODEL=gemini-3.5-flash-lite` (chosen for availability — bigger
  models 503'd constantly right after release), `WHISPERX_MODEL=small`,
  `WHISPERX_DEVICE=cpu`, `FFMPEG_BIN`/`FFPROBE_BIN` set to absolute winget
  paths (Windows PATH-propagation issue for already-running processes).
- Dev server: `.claude/launch.json` config name `highlight-cutter-api`,
  runs `uvicorn api.main:app` on port 8000, no `--reload` — **you must
  manually stop/restart the preview server after any backend `.py` change**
  for it to take effect; `web/index.html` is read fresh on every request so
  it doesn't need a restart.
- Tests: `pytest` (75 passing as of this session's end), `ruff check .`
  (clean). `tests/conftest.py` isolates `STORAGE_DIR` per test automatically.
- The real 98.5-minute test video/job lives in
  `storage/jobs/7b93aa4cd7ef436895213e6fff9365a3/` — useful for future
  regression testing of the crosstalk prompt/segmentation without needing to
  re-download or re-transcribe (native transcript is already cached in its
  `transcript.json`).

## Not yet implemented (tracked in `POSSIBLE_FEATURES.md`, nothing built)

User-maintained running list of ideas, explicitly not to be implemented
until asked. Currently:
- A short summary/overview of the meeting at the start of the output video.
- On-screen timestamps for users.
- (Claude-suggested, not yet accepted) reviewing/adjusting the AI's
  keep/remove decisions before final render; a job history list on the
  page; using real speaker names when available instead of generic labels.

## Outstanding / worth knowing for next session

- The crosstalk-mode fix has real evidence behind it but was only validated
  on **one** real video. Worth trying on a second, different meeting before
  fully trusting it generalizes.
- Gemini free-tier rate limits (15 req/min) were hit multiple times during
  this session's validation runs on the 98.5-min video (~27 chunks needed);
  the backoff-and-retry logic handled it correctly, but a long/heavily-used
  video will hit this regularly — worth knowing if jobs seem slow on the
  decide stage.
- Highlights mode was reported working well by the user and was not touched
  this session — only crosstalk/light-cleanup mode's prompt changed.
- `web/index.html`'s new marked-transcript feature fetches
  `?clean=false` transcript + EDL client-side and computes kept/cut per word
  — fine for meeting-length transcripts, no virtualization/pagination added
  (not needed at observed scale).
