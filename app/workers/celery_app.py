from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "image_optimizer",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    # One task at a time per worker — image processing is memory-intensive
    worker_prefetch_multiplier=1,
    beat_schedule={
        "cleanup-expired-jobs": {
            "task": "app.workers.tasks.cleanup_expired_jobs",
            "schedule": crontab(minute=0),  # top of every hour
        }
    },
)
