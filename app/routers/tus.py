"""Resumable video uploads — tus 1.0 (https://tus.io/protocols/resumable-upload).

Implements the core protocol plus the `creation` and `termination` extensions, which
is what tus-js-client needs:

    POST   /video/tus/        create an upload (Upload-Length + Upload-Metadata)
    HEAD   /video/tus/{id}    how many bytes the server has (resume point)
    PATCH  /video/tus/{id}    append one chunk at Upload-Offset
    DELETE /video/tus/{id}    abandon it
    GET    /video/tus/{id}    (not tus) the VideoJob the finished upload became

Chunks are capped at TUS_MAX_CHUNK_MB so every request fits under Cloudflare's 100 MB
body limit; a multi-gigabyte video is many small requests through the normal API.
A dropped connection keeps whatever arrived, so the client resumes from there.
"""

import base64
import binascii
import logging
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import update
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from app.auth import User, current_user
from app.config import settings
from app.database import get_db
from app.models import JobStatus, TusUpload, VideoCodec, VideoJob, VideoPreset
from app.routers.video import ALLOWED_EXTENSIONS
from app.schemas import VideoJobResponse
from app.services.video_processor import ffmpeg_bin, ffprobe_bin
from app.uploads import check_capacity, delete_files
from app.workers.tasks import process_video_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/video/tus", tags=["video"])

TUS_VERSION = "1.0.0"
TUS_HEADERS = {
    "Tus-Resumable": TUS_VERSION,
    "Tus-Version": TUS_VERSION,
    "Tus-Extension": "creation,termination",
}
# Headers tus-js-client must be able to read on cross-origin responses
EXPOSE_HEADERS = [
    "Location",
    "Upload-Offset",
    "Upload-Length",
    "Tus-Resumable",
    "Tus-Version",
    "Tus-Extension",
    "Tus-Max-Size",
]


def _tus_response(status: int, **headers: str) -> Response:
    return Response(status_code=status, headers={"Tus-Resumable": TUS_VERSION, **headers})


def _require_tus(request: Request) -> None:
    if request.headers.get("tus-resumable") != TUS_VERSION:
        raise HTTPException(412, "Unsupported tus version", headers={"Tus-Version": TUS_VERSION})


def _parse_metadata(raw: str) -> dict[str, str]:
    """`Upload-Metadata: key base64value,key2 base64value2`."""
    meta: dict[str, str] = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        key, _, value = pair.partition(" ")
        try:
            meta[key] = base64.b64decode(value, validate=True).decode("utf-8") if value else ""
        except (binascii.Error, UnicodeDecodeError):
            raise HTTPException(400, f"Bad Upload-Metadata value for '{key}'")
    return meta


def _owned_upload(db: Session, upload_id: str, user: User) -> TusUpload:
    upload = db.get(TusUpload, upload_id)
    # Someone else's upload looks like it doesn't exist
    if not upload or upload.owner_email != user.email:
        raise HTTPException(404, "Upload not found", headers={"Tus-Resumable": TUS_VERSION})
    return upload


@router.options("/")
@router.options("/{upload_id}")
def options(upload_id: str | None = None):
    """Capability discovery (a CORS preflight never reaches here)."""
    return Response(
        status_code=204,
        headers={**TUS_HEADERS, "Tus-Max-Size": str(settings.MAX_VIDEO_SIZE_MB * 1024 * 1024)},
    )


@router.post("/", status_code=201)
def create_upload(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_tus(request)
    if not ffmpeg_bin() or not ffprobe_bin():
        raise HTTPException(503, "Video compression unavailable: ffmpeg is not installed")

    try:
        length = int(request.headers.get("upload-length", ""))
    except ValueError:
        # No Upload-Defer-Length support: the size must be known up front
        raise HTTPException(400, "Upload-Length is required")
    max_bytes = settings.MAX_VIDEO_SIZE_MB * 1024 * 1024
    if length <= 0:
        raise HTTPException(400, "File is empty")
    if length > max_bytes:
        raise HTTPException(413, f"File exceeds {settings.MAX_VIDEO_SIZE_MB} MB limit")

    meta = _parse_metadata(request.headers.get("upload-metadata", ""))
    filename = (meta.get("filename") or "video").strip()[:255] or "video"
    ext = Path(filename).suffix.lower()
    if not (meta.get("filetype", "").startswith("video/") or ext in ALLOWED_EXTENSIONS):
        raise HTTPException(400, f"Unsupported file type: {meta.get('filetype') or ext or 'unknown'}")

    try:
        preset = VideoPreset(meta.get("preset", VideoPreset.BALANCED.value))
        codec = VideoCodec(meta.get("codec", VideoCodec.H264.value))
    except ValueError:
        raise HTTPException(400, "Unknown preset or codec")
    width = None
    if meta.get("width"):
        if not meta["width"].isdigit() or not 0 < int(meta["width"]) <= 7680:
            raise HTTPException(400, "Width must be 1-7680")
        width = int(meta["width"])
    mute = meta.get("mute", "").lower() == "true"

    open_uploads = (
        db.query(TusUpload)
        .filter(TusUpload.owner_email == user.email, TusUpload.job_id.is_(None))
        .count()
    )
    if open_uploads >= settings.TUS_MAX_OPEN_PER_USER:
        raise HTTPException(429, "Too many unfinished uploads. Finish or cancel one first.")
    check_capacity(db, VideoJob, settings.MAX_QUEUED_VIDEOS, 1)
    # Reserve the whole file's worth of disk up front, not just the first chunk
    free = shutil.disk_usage(settings.UPLOAD_DIR).free
    if free - length < settings.MIN_FREE_DISK_MB * 1024 * 1024:
        raise HTTPException(507, "Not enough storage on the server for this file. Try again later.")

    upload_id = str(uuid.uuid4())
    path = settings.UPLOAD_DIR / f"tus-{upload_id}.part"
    path.touch()
    db.add(
        TusUpload(
            id=upload_id,
            owner_email=user.email,
            filename=filename,
            path=str(path),
            length=length,
            offset=0,
            preset=preset.value,
            codec=codec.value,
            target_width=width,
            mute=mute,
        )
    )
    db.commit()
    # Relative: tus-js-client resolves it against the endpoint, so the scheme and host
    # stay whatever the browser used (no http/https mix-ups behind the proxy)
    return _tus_response(201, Location=f"/video/tus/{upload_id}")


@router.head("/{upload_id}")
def upload_offset(upload_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_tus(request)
    upload = _owned_upload(db, upload_id, user)
    return _tus_response(
        200,
        **{
            "Upload-Offset": str(upload.offset),
            "Upload-Length": str(upload.length),
            "Cache-Control": "no-store",
        },
    )


@router.patch("/{upload_id}")
async def append_chunk(
    upload_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    _require_tus(request)
    if request.headers.get("content-type") != "application/offset+octet-stream":
        raise HTTPException(415, "Content-Type must be application/offset+octet-stream")
    upload = _owned_upload(db, upload_id, user)
    if upload.job_id:
        raise HTTPException(409, "Upload already complete")
    try:
        client_offset = int(request.headers.get("upload-offset", ""))
    except ValueError:
        raise HTTPException(400, "Upload-Offset is required")
    if client_offset != upload.offset:
        # The client is out of step (e.g. a retried chunk); it should HEAD and resume
        raise HTTPException(409, "Upload-Offset does not match", headers={"Upload-Offset": str(upload.offset)})

    start = upload.offset
    limit = min(settings.TUS_MAX_CHUNK_MB * 1024 * 1024, upload.length - start)
    written = 0
    too_big = False
    with open(upload.path, "r+b") as out:
        out.seek(start)
        try:
            async for piece in request.stream():
                if written + len(piece) > limit:
                    too_big = True
                    break
                out.write(piece)
                written += len(piece)
        except ClientDisconnect:
            # Keep what arrived: the client resumes from the new offset
            logger.info("tus %s: client disconnected after %d bytes", upload_id, written)
        out.truncate(start + written)

    if too_big:
        # Discard the oversized chunk entirely
        with open(upload.path, "r+b") as out:
            out.truncate(start)
        raise HTTPException(413, f"Chunk exceeds {settings.TUS_MAX_CHUNK_MB} MB or the declared length")

    new_offset = start + written
    # Optimistic check: another PATCH for the same upload must not have moved it
    moved = db.execute(
        update(TusUpload)
        .where(TusUpload.id == upload_id, TusUpload.offset == start)
        .values(offset=new_offset)
    ).rowcount
    db.commit()
    if not moved:
        raise HTTPException(409, "Upload changed concurrently")

    if new_offset == upload.length:
        db.refresh(upload)
        _finish(db, upload)

    return _tus_response(204, **{"Upload-Offset": str(new_offset)})


def _finish(db: Session, upload: TusUpload) -> None:
    """The last byte arrived: turn the upload into a queued encode."""
    job_id = str(uuid.uuid4())
    ext = Path(upload.filename).suffix.lower() or ".mp4"
    job_path = settings.UPLOAD_DIR / f"{job_id}{ext}"
    os.replace(upload.path, job_path)
    db.add(
        VideoJob(
            id=job_id,
            owner_email=upload.owner_email,
            original_filename=upload.filename,
            original_path=str(job_path),
            status=JobStatus.PENDING,
            preset=VideoPreset(upload.preset),
            codec=VideoCodec(upload.codec),
            target_width=upload.target_width,
            mute=upload.mute,
            original_size_bytes=upload.length,
        )
    )
    upload.job_id = job_id
    upload.path = str(job_path)
    db.commit()
    process_video_task.delay(job_id)


@router.delete("/{upload_id}")
def cancel_upload(upload_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _require_tus(request)
    upload = _owned_upload(db, upload_id, user)
    if upload.job_id:
        raise HTTPException(409, "Upload already complete; the encode is queued")
    delete_files(upload.path)
    db.delete(upload)
    db.commit()
    return _tus_response(204)


@router.get("/{upload_id}")
def upload_result(upload_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Not part of tus: what the finished upload became, so the client can poll the job."""
    upload = _owned_upload(db, upload_id, user)
    job = db.get(VideoJob, upload.job_id) if upload.job_id else None
    return {
        "offset": upload.offset,
        "length": upload.length,
        "job": VideoJobResponse.model_validate(job).model_dump(mode="json") if job else None,
    }
