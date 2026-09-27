import hashlib
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache, partial
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core.config import get_settings, settings_problems
from core.errors import PipelineError
from core.logging import configure_logging, get_logger
from core.models import TERMINAL_STATUSES, JobOptions, JobRecord, JobStatus
from core.proc import run_checked
from core.version import PIPELINE_VERSION
from db.session import check_db, get_engine, session_scope
from ingest import zoom
from ingest.store import store_transcript, store_video, title_from_filename
from pipeline import run_pipeline, run_youtube_pipeline, run_zoom_pipeline
from services import users as users_service

from .auth import AuthMiddleware, auth_enabled
from .errors import install_error_handlers
from .jobs import is_valid_job_id, job_store
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


def _startup_database() -> None:
    """Create/upgrade the product database and promote ADMIN_EMAILS users
    who have verified their address."""
    get_engine()
    with session_scope() as db:
        promoted = users_service.promote_configured_admins(db)
    if promoted:
        logger.info("promoted %d verified user(s) listed in ADMIN_EMAILS to admin", promoted)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    problems = settings_problems(settings)
    if settings.is_prod and problems:
        bullet = "\n  - "
        raise RuntimeError(f"refusing to start with APP_ENV=prod:{bullet}{bullet.join(problems)}")
    if not auth_enabled():
        logger.warning("DEV_OPEN_API=true and no HC_API_TOKEN: API calls without credentials are accepted "
                       "(local development only)")
    await run_in_threadpool(_startup_database)
    reconcile_on_startup(job_store)
    yield


# No /docs, /redoc or /openapi.json: they map every route for anyone who can
# reach the host, and nothing uses them (n8n and the web UI call fixed routes).
app = FastAPI(
    title="Meeting Crosstalk Remover / Highlight Cutter",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(AuthMiddleware)
install_error_handlers(app)

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


class ZoomJobRequest(BaseModel):
    # the meeting INSTANCE uuid from the recordings list or Zoom's webhook
    meeting_uuid: str
    options: JobOptions = JobOptions()


# The recordings picker lists at most this many days per request (every 30
# days is one more Zoom call).
ZOOM_MAX_LIST_DAYS = 366
# Serialises "is this meeting already a job?" with creating the job, so two
# identical POST /jobs/zoom calls (an n8n retry) can't both create one.
_zoom_submit_lock = threading.Lock()


def _error_response(
    status_code: int, error_code: str, message: str, retryable: bool = False, retry_after_s: int | None = None
) -> JSONResponse:
    """Same top-level shape as the busy answer, with the message in `detail`
    where the other error answers keep theirs."""
    return JSONResponse(
        status_code=status_code,
        headers={"Retry-After": str(retry_after_s)} if retry_after_s else None,
        content={"error_code": error_code, "retryable": retryable, "retry_after_s": retry_after_s,
                 "detail": message},
    )


def _zoom_error(exc: PipelineError) -> JSONResponse:
    """Zoom not set up here -> 400; unknown host/meeting -> 404; Zoom refused
    our credentials or failed -> 502."""
    if isinstance(exc, zoom.ZoomNotConfigured):
        status_code = 400
    elif exc.code == "zoom_not_found":
        status_code = 404
    else:
        status_code = 502
    return _error_response(status_code, exc.code, str(exc), exc.retryable, exc.retry_after_s)


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
        "db": "ok" if await run_in_threadpool(check_db) else "error",
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
def create_job(
    file: UploadFile,
    transcript: UploadFile | None = None,
    decide_only: bool = Form(False),
    transitions: bool | None = Form(None),
    highlights_target_s: float | None = Form(None),
    shorts_count: int | None = Form(None),
    highlights_criteria: str | None = Form(None),
    shorts_criteria: str | None = Form(None),
    title: str | None = Form(None),
    cut_silence: bool | None = Form(None),
):
    """Hidden upload path (ops/regression); production jobs come from Zoom.
    `title` (the meeting name on the title card and in the report) defaults
    to the uploaded file's name.

    A plain `def` on purpose: FastAPI runs it in the threadpool, so copying a
    multi-GB upload into the job folder doesn't stall the event loop (and with
    it /health and every job poll) for the whole copy."""
    try:
        options = JobOptions(
            decide_only=decide_only,
            transitions=transitions,
            highlights_target_s=highlights_target_s,
            shorts_count=shorts_count,
            highlights_criteria=(highlights_criteria or "").strip() or None,
            shorts_criteria=(shorts_criteria or "").strip() or None,
            cut_silence=cut_silence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not _has_capacity():
        return _busy_response()
    job_id = uuid.uuid4().hex
    native_transcript_path = None
    try:
        _, path = store_video(file.filename or "upload.mp4", file.file, job_id=job_id)
        if transcript is not None and transcript.filename:
            native_transcript_path = str(store_transcript(transcript.filename, transcript.file, job_id=job_id))
    except Exception as exc:
        # Any failure here (a bad extension, or an OSError such as a full disk
        # halfway through a multi-GB copy) happens after the job folder was
        # created and before a job.json exists, so nothing else would ever
        # find or delete that half-written folder.
        delete_job_files(job_store, job_id)
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise

    job = JobRecord(
        job_id=job_id,
        status=JobStatus.QUEUED,
        options=options,
        title=" ".join((title or "").split())[:120] or title_from_filename(file.filename or ""),
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


@app.get("/zoom/status")
async def zoom_status() -> dict:
    """Whether Zoom credentials are set (never the credentials themselves)."""
    settings = get_settings()
    return {
        "configured": zoom.is_configured(settings),
        "host_email": settings.zoom_host_email or None,
        "require_native_transcript": settings.require_native_transcript,
    }


@app.get("/zoom/recordings")
def list_zoom_recordings(
    host: str | None = None,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    """A host's cloud recordings (default: ZOOM_HOST_EMAIL, the last 30 days),
    newest first, with what the picker needs to show and whether each one can
    be processed now. A plain `def`: Zoom calls are blocking HTTP."""
    host_email = (host or get_settings().zoom_host_email).strip()
    if not host_email:
        raise HTTPException(status_code=400, detail="no host email: pass ?host= or set ZOOM_HOST_EMAIL in .env")
    try:
        end = date.fromisoformat(to_date) if to_date else datetime.now(UTC).date()
        start = date.fromisoformat(from_date) if from_date else end - timedelta(days=29)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="from/to must be dates like 2026-09-01") from exc
    if start > end:
        raise HTTPException(status_code=400, detail="'from' is after 'to'")
    if (end - start).days >= ZOOM_MAX_LIST_DAYS:
        raise HTTPException(status_code=400, detail=f"list at most {ZOOM_MAX_LIST_DAYS} days at a time")
    try:
        meetings = zoom.get_client().list_recordings(host_email, start, end)
    except PipelineError as exc:
        return _zoom_error(exc)
    return {"host_email": host_email, "from": start.isoformat(), "to": end.isoformat(), "meetings": meetings}


@app.post("/jobs/zoom")
def create_zoom_job(body: ZoomJobRequest):
    """Process one Zoom meeting instance. 425 while Zoom is still processing
    the recording or its transcript (retry after `retry_after_s`). The same
    meeting with the same options returns the job that already exists, with
    "deduplicated": true, unless that one failed or was cancelled — so an n8n
    retry never processes a meeting twice. Then the usual 503 when busy.

    A plain `def`: the readiness check is a blocking Zoom call."""
    meeting_uuid = body.meeting_uuid.strip()
    if not meeting_uuid:
        raise HTTPException(status_code=400, detail="meeting_uuid is required")
    try:
        meeting = zoom.get_client().get_meeting(meeting_uuid)
    except PipelineError as exc:
        return _zoom_error(exc)
    choice = zoom.choose_files(meeting)
    readiness = zoom.readiness(meeting, choice)
    # "no_transcript" is only a problem when this server can't transcribe itself
    if readiness != "ready" and not (readiness == "no_transcript" and not get_settings().require_native_transcript):
        exc = zoom.not_ready_error(readiness)
        return _error_response(425 if exc.retryable else 422, exc.code, str(exc), exc.retryable, exc.retry_after_s)

    options_hash = hashlib.sha256(body.options.model_dump_json().encode()).hexdigest()[:16]
    with _zoom_submit_lock:
        existing = _find_zoom_job(meeting_uuid, options_hash)
        if existing is not None:
            return _zoom_job_response(existing, deduplicated=True)
        if not _has_capacity():
            return _busy_response()
        info = zoom.meeting_info(meeting, choice)
        job = JobRecord(
            job_id=uuid.uuid4().hex,
            status=JobStatus.QUEUED,
            options=body.options,
            title=" ".join(info["topic"].split())[:120],
            zoom_meeting_uuid=meeting_uuid,
            zoom_options_hash=options_hash,
            zoom_meeting=info,
        )
        created = _enqueue(job, run_zoom_pipeline)
    if isinstance(created, JSONResponse):
        return created
    return _zoom_job_response(created, deduplicated=False)


def _find_zoom_job(meeting_uuid: str, options_hash: str) -> JobRecord | None:
    for job_id in job_store.list_ids():
        try:
            job = job_store.get(job_id)
        except Exception as exc:  # noqa: BLE001 — one unreadable job.json must not block new jobs
            logger.warning("skipping unreadable job %s in the Zoom dedupe check: %s", job_id, exc)
            continue
        if (job is not None and job.zoom_meeting_uuid == meeting_uuid and job.zoom_options_hash == options_hash
                and job.status not in (JobStatus.FAILED, JobStatus.CANCELLED)):
            return job
    return None


def _zoom_job_response(job: JobRecord, deduplicated: bool) -> JSONResponse:
    return JSONResponse(content={**job.model_dump(mode="json"), "deduplicated": deduplicated})


@app.post("/jobs/{job_id}/render", response_model=JobRecord)
async def render_decided_job(job_id: str):
    """Render a decide_only job, or re-render one whose render failed or was
    interrupted, with its saved decisions and selection (no new AI calls
    unless the saved ones no longer match).

    Stays `async` with no await between the status check and the submit, so
    two clicks (or a DELETE) can't interleave and queue the job twice."""
    job = _get_job_or_404(job_id)
    if not _can_render(job):
        raise HTTPException(
            status_code=409,
            detail=(
                "only a decided job, or a retryable failed job with saved picks, can be rendered "
                f"(status={job.status.value})"
            ),
        )
    if not _has_capacity():
        return _busy_response()
    previous = job.model_copy(deep=True)
    job.options = job.options.model_copy(update={"decide_only": False})
    job.status = JobStatus.QUEUED
    # A re-rendered failed job must not look failed while it waits in the queue.
    job.error = job.error_code = job.retry_after_s = None
    job.retryable = False
    # the Zoom/YouTube runners fetch the source again when it was deleted
    # after delivery (delete_source_when_done)
    if job.zoom_meeting_uuid:
        runner, args = run_zoom_pipeline, ()
    elif job.source_url:
        runner, args = run_youtube_pipeline, (job.source_url,)
    else:
        runner, args = run_pipeline, ()
    job_store.update(job)
    try:
        job_queue.submit(job.job_id, partial(runner, job, job_queue.make_updater(job.job_id), *args))
    except QueueFull:
        job_store.update(previous)
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
    job = _get_job_or_404(job_id)
    return job.model_copy(update={"stale": _is_stale(job)})


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, force: bool = False):
    """Queued -> removed and wiped. Running -> 409 unless ?force=true, which
    cancels it at the next checkpoint and wipes it afterwards. Finished ->
    wiped. A job folder containing `.keep` is protected."""
    job = _get_job_or_404(job_id)
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
    _get_job_or_404(job_id)
    path = get_settings().jobs_dir / job_id / "job.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


@app.get("/jobs/{job_id}/video")
async def get_job_video(job_id: str) -> FileResponse:
    """final.mp4 (highlights reel, title card, cleaned meeting)."""
    job = _require_done(job_id)
    if not job.output_video_path:
        raise HTTPException(status_code=404, detail="output video not available")
    return _file_or_gone(Path(job.output_video_path), "video/mp4")


@app.get("/jobs/{job_id}/bundle")
async def get_job_bundle(job_id: str) -> FileResponse:
    """bundle.zip with every deliverable and manifest.json (Range requests
    are supported, so a dropped download can resume)."""
    job = _require_done(job_id)
    if not job.bundle_path:
        raise HTTPException(status_code=404, detail="bundle not available")
    return _file_or_gone(Path(job.bundle_path), "application/zip", filename=f"{_slug(job)}.zip")


@app.get("/jobs/{job_id}/artifacts/{name:path}")
async def get_job_artifact(job_id: str, name: str, download: bool = False) -> FileResponse:
    """One deliverable by its name in job.artifacts ("final.mp4",
    "shorts/short_01.mp4", "report.pdf", ...). Only names the job itself
    lists are served, so no other path under the job folder is reachable."""
    job = _require_done(job_id)
    info = job.artifacts.get(name)
    if info is None or info.status != "ok":
        raise HTTPException(status_code=404, detail=f"no artifact {name!r}")
    if not info.on_disk:
        raise HTTPException(status_code=410, detail=f"{name} is only in bundle.zip (individual files were removed)")
    path = get_settings().jobs_dir / job_id / info.path
    filename = f"{_slug(job)}_{Path(info.path).name}" if download else None
    return _file_or_gone(path, _MEDIA_TYPES.get(Path(info.path).suffix.lower(), "application/octet-stream"),
                         filename=filename)


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
    into the highlights reel and the shorts. Also served for a failed job that
    can be re-rendered, so the UI can show its picks with "Render now" again."""
    job = _get_job_or_404(job_id)
    if job.status != JobStatus.DONE and not _can_render(job):
        raise HTTPException(status_code=409, detail=f"job is not done yet (status={job.status.value})")
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


_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".srt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".zip": "application/zip",
}


def _file_or_gone(path: Path, media_type: str, filename: str | None = None) -> FileResponse:
    """A missing file (deleted after bundling on EC2, or by hand) is a clear
    410, not a 500 from inside the response."""
    if not path.is_file():
        raise HTTPException(status_code=410, detail=f"{path.name} is no longer on disk")
    return FileResponse(path, media_type=media_type, filename=filename)


def _slug(job: JobRecord) -> str:
    name = "".join(c if c.isalnum() else "_" for c in (job.title or "meeting").lower()).strip("_")
    name = "_".join(filter(None, name.split("_")))[:60] or "meeting"
    return f"{name}_{job.job_id[:8]}"


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


def _get_job_or_404(job_id: str) -> JobRecord:
    """Every /jobs/{job_id} route starts here, so only a plain job id ever
    reaches the filesystem (see api.jobs.is_valid_job_id): "<id>." used to
    let DELETE find a running job under a name the queue didn't know, skip the
    running-job guard and wipe its folder."""
    job = job_store.get(job_id) if is_valid_job_id(job_id) else None
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _can_render(job: JobRecord) -> bool:
    """A decided job, or one whose render failed or was interrupted after its
    picks were saved: decisions and selection are on disk, so rendering again
    needs no new AI calls. A non-retryable failure would only fail again."""
    if job.status == JobStatus.DECIDED:
        return True
    return job.status == JobStatus.FAILED and job.retryable and bool(job.selection_path and job.decisions_path)


def _require_done(job_id: str, allow_decided: bool = False) -> JobRecord:
    job = _get_job_or_404(job_id)
    if job.status != JobStatus.DONE and not (allow_decided and job.status == JobStatus.DECIDED):
        raise HTTPException(status_code=409, detail=f"job is not done yet (status={job.status.value})")
    return job


# ---------------------------------------------------------------- product API (/api/v1)
from .routers import auth as auth_routes

app.include_router(auth_routes.router, prefix="/api/v1")
