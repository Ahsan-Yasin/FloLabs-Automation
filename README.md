# Meeting Crosstalk Remover

Given a meeting recording, produces a trimmed video with crosstalk and
low-value overlapping speech removed. Transcript timestamps (WhisperX ASR +
diarization) decide what to cut, a Gemini LLM pass judges which overlaps are
actually low-value, and ffmpeg does the cutting. See the original spec for
the full pipeline design.

## Pipeline

```
Video -> [ingest] -> [transcribe: WhisperX ASR + diarization]
      -> [transcribe.overlap: cheap heuristic overlap flags]
      -> [decide: Gemini keep/remove pass]
      -> [edl: sort/de-overlap/snap-to-word/merge -> EDL]
      -> [slice: ffmpeg extract + concat]
      -> clean output video + remapped transcript
```

Module layout matches the spec's suggested repo structure: `ingest/`,
`transcribe/`, `decide/`, `edl/`, `slice/`, `api/`, plus `core/` for shared
config/models and `pipeline.py` tying the stages together.

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

- `HF_TOKEN` — Hugging Face token with access to `pyannote/speaker-diarization-3.1`
  and `pyannote/segmentation-3.0` (accept each model's terms on Hugging Face
  first, then create a read token). WhisperX diarization is self-hosted and
  free to run; this token is only for downloading the gated model weights.
- `GEMINI_API_KEY` — used for the keep/remove edit-decision pass.
- ffmpeg/ffprobe must be on `PATH` (or set `FFMPEG_BIN`/`FFPROBE_BIN`).

## Running

```bash
uvicorn api.main:app --reload
```

- `POST /jobs` — multipart upload (`file`), starts the pipeline in the
  background, returns a `job_id`.
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
WhisperX and Gemini calls mocked out — it does not require ffmpeg, torch, or
network access. It intentionally doesn't spin up WhisperX or hit the Gemini
API; the golden-recording tests from spec section 9 (clean / crosstalk-heavy
/ silence-only) need real audio fixtures to be added separately once sample
recordings are available.
