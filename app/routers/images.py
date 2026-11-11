import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ImageJob, JobStatus, OutputFormat
from app.schemas import JobListResponse, JobResponse, UploadResponse
from app.auth import User, current_user
from app.uploads import (
    check_capacity,
    delete_files,
    downloadable_path,
    get_owned_job,
    save_upload,
    visible_jobs,
)
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

# What Pillow must detect from the file's own bytes; the client's content type is
# only a first filter and is trivially spoofed.
ALLOWED_PIL_FORMATS = {"JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP", "AVIF", "MPO"}


def _check_is_image(path: Path, filename: str | None) -> None:
    try:
        with Image.open(path) as img:  # parses the header only, no full decode
            detected = img.format
    except Image.DecompressionBombError:
        raise HTTPException(400, f"File '{filename}' has too many pixels")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, f"File '{filename}' is not a valid image")
    if detected not in ALLOWED_PIL_FORMATS:
        raise HTTPException(400, f"File '{filename}' has unsupported format {detected}")


@router.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_images(
    files: Annotated[list[UploadFile], File(description="One or more image files")],
    format: OutputFormat = Query(OutputFormat.WEBP, description="Output format"),
    width: int | None = Query(
        None, gt=0, le=10000, description="Target width in pixels; aspect ratio is preserved"
    ),
    quality: int = Query(
        85,
        ge=50,
        le=100,
        description="Compression quality: 50 = max compression … 100 = lossless",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if not files:
        raise HTTPException(400, "No files provided")
    if len(files) > 20:
        raise HTTPException(400, "Maximum 20 files per request")

    check_capacity(db, ImageJob, settings.MAX_QUEUED_JOBS, len(files))

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    jobs: list[ImageJob] = []
    written_paths: list[Path] = []

    try:
        for file in files:
            if file.content_type not in ALLOWED_CONTENT_TYPES:
                raise HTTPException(400, f"Unsupported file type: {file.content_type}")

            job_id = str(uuid.uuid4())
            ext = Path(file.filename or "image").suffix.lower() or ".jpg"
            upload_path = settings.UPLOAD_DIR / f"{job_id}{ext}"
            written_paths.append(upload_path)

            size = await save_upload(file, upload_path, max_bytes, settings.MAX_FILE_SIZE_MB)
            _check_is_image(upload_path, file.filename)

            job = ImageJob(
                id=job_id,
                owner_email=user.email,
                original_filename=file.filename or "upload",
                original_path=str(upload_path),
                status=JobStatus.PENDING,
                output_format=format,
                resize_width=width,
                quality=quality,
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
        process_image_task.delay(job.id)
        db.refresh(job)

    return UploadResponse(
        jobs=[JobResponse.model_validate(j) for j in jobs],
        total=len(jobs),
    )


@router.get("/status/{job_id}", response_model=JobResponse)
def get_status(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    job = get_owned_job(db, ImageJob, job_id, user)
    return JobResponse.model_validate(job)


@router.get("/download/{job_id}")
def download_image(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    job = get_owned_job(db, ImageJob, job_id, user)

    processed_path = downloadable_path(job, f"Not ready yet — current status: {job.status}")
    db.commit()

    ext = Path(processed_path).suffix
    filename = f"{Path(job.original_filename).stem}{ext}"
    return FileResponse(processed_path, filename=filename)


@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    all_users: bool = Query(False, alias="all", description="Admins only: list every user's jobs"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """The signed-in user's jobs, newest first."""
    query = visible_jobs(db, ImageJob, user, all_users)
    total = query.count()
    jobs = (
        query
        .order_by(ImageJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return JobListResponse(
        jobs=[JobResponse.model_validate(j) for j in jobs],
        total=total,
    )
