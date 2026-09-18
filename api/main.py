import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from core.logging import configure_logging
from core.models import EditMode, JobRecord, JobStatus
from ingest.store import store_transcript, store_video
from pipeline import run_pipeline, run_youtube_pipeline

from .jobs import job_store

configure_logging()

app = FastAPI(title="Meeting Crosstalk Remover / Highlight Cutter")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class YoutubeJobRequest(BaseModel):
    url: str
    mode: EditMode = "highlights"


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/jobs", response_model=JobRecord)
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    mode: EditMode = Form("crosstalk"),  # noqa: B008
    transcript: UploadFile | None = None,
) -> JobRecord:
    try:
        job_id, path = store_video(file.filename or "upload.mp4", file.file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    native_transcript_path = None
    if transcript is not None and transcript.filename:
        try:
            native_transcript_path = str(store_transcript(transcript.filename, transcript.file))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = JobRecord(
        job_id=job_id,
        status=JobStatus.QUEUED,
        mode=mode,
        source_path=str(path),
        native_transcript_path=native_transcript_path,
    )
    job_store.create(job)

    background_tasks.add_task(run_pipeline, job, job_store.update)
    return job


@app.post("/jobs/youtube", response_model=JobRecord)
async def create_youtube_job(background_tasks: BackgroundTasks, body: YoutubeJobRequest) -> JobRecord:
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    job = JobRecord(job_id=uuid.uuid4().hex, status=JobStatus.QUEUED, mode=body.mode, source_url=url)
    job_store.create(job)

    background_tasks.add_task(run_youtube_pipeline, job, job_store.update, url)
    return job


@app.get("/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str) -> JobRecord:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/jobs/{job_id}/video")
async def get_job_video(job_id: str) -> FileResponse:
    job = _require_done(job_id)
    if not job.output_video_path:
        raise HTTPException(status_code=404, detail="output video not available")
    return FileResponse(job.output_video_path, media_type="video/mp4")


@app.get("/jobs/{job_id}/edl")
async def get_job_edl(job_id: str) -> FileResponse:
    job = _require_done(job_id)
    if not job.edl_path:
        raise HTTPException(status_code=404, detail="EDL not available")
    return FileResponse(job.edl_path, media_type="application/json")


@app.get("/jobs/{job_id}/transcript")
async def get_job_transcript(job_id: str, clean: bool = True) -> FileResponse:
    job = _require_done(job_id)
    path = job.clean_transcript_path if clean else job.transcript_path
    if not path:
        raise HTTPException(status_code=404, detail="transcript not available")
    return FileResponse(path, media_type="application/json")


def _require_done(job_id: str) -> JobRecord:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(status_code=409, detail=f"job is not done yet (status={job.status.value})")
    return job
