# Highlight Cutter

Given a YouTube link or an uploaded recording, produces a trimmed video —
either an aggressive highlight reel or a light crosstalk-only cleanup.
Transcript timestamps (WhisperX ASR + diarization) decide candidate cuts, a
Gemini LLM pass judges what's actually worth keeping, and ffmpeg does the
cutting. Includes a small web UI. See the original spec for the full
pipeline design this was built from.

## Pipeline

```
YouTube URL / uploaded file -> [ingest] -> [transcribe: WhisperX ASR + diarization]
      -> [transcribe.overlap: cheap heuristic overlap flags]
      -> [decide: Gemini keep/remove pass — "highlights" or "crosstalk" mode]
      -> [edl: sort/de-overlap/snap-to-word/merge -> EDL]
      -> [slice: ffmpeg extract + concat]
      -> clean output video + remapped transcript
```

Module layout matches the spec's suggested repo structure: `ingest/`,
`transcribe/`, `decide/`, `edl/`, `slice/`, `api/`, plus `core/` for shared
config/models, `pipeline.py` tying the stages together, and `web/` for the UI.

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
- `GEMINI_API_KEY` — used for the keep/remove edit-decision pass.
- `GEMINI_MODEL` — defaults to `gemini-3.5-flash-lite`. Google's newest
  flagship "flash" models are prone to `503 UNAVAILABLE` (high demand) right
  after release; the `-lite` tier tends to be far more available. `decide`
  already retries once on a transient API error before failing the job.
- ffmpeg/ffprobe must be on `PATH` (or set `FFMPEG_BIN`/`FFPROBE_BIN` to
  absolute paths — useful if you just installed ffmpeg and the running
  process's PATH hasn't picked it up yet; `core.config` also injects that
  directory onto `PATH` for the current process so libraries like WhisperX
  that shell out to a bare `ffmpeg` still find it).

## Running

```bash
uvicorn api.main:app --reload
```

Open `http://localhost:8000` for the web UI (paste a YouTube URL or drop a
file, pick Highlights vs. Light cleanup, watch it process, preview/download
the result). Or drive it directly:

- `POST /jobs` — multipart upload (`file`, optional `mode` form field:
  `highlights` | `crosstalk`), starts the pipeline in the background, returns
  a `job_id`.
- `POST /jobs/youtube` — JSON body `{"url": "...", "mode": "highlights"}`,
  downloads via yt-dlp then runs the same pipeline.
- `GET /jobs/{job_id}` — status/progress/paths.
- `GET /jobs/{job_id}/video` — the trimmed output (once `status == done`).
- `GET /jobs/{job_id}/edl` — the Edit Decision List used to produce it.
- `GET /jobs/{job_id}/transcript?clean=true` — transcript remapped onto the
  trimmed timeline (`clean=false` for the original, untrimmed transcript).

n8n (or any orchestrator) calls this API over HTTP per section 6 of the spec
— it should not run ffmpeg itself.

## Tests

```bash
pytest
```

The test suite covers the deterministic logic (overlap detection, EDL
sorting/de-overlap/snapping/merging, transcript remapping, API wiring) with
WhisperX, yt-dlp, and Gemini calls mocked out — it does not require ffmpeg,
torch, or network access. The full pipeline has also been verified end to
end against a real YouTube video with real WhisperX transcription/diarization
and a real Gemini call. The golden-recording tests from spec section 9
(clean / crosstalk-heavy / silence-only) still need real audio fixtures to be
added separately once sample recordings are available.
