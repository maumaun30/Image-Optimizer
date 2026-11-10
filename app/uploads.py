"""Shared helpers for the upload, download and listing endpoints."""

import logging
import os
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Header, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ImageJob, JobStatus, PdfJob, VideoJob

logger = logging.getLogger(__name__)

# Uploads are streamed to disk in chunks so no file is ever held in memory whole
CHUNK_SIZE = 4 * 1024 * 1024

QUEUED = (JobStatus.PENDING, JobStatus.PROCESSING)


def delete_files(*paths: str | Path | None) -> None:
    for path in paths:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


async def save_upload(file: UploadFile, dest: Path, max_bytes: int, limit_mb: int) -> int:
    """Stream `file` to `dest`, aborting as soon as `max_bytes` is crossed.

    Returns the byte count. The caller owns `dest` and must delete it if the request
    is rejected later.
    """
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(CHUNK_SIZE):
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(413, f"File '{file.filename}' exceeds {limit_mb} MB limit")
            out.write(chunk)
    if size == 0:
        raise HTTPException(400, f"File '{file.filename}' is empty")
    return size


def check_capacity(db: Session, model: type, max_queued: int, incoming: int) -> None:
    """Reject the upload when the queue or the disk can't take more work.

    Per-IP rate limiting lives in nginx; this is the backstop that holds no matter how
    many addresses the traffic comes from.
    """
    queued = db.query(model).filter(model.status.in_(QUEUED)).count()
    if queued + incoming > max_queued:
        raise HTTPException(503, "Server is busy — too many files queued. Try again shortly.")

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    free_mb = shutil.disk_usage(settings.UPLOAD_DIR).free // (1024 * 1024)
    if free_mb < settings.MIN_FREE_DISK_MB:
        logger.error("Rejecting upload: only %d MB free on upload disk", free_mb)
        raise HTTPException(503, "Server is low on storage. Try again later.")


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Guard for endpoints that expose every job (filenames + downloadable IDs).

    With ADMIN_API_KEY unset the endpoints are disabled outright and answer 404, so a
    forgotten setting fails closed.
    """
    if not settings.ADMIN_API_KEY:
        raise HTTPException(404, "Not found")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
        raise HTTPException(401, "Invalid admin key")


def downloadable_path(job: ImageJob | PdfJob | VideoJob, not_ready_detail: str) -> str:
    """Validate a download request and record it. Returns the file to stream.

    The file is not deleted when the response is sent: a dropped connection or a second
    click would otherwise lose it for good. It stays downloadable for
    DOWNLOAD_GRACE_MINUTES after the first download, then `cleanup_expired_jobs`
    deletes it.
    """
    if job.status == JobStatus.FAILED:
        raise HTTPException(422, f"Processing failed: {job.error_message}")
    if job.status == JobStatus.EXPIRED:
        raise HTTPException(410, "File expired and deleted")
    if job.status == JobStatus.DOWNLOADED:
        grace = timedelta(minutes=settings.DOWNLOAD_GRACE_MINUTES)
        if not job.downloaded_at or datetime.utcnow() - job.downloaded_at > grace:
            raise HTTPException(410, "File already downloaded and deleted")
    elif job.status != JobStatus.READY:
        raise HTTPException(425, not_ready_detail)

    if not job.processed_path or not os.path.exists(job.processed_path):
        raise HTTPException(410, "File already downloaded and deleted")

    if job.status == JobStatus.READY:
        job.status = JobStatus.DOWNLOADED
        job.downloaded_at = datetime.utcnow()
    return job.processed_path
