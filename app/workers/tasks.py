import os
from datetime import datetime, timedelta

from app.config import settings
from app.database import SessionLocal
from app.models import ImageJob, JobStatus
from app.services.image_processor import process_image
from app.workers.celery_app import celery_app


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
            quality=settings.DEFAULT_QUALITY,
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


@celery_app.task
def cleanup_expired_jobs():
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=settings.AUTO_DELETE_HOURS)
        expired = (
            db.query(ImageJob)
            .filter(
                ImageJob.status.in_([JobStatus.READY, JobStatus.PENDING, JobStatus.PROCESSING]),
                ImageJob.created_at < cutoff,
            )
            .all()
        )

        count = 0
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

        db.commit()
        return f"Cleaned up {count} expired jobs"
    finally:
        db.close()
