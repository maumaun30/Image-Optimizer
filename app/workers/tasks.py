import os
from datetime import datetime, timedelta

import pikepdf
from PIL import Image, UnidentifiedImageError

from app.config import settings
from app.database import SessionLocal
from app.models import ImageJob, JobStatus, PdfJob, VideoJob
from app.services.image_processor import process_image
from app.services.pdf_processor import process_pdf
from app.services.video_processor import FfmpegMissingError, VideoProcessingError, process_video
from app.workers.celery_app import celery_app

# Errors caused by the input file itself. Retrying can't fix them, so the job fails at
# once instead of burning three retries on a corrupt upload.
PERMANENT_IMAGE_ERRORS = (UnidentifiedImageError, Image.DecompressionBombError, ValueError)
PERMANENT_PDF_ERRORS = (pikepdf.PdfError,)


def _fail(db, model, job_id: str, exc: Exception) -> None:
    db.rollback()
    job = db.get(model, job_id)
    if job:
        job.status = JobStatus.FAILED
        job.error_message = str(exc)
        db.commit()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_image_task(self, job_id: str):
    db = SessionLocal()
    try:
        job = db.get(ImageJob, job_id)
        if not job:
            return

        job.status = JobStatus.PROCESSING
        db.commit()

        settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

        output_path, output_size = process_image(
            input_path=job.original_path,
            output_dir=settings.PROCESSED_DIR,
            output_format=job.output_format,
            resize_width=job.resize_width,
            quality=job.quality or settings.DEFAULT_QUALITY,
        )

        job.processed_path = output_path
        job.processed_size_bytes = output_size
        job.status = JobStatus.READY
        job.processed_at = datetime.utcnow()
        db.commit()

        # Delete original upload immediately after successful processing
        if job.original_path and os.path.exists(job.original_path):
            os.remove(job.original_path)
        job.original_path = None
        db.commit()

    except PERMANENT_IMAGE_ERRORS as exc:
        _fail(db, ImageJob, job_id, exc)
        return

    except Exception as exc:
        db.rollback()
        job = db.get(ImageJob, job_id)
        if job:
            if self.request.retries >= self.max_retries:
                job.status = JobStatus.FAILED
                job.error_message = str(exc)
                db.commit()
            else:
                job.status = JobStatus.PENDING
                db.commit()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_pdf_task(self, job_id: str):
    db = SessionLocal()
    try:
        job = db.get(PdfJob, job_id)
        if not job:
            return

        job.status = JobStatus.PROCESSING
        db.commit()

        settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

        output_path, output_size = process_pdf(
            input_path=job.original_path,
            output_dir=settings.PROCESSED_DIR,
            compression_level=job.compression_level,
        )

        job.processed_path = output_path
        job.processed_size_bytes = output_size
        job.status = JobStatus.READY
        job.processed_at = datetime.utcnow()
        db.commit()

        # Delete original upload immediately after successful processing
        if job.original_path and os.path.exists(job.original_path):
            os.remove(job.original_path)
        job.original_path = None
        db.commit()

    except PERMANENT_PDF_ERRORS as exc:
        _fail(db, PdfJob, job_id, exc)
        return

    except Exception as exc:
        db.rollback()
        job = db.get(PdfJob, job_id)
        if job:
            if self.request.retries >= self.max_retries:
                job.status = JobStatus.FAILED
                job.error_message = str(exc)
                db.commit()
            else:
                job.status = JobStatus.PENDING
                db.commit()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    # Encodes are expensive — a blind retry means re-running a job that may take hours.
    # Deterministic ffmpeg failures are not retried at all (see below).
    max_retries=1,
    default_retry_delay=30,
    soft_time_limit=settings.VIDEO_TIME_LIMIT_SECONDS,
    time_limit=settings.VIDEO_TIME_LIMIT_SECONDS + 300,
)
def process_video_task(self, job_id: str):
    db = SessionLocal()
    try:
        job = db.get(VideoJob, job_id)
        if not job:
            return

        job.status = JobStatus.PROCESSING
        job.progress_percent = 0
        db.commit()

        settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

        def report(percent: int) -> None:
            job.progress_percent = percent
            db.commit()

        output_path, output_size, duration = process_video(
            input_path=job.original_path,
            output_dir=settings.PROCESSED_DIR,
            preset=job.preset,
            codec=job.codec,
            target_width=job.target_width,
            mute=job.mute,
            threads=settings.FFMPEG_THREADS,
            time_limit=settings.VIDEO_TIME_LIMIT_SECONDS,
            on_progress=report,
        )

        job.processed_path = output_path
        job.processed_size_bytes = output_size
        job.duration_seconds = duration
        job.progress_percent = 100
        job.status = JobStatus.READY
        job.processed_at = datetime.utcnow()
        db.commit()

        # Delete original upload immediately after successful processing
        if job.original_path and os.path.exists(job.original_path):
            os.remove(job.original_path)
        job.original_path = None
        db.commit()

    except (FfmpegMissingError, VideoProcessingError) as exc:
        # Bad input, missing encoder, or time limit hit — retrying changes nothing.
        db.rollback()
        job = db.get(VideoJob, job_id)
        if job:
            job.status = JobStatus.FAILED
            job.error_message = str(exc)
            db.commit()
        return

    except Exception as exc:
        db.rollback()
        job = db.get(VideoJob, job_id)
        if job:
            if self.request.retries >= self.max_retries:
                job.status = JobStatus.FAILED
                job.error_message = str(exc)
                db.commit()
            else:
                job.status = JobStatus.PENDING
                job.progress_percent = 0
                db.commit()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task
def cleanup_expired_jobs():
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=settings.AUTO_DELETE_HOURS)
        active = [JobStatus.READY, JobStatus.PENDING, JobStatus.PROCESSING]

        count = 0
        for model in (ImageJob, PdfJob, VideoJob):
            expired = (
                db.query(model)
                .filter(model.status.in_(active), model.created_at < cutoff)
                .all()
            )
            for job in expired:
                for path in (job.original_path, job.processed_path):
                    if path:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                job.status = JobStatus.EXPIRED
                job.original_path = None
                job.processed_path = None
                count += 1

            # Downloaded files are kept for a short grace window (dropped connections,
            # browser retries) and deleted here once it has passed.
            grace_cutoff = datetime.utcnow() - timedelta(minutes=settings.DOWNLOAD_GRACE_MINUTES)
            downloaded = (
                db.query(model)
                .filter(
                    model.status == JobStatus.DOWNLOADED,
                    model.processed_path.isnot(None),
                    model.downloaded_at < grace_cutoff,
                )
                .all()
            )
            for job in downloaded:
                for path in (job.original_path, job.processed_path):
                    if path:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                job.original_path = None
                job.processed_path = None
                count += 1

        db.commit()
        return f"Cleaned up {count} expired jobs"
    finally:
        db.close()
