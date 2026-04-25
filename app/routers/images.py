import os
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.database import get_db
from app.models import ImageJob, JobStatus, OutputFormat
from app.schemas import JobListResponse, JobResponse, UploadResponse
from app.workers.tasks import process_image_task

router = APIRouter(prefix="/images", tags=["images"])

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/bmp",
    "image/tiff",
    "image/webp",
    "image/avif",
}


def _delete_files(*paths: str | None) -> None:
    for path in paths:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


@router.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_images(
    files: Annotated[list[UploadFile], File(description="One or more image files")],
    format: OutputFormat = Query(OutputFormat.WEBP, description="Output format"),
    width: int | None = Query(
        None, gt=0, le=10000, description="Target width in pixels; aspect ratio is preserved"
    ),
    db: Session = Depends(get_db),
):
    if not files:
        raise HTTPException(400, "No files provided")
    if len(files) > 20:
        raise HTTPException(400, "Maximum 20 files per request")

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    jobs: list[ImageJob] = []

    for file in files:
        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(400, f"Unsupported file type: {file.content_type}")

        content = await file.read()
        size = len(content)

        if size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(
                400, f"File '{file.filename}' exceeds {settings.MAX_FILE_SIZE_MB} MB limit"
            )

        job_id = str(uuid.uuid4())
        ext = Path(file.filename or "image").suffix.lower() or ".jpg"
        upload_path = settings.UPLOAD_DIR / f"{job_id}{ext}"

        upload_path.write_bytes(content)

        job = ImageJob(
            id=job_id,
            original_filename=file.filename or "upload",
            original_path=str(upload_path),
            status=JobStatus.PENDING,
            output_format=format,
            resize_width=width,
            original_size_bytes=size,
        )
        db.add(job)
        db.flush()
        jobs.append(job)

    db.commit()

    # Dispatch Celery tasks after commit so workers see the DB records
    for job in jobs:
        process_image_task.delay(job.id)
        db.refresh(job)

    return UploadResponse(
        jobs=[JobResponse.model_validate(j) for j in jobs],
        total=len(jobs),
    )


@router.get("/status/{job_id}", response_model=JobResponse)
def get_status(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ImageJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JobResponse.model_validate(job)


@router.get("/download/{job_id}")
def download_image(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ImageJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job.status == JobStatus.FAILED:
        raise HTTPException(422, f"Processing failed: {job.error_message}")
    if job.status == JobStatus.DOWNLOADED:
        raise HTTPException(410, "File already downloaded and deleted")
    if job.status == JobStatus.EXPIRED:
        raise HTTPException(410, "File expired and deleted")
    if job.status != JobStatus.READY:
        raise HTTPException(425, f"Not ready yet — current status: {job.status}")

    if not job.processed_path or not os.path.exists(job.processed_path):
        raise HTTPException(404, "Processed file not found")

    from datetime import datetime

    processed_path = job.processed_path
    original_path = job.original_path  # may already be None if deleted by worker

    job.status = JobStatus.DOWNLOADED
    job.downloaded_at = datetime.utcnow()
    db.commit()

    ext = Path(processed_path).suffix
    filename = f"{Path(job.original_filename).stem}{ext}"

    return FileResponse(
        processed_path,
        filename=filename,
        background=BackgroundTask(_delete_files, processed_path, original_path),
    )


@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    total = db.query(ImageJob).count()
    jobs = (
        db.query(ImageJob)
        .order_by(ImageJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return JobListResponse(
        jobs=[JobResponse.model_validate(j) for j in jobs],
        total=total,
    )
