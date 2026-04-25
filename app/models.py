import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    EXPIRED = "expired"


class OutputFormat(str, enum.Enum):
    WEBP = "webp"
    AVIF = "avif"
    ORIGINAL = "original"


class ImageJob(Base):
    __tablename__ = "image_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    original_filename = Column(String(255), nullable=False)
    original_path = Column(String(500))
    processed_path = Column(String(500))
    status = Column(Enum(JobStatus), default=JobStatus.PENDING, nullable=False)
    output_format = Column(Enum(OutputFormat), default=OutputFormat.WEBP, nullable=False)
    resize_width = Column(Integer)
    original_size_bytes = Column(Integer)
    processed_size_bytes = Column(Integer)
    error_message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    processed_at = Column(DateTime)
    downloaded_at = Column(DateTime)
