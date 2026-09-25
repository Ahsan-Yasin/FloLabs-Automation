import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache, partial
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from pydantic import BaseModel

from core.config import get_settings
from core.logging import configure_logging, get_logger
from core.models import TERMINAL_STATUSES, JobOptions, JobRecord, JobStatus
from core.proc import run_checked
from core.version import PIPELINE_VERSION
from ingest.store import store_transcript, store_video
from pipeline import run_pipeline, run_youtube_pipeline

from .auth import auth_enabled, require_api_key
from .jobs import job_store
from .queue import (
    JobQueue,
    QueueFull,
    delete_job_files,
    is_protected,
    reconcile_on_startup,
)

configure_logging()
logger = get_logger(__name__)

job_queue = JobQueue(job_store)
BUSY_RETRY_AFTER_S = 60


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not auth_enabled():
        logger.warning("HC_API_TOKEN is empty: the API is open (fine locally, never on a public host)")
    reconcile_on_startup(job_store)
    yield


app = FastAPI(
    title="Meeting Crosstalk Remover / Highlight Cutter",
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)],
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_STARTED_AT = time.monotonic()


@lru_cache
def _ffmpeg_version() -> str | None:
    try:
        out = run_checked([get_settings().ffmpeg_bin, "-hide_banner", "-version"], timeout=15).stdout
    except Exception:  # noqa: BLE001 — health must answer even when ffmpeg is missing
        return None
    first = out.splitlines()[0] if out else ""
    return first.removeprefix("ffmpeg version ").split(" ")[0] or None


class YoutubeJobRequest(BaseModel):
    url: str
    options: JobOptions = JobOptions()


def _busy_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(BUSY_RETRY_AFTER_S)},
        content={
            "error_code": "busy",
            "retryable": True,
            "retry_after_s": BUSY_RETRY_AFTER_S,
            "running_job_id": job_queue.running_job_id,
            "queue_depth": job_queue.depth(),
        },
    )


def _has_capacity() -> bool:
    max_depth = get_settings().max_queue_depth
    return max_depth <= 0 or job_queue.depth() < max_depth


def _enqueue(job: JobRecord, runner, *args) -> JSONResponse | JobRecord:
    job_store.create(job)
    try:
        job_queue.submit(job.job_id, partial(runner, job, job_queue.make_updater(job.job_id), *args))
    except QueueFull:
        delete_job_files(job_store, job.job_id)
        return _busy_response()
    return job


@app.get("/health")
async def health() -> dict:
    """Liveness/readiness probe for n8n (no auth, no job details beyond ids)."""
    settings = get_settings()
    try:
        disk_free = shutil.disk_usage(settings.storage_dir).free
    except OSError:
        disk_free = None
    return {
        "status": "ok",
        "version": PIPELINE_VERSION,
        "ffmpeg_version": _ffmpeg_version(),
        "disk_free_bytes": disk_free,
        "busy": job_queue.is_busy(),
        "running_job_id": job_queue.running_job_id,
        "queue_depth": job_queue.depth(),
        "auth_enabled": auth_enabled(),
        "uptime_s": round(time.monotonic() - _STARTED_AT, 1),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/app", response_class=HTMLResponse)
async def app_page() -> str:
    return (WEB_DIR / "app.html").read_text(encoding="utf-8")


@app.get("/styles.css")
async def styles() -> FileResponse:
    return FileResponse(WEB_DIR / "styles.css", media_type="text/css")


@app.post("/jobs", response_model=JobRecord)
async def create_job(
    file: UploadFile,
    transcript: UploadFile | None = None,
    decide_only: bool = Form(False),
    transitions: bool | None = Form(None),
    highlights_target_s: float | None = Form(None),
    shorts_count: int | None = Form(None),
    highlights_criteria: str | None = Form(None),
    shorts_criteria: str | None = Form(None),
):
    """Hidden upload path (ops/regression); production jobs come from Zoom."""
    try:
        options = JobOptions(
            decide_only=decide_only,
            transitions=transitions,
            highlights_target_s=highlights_target_s,
            shorts_count=shorts_count,
            highlights_criteria=(highlights_criteria or "").strip() or None,
            shorts_criteria=(shorts_criteria or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not _has_capacity():
        return _busy_response()
    job_id = uuid.uuid4().hex
    try:
        _, path = store_video(file.filename or "upload.mp4", file.file, job_id=job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    native_transcript_path = None
    if transcript is not None and transcript.filename:
        try:
            native_transcript_path = str(store_transcript(transcript.filename, transcript.file, job_id=job_id))
        except ValueError as exc:
            delete_job_files(job_store, job_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = JobRecord(
        job_id=job_id,
        status=JobStatus.QUEUED,
        options=options,
        source_path=str(path),
        native_transcript_path=native_transcript_path,
    )
    return _enqueue(job, run_pipeline)


@app.post("/jobs/youtube", response_model=JobRecord)
async def create_youtube_job(body: YoutubeJobRequest):
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if not _has_capacity():
        return _busy_response()

    job = JobRecord(job_id=uuid.uuid4().hex, status=JobStatus.QUEUED, options=body.options, source_url=url)
    return _enqueue(job, run_youtube_pipeline, url)


@app.post("/jobs/{job_id}/render", response_model=JobRecord)
async def render_decided_job(job_id: str):
    """Render a decide_only job with its saved decisions and selection (no new
    AI calls unless the saved ones no longer match)."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.DECIDED:
        raise HTTPException(status_code=409, detail=f"only a decided job can be rendered (status={job.status.value})")
    if not _has_capacity():
        return _busy_response()
    job.options = job.options.model_copy(update={"decide_only": False})
    job.status = JobStatus.QUEUED
    runner = run_youtube_pipeline if job.source_url else run_pipeline
    args = (job.source_url,) if job.source_url else ()
    job_store.update(job)
    try:
        job_queue.submit(job.job_id, partial(runner, job, job_queue.make_updater(job.job_id), *args))
    except QueueFull:
        job.status = JobStatus.DECIDED
        job_store.update(job)
        return _busy_response()
    return job


@app.get("/jobs")
async def list_jobs(limit: int = 50) -> list[dict]:
    """Job history, newest first (ids, status, timestamps, error codes)."""
    jobs = []
    for job_id in job_store.list_ids():
        try:
            job = job_store.get(job_id)
        except Exception as exc:  # noqa: BLE001 — one unreadable job.json must not break the list
            logger.warning("skipping unreadable job %s in GET /jobs: %s", job_id, exc)
            continue
        if job is not None:
            if job.created_at is None:  # legacy job.json without timestamps
                stamp = (get_settings().jobs_dir / job_id / "job.json").stat().st_mtime
                job = job.model_copy(update={"created_at": datetime.fromtimestamp(stamp, UTC)})
            jobs.append(job)
    epoch = datetime.min.replace(tzinfo=UTC)
    jobs.sort(key=lambda j: _aware(j.created_at) or epoch, reverse=True)
    return [
        {
            "job_id": j.job_id,
            "status": j.status.value,
            "created_at": j.created_at,
            "updated_at": j.updated_at,
            "error_code": j.error_code,
            "stale": _is_stale(j),
        }
        for j in jobs[: max(1, limit)]
    ]


@app.get("/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str) -> JobRecord:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.model_copy(update={"stale": _is_stale(job)})


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, force: bool = False):
    """Queued -> removed and wiped. Running -> 409 unless ?force=true, which
    cancels it at the next checkpoint and wipes it afterwards. Finished ->
    wiped. A job folder containing `.keep` is protected."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if is_protected(job_id):
        raise HTTPException(status_code=409, detail={"error_code": "protected", "message": "job has a .keep marker"})

    if job.status not in TERMINAL_STATUSES and job_queue.contains(job_id):
        if job_queue.running_job_id == job_id:
            if not force:
                raise HTTPException(
                    status_code=409,
                    detail={"error_code": "running", "message": "job is running; use ?force=true to cancel it"},
                )
            job_queue.cancel(job_id, delete_after=True)
            return JSONResponse(status_code=202, content={"job_id": job_id, "cancelling": True})
        job_queue.cancel(job_id)
    delete_job_files(job_store, job_id)
    return {"job_id": job_id, "deleted": True}


@app.get("/jobs/{job_id}/log", response_class=PlainTextResponse)
async def get_job_log(job_id: str) -> str:
    if job_store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    path = get_settings().jobs_dir / job_id / "job.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


@app.get("/jobs/{job_id}/video")
async def get_job_video(job_id: str) -> FileResponse:
    job = _require_done(job_id)
    if not job.output_video_path:
        raise HTTPException(status_code=404, detail="output video not available")
    return FileResponse(job.output_video_path, media_type="video/mp4")


@app.get("/jobs/{job_id}/edl")
async def get_job_edl(job_id: str) -> FileResponse:
    job = _require_done(job_id, allow_decided=True)
    if not job.edl_path:
        raise HTTPException(status_code=404, detail="EDL not available")
    return FileResponse(job.edl_path, media_type="application/json")


@app.get("/jobs/{job_id}/removed")
async def get_job_removed(job_id: str) -> FileResponse:
    job = _require_done(job_id, allow_decided=True)
    if not job.removed_edl_path:
        raise HTTPException(status_code=404, detail="removed list not available")
    return FileResponse(job.removed_edl_path, media_type="application/json")


@app.get("/jobs/{job_id}/selection")
async def get_job_selection(job_id: str) -> FileResponse:
    """Candidate moments (scores, titles, hooks, source times) and which went
    into the highlights reel and the shorts."""
    job = _require_done(job_id, allow_decided=True)
    if not job.selection_path:
        raise HTTPException(status_code=404, detail="selection not available")
    return FileResponse(job.selection_path, media_type="application/json")


@app.get("/jobs/{job_id}/chapters", response_class=PlainTextResponse)
async def get_job_chapters(job_id: str) -> str:
    """YouTube chapter lines for the output video (paste into the description)."""
    job = _require_done(job_id)
    if not job.chapters_path or not Path(job.chapters_path).exists():
        raise HTTPException(status_code=404, detail="chapters not available")
    return Path(job.chapters_path).read_text(encoding="utf-8")


@app.get("/jobs/{job_id}/transcript")
async def get_job_transcript(job_id: str, clean: bool = True) -> FileResponse:
    job = _require_done(job_id, allow_decided=True)
    path = job.clean_transcript_path if clean else job.transcript_path
    if not path:
        raise HTTPException(status_code=404, detail="transcript not available")
    return FileResponse(path, media_type="application/json")


def _aware(stamp: datetime | None) -> datetime | None:
    if stamp is not None and stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp


def _is_stale(job: JobRecord) -> bool:
    if job.status in TERMINAL_STATUSES or job.status == JobStatus.QUEUED:
        return False
    stamp = _aware(job.updated_at)
    if stamp is None:
        return False
    return (datetime.now(UTC) - stamp).total_seconds() > get_settings().stale_after_s


def _require_done(job_id: str, allow_decided: bool = False) -> JobRecord:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.DONE and not (allow_decided and job.status == JobStatus.DECIDED):
        raise HTTPException(status_code=409, detail=f"job is not done yet (status={job.status.value})")
    return job
