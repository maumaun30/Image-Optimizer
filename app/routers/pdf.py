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
from app.models import CompressionLevel, JobStatus, PdfJob
from app.schemas import PdfJobListResponse, PdfJobResponse, PdfUploadResponse
from app.workers.tasks import process_pdf_task

router = APIRouter(prefix="/pdf", tags=["pdf"])

ALLOWED_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}


def _delete_files(*paths: str | None) -> None:
    for path in paths:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


@router.post("/upload", response_model=PdfUploadResponse, status_code=202)
async def upload_pdfs(
    files: Annotated[list[UploadFile], File(description="One or more PDF files")],
    level: CompressionLevel = Query(
        CompressionLevel.EBOOK, description="Compression preset"
    ),
    db: Session = Depends(get_db),
):
    if not files:
        raise HTTPException(400, "No files provided")
    if len(files) > 20:
        raise HTTPException(400, "Maximum 20 files per request")

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    jobs: list[PdfJob] = []

    for file in files:
        is_pdf = file.content_type in ALLOWED_CONTENT_TYPES or (
            file.filename or ""
        ).lower().endswith(".pdf")
        if not is_pdf:
            raise HTTPException(400, f"Unsupported file type: {file.content_type}")

        content = await file.read()
        size = len(content)

        if size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(
                400, f"File '{file.filename}' exceeds {settings.MAX_FILE_SIZE_MB} MB limit"
            )

        job_id = str(uuid.uuid4())
        upload_path = settings.UPLOAD_DIR / f"{job_id}.pdf"
        upload_path.write_bytes(content)

        job = PdfJob(
            id=job_id,
            original_filename=file.filename or "upload.pdf",
            original_path=str(upload_path),
            status=JobStatus.PENDING,
            compression_level=level,
            original_size_bytes=size,
        )
        db.add(job)
        db.flush()
        jobs.append(job)

    db.commit()

    # Dispatch Celery tasks after commit so workers see the DB records
    for job in jobs:
        process_pdf_task.delay(job.id)
        db.refresh(job)

    return PdfUploadResponse(
        jobs=[PdfJobResponse.model_validate(j) for j in jobs],
        total=len(jobs),
    )


@router.get("/status/{job_id}", response_model=PdfJobResponse)
def get_status(job_id: str, db: Session = Depends(get_db)):
    job = db.get(PdfJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return PdfJobResponse.model_validate(job)


@router.get("/download/{job_id}")
def download_pdf(job_id: str, db: Session = Depends(get_db)):
    job = db.get(PdfJob, job_id)
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

    filename = f"{Path(job.original_filename).stem}.pdf"

    return FileResponse(
        processed_path,
        filename=filename,
        media_type="application/pdf",
        background=BackgroundTask(_delete_files, processed_path, original_path),
    )


@router.get("/jobs", response_model=PdfJobListResponse)
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    total = db.query(PdfJob).count()
    jobs = (
        db.query(PdfJob)
        .order_by(PdfJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return PdfJobListResponse(
        jobs=[PdfJobResponse.model_validate(j) for j in jobs],
        total=total,
    )
