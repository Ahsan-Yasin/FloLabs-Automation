from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core.logging import configure_logging
from core.models import JobRecord, JobStatus
from ingest.store import store_video
from pipeline import run_pipeline

from .jobs import job_store

configure_logging()

app = FastAPI(title="Meeting Crosstalk Remover")


@app.post("/jobs", response_model=JobRecord)
async def create_job(background_tasks: BackgroundTasks, file: UploadFile) -> JobRecord:
    try:
        job_id, path = store_video(file.filename or "upload.mp4", file.file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = JobRecord(job_id=job_id, status=JobStatus.QUEUED, source_path=str(path))
    job_store.create(job)

    background_tasks.add_task(run_pipeline, job, job_store.update)
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
    return FileResponse(job.output_video_path)


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
