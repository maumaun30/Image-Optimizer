import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import CompressionLevel, JobStatus, PdfJob
from app.schemas import PdfJobListResponse, PdfJobResponse, PdfUploadResponse
from app.auth import User, current_user
from app.uploads import (
    check_capacity,
    delete_files,
    downloadable_path,
    get_owned_job,
    save_upload,
    visible_jobs,
)
from app.workers.tasks import process_pdf_task

router = APIRouter(prefix="/pdf", tags=["pdf"])

ALLOWED_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}


def _check_is_pdf(path: Path, filename: str | None) -> None:
    # The spec allows junk before the header; readers accept it within the first 1 KB
    with path.open("rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        raise HTTPException(400, f"File '{filename}' is not a valid PDF")


@router.post("/upload", response_model=PdfUploadResponse, status_code=202)
async def upload_pdfs(
    files: Annotated[list[UploadFile], File(description="One or more PDF files")],
    level: CompressionLevel = Query(
        CompressionLevel.EBOOK, description="Compression preset"
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if not files:
        raise HTTPException(400, "No files provided")
    if len(files) > 20:
        raise HTTPException(400, "Maximum 20 files per request")

    check_capacity(db, PdfJob, settings.MAX_QUEUED_JOBS, len(files))

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    jobs: list[PdfJob] = []
    written_paths: list[Path] = []

    try:
        for file in files:
            is_pdf = file.content_type in ALLOWED_CONTENT_TYPES or (
                file.filename or ""
            ).lower().endswith(".pdf")
            if not is_pdf:
                raise HTTPException(400, f"Unsupported file type: {file.content_type}")

            job_id = str(uuid.uuid4())
            upload_path = settings.UPLOAD_DIR / f"{job_id}.pdf"
            written_paths.append(upload_path)

            size = await save_upload(file, upload_path, max_bytes, settings.MAX_FILE_SIZE_MB)
            _check_is_pdf(upload_path, file.filename)

            job = PdfJob(
                id=job_id,
                owner_email=user.email,
                original_filename=file.filename or "upload.pdf",
                original_path=str(upload_path),
                status=JobStatus.PENDING,
                compression_level=level,
                original_size_bytes=size,
            )
            db.add(job)
            db.flush()
            jobs.append(job)
    except Exception:
        # A rejected file fails the whole request; don't leave earlier files behind
        db.rollback()
        delete_files(*written_paths)
        raise

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
def get_status(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    job = get_owned_job(db, PdfJob, job_id, user)
    return PdfJobResponse.model_validate(job)


@router.get("/download/{job_id}")
def download_pdf(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    job = get_owned_job(db, PdfJob, job_id, user)

    processed_path = downloadable_path(job, f"Not ready yet — current status: {job.status}")
    db.commit()

    filename = f"{Path(job.original_filename).stem}.pdf"
    return FileResponse(processed_path, filename=filename, media_type="application/pdf")


@router.get("/jobs", response_model=PdfJobListResponse)
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    all_users: bool = Query(False, alias="all", description="Admins only: list every user's jobs"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """The signed-in user's jobs, newest first."""
    query = visible_jobs(db, PdfJob, user, all_users)
    total = query.count()
    jobs = (
        query
        .order_by(PdfJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return PdfJobListResponse(
        jobs=[PdfJobResponse.model_validate(j) for j in jobs],
        total=total,
    )
