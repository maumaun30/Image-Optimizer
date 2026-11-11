import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import JobStatus, VideoCodec, VideoJob, VideoPreset
from app.schemas import VideoJobListResponse, VideoJobResponse, VideoUploadResponse
from app.services.video_processor import ffmpeg_bin, ffprobe_bin
from app.auth import User, current_user
from app.uploads import (
    check_capacity,
    delete_files,
    downloadable_path,
    get_owned_job,
    save_upload,
    visible_jobs,
)
from app.workers.tasks import process_video_task

router = APIRouter(prefix="/video", tags=["video"])

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ts",
}


@router.post("/upload", response_model=VideoUploadResponse, status_code=202)
async def upload_videos(
    files: Annotated[list[UploadFile], File(description="One or more video files")],
    preset: VideoPreset = Query(VideoPreset.BALANCED, description="Quality tier"),
    codec: VideoCodec = Query(
        VideoCodec.H264, description="h264 = universal/fast, vp9 = smaller, av1 = smallest/slowest"
    ),
    width: int | None = Query(
        None, gt=0, le=7680, description="Target width in pixels; aspect ratio preserved, never upscaled"
    ),
    mute: bool = Query(False, description="Drop the audio track entirely"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if not ffmpeg_bin() or not ffprobe_bin():
        raise HTTPException(503, "Video compression unavailable: ffmpeg is not installed")

    if not files:
        raise HTTPException(400, "No files provided")
    if len(files) > settings.MAX_VIDEO_FILES:
        raise HTTPException(400, f"Maximum {settings.MAX_VIDEO_FILES} files per request")

    check_capacity(db, VideoJob, settings.MAX_QUEUED_VIDEOS, len(files))

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.MAX_VIDEO_SIZE_MB * 1024 * 1024
    jobs: list[VideoJob] = []
    written_paths: list[Path] = []

    try:
        for file in files:
            ext = Path(file.filename or "video").suffix.lower()
            is_video = (file.content_type or "").startswith("video/") or ext in ALLOWED_EXTENSIONS
            if not is_video:
                raise HTTPException(400, f"Unsupported file type: {file.content_type}")

            job_id = str(uuid.uuid4())
            upload_path = settings.UPLOAD_DIR / f"{job_id}{ext or '.mp4'}"
            written_paths.append(upload_path)

            # ffprobe in the worker is the real format check — a non-video fails there
            size = await save_upload(file, upload_path, max_bytes, settings.MAX_VIDEO_SIZE_MB)

            job = VideoJob(
                id=job_id,
                owner_email=user.email,
                original_filename=file.filename or "upload",
                original_path=str(upload_path),
                status=JobStatus.PENDING,
                preset=preset,
                codec=codec,
                target_width=width,
                mute=mute,
                original_size_bytes=size,
            )
            db.add(job)
            db.flush()
            jobs.append(job)
    except Exception:
        # Don't leave gigabytes of orphaned upload behind on a rejected request
        db.rollback()
        delete_files(*written_paths)
        raise

    db.commit()

    # Dispatch Celery tasks after commit so workers see the DB records
    for job in jobs:
        process_video_task.delay(job.id)
        db.refresh(job)

    return VideoUploadResponse(
        jobs=[VideoJobResponse.model_validate(j) for j in jobs],
        total=len(jobs),
    )


@router.get("/status/{job_id}", response_model=VideoJobResponse)
def get_status(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    job = get_owned_job(db, VideoJob, job_id, user)
    return VideoJobResponse.model_validate(job)


@router.get("/download/{job_id}")
def download_video(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    job = get_owned_job(db, VideoJob, job_id, user)

    processed_path = downloadable_path(
        job, f"Not ready yet — current status: {job.status} ({job.progress_percent}%)"
    )
    db.commit()

    ext = Path(processed_path).suffix
    filename = f"{Path(job.original_filename).stem}{ext}"
    return FileResponse(processed_path, filename=filename)


@router.get("/jobs", response_model=VideoJobListResponse)
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    all_users: bool = Query(False, alias="all", description="Admins only: list every user's jobs"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """The signed-in user's jobs, newest first."""
    query = visible_jobs(db, VideoJob, user, all_users)
    total = query.count()
    jobs = (
        query
        .order_by(VideoJob.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return VideoJobListResponse(
        jobs=[VideoJobResponse.model_validate(j) for j in jobs],
        total=total,
    )
