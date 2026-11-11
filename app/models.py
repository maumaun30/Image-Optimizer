import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
)
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


class CompressionLevel(str, enum.Enum):
    """PDF compression presets. Map to Ghostscript -dPDFSETTINGS when available;
    `lossless` skips image downsampling and only restructures/compresses streams."""

    SCREEN = "screen"      # 72 dpi — smallest
    EBOOK = "ebook"        # 150 dpi — balanced (default)
    PRINTER = "printer"    # 300 dpi — high quality
    LOSSLESS = "lossless"  # no image downsampling, structural compression only


class VideoPreset(str, enum.Enum):
    """Video quality tiers. Map to a per-codec CRF value plus an audio bitrate."""

    LOW = "low"            # smallest file, visible quality loss
    BALANCED = "balanced"  # default
    HIGH = "high"          # near-source quality


class VideoCodec(str, enum.Enum):
    H264 = "h264"  # .mp4 — universal playback, fast encode (default)
    VP9 = "vp9"    # .webm — smaller than h264, slower
    AV1 = "av1"    # .mp4 — smallest, much slower encode


class ImageJob(Base):
    __tablename__ = "image_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    original_filename = Column(String(255), nullable=False)
    # Google account that created the job; NULL on jobs from before sign-in existed
    owner_email = Column(String(255), index=True)
    original_path = Column(String(500))
    processed_path = Column(String(500))
    status = Column(
        Enum(JobStatus, values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.PENDING,
        nullable=False,
    )
    output_format = Column(
        Enum(OutputFormat, values_callable=lambda e: [m.value for m in e]),
        default=OutputFormat.WEBP,
        nullable=False,
    )
    resize_width = Column(Integer)
    # 50 = max compression … 100 = lossless. Default 85.
    quality = Column(Integer, nullable=False, default=85, server_default="85")
    original_size_bytes = Column(Integer)
    processed_size_bytes = Column(Integer)
    error_message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    processed_at = Column(DateTime)
    downloaded_at = Column(DateTime)


class VideoJob(Base):
    __tablename__ = "video_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    original_filename = Column(String(255), nullable=False)
    # Google account that created the job; NULL on jobs from before sign-in existed
    owner_email = Column(String(255), index=True)
    original_path = Column(String(500))
    processed_path = Column(String(500))
    status = Column(
        Enum(JobStatus, values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.PENDING,
        nullable=False,
    )
    preset = Column(
        Enum(VideoPreset, values_callable=lambda e: [m.value for m in e]),
        default=VideoPreset.BALANCED,
        nullable=False,
    )
    codec = Column(
        Enum(VideoCodec, values_callable=lambda e: [m.value for m in e]),
        default=VideoCodec.H264,
        nullable=False,
    )
    target_width = Column(Integer)
    mute = Column(Boolean, nullable=False, default=False, server_default="0")
    duration_seconds = Column(Integer)
    # 0-100. Encodes are slow, so the worker writes progress here as it goes.
    progress_percent = Column(Integer, nullable=False, default=0, server_default="0")
    # BigInteger, not Integer: a 2 GB upload overflows MySQL's signed INT (max ~2.147 GB)
    original_size_bytes = Column(BigInteger)
    processed_size_bytes = Column(BigInteger)
    error_message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    processed_at = Column(DateTime)
    downloaded_at = Column(DateTime)


class PdfJob(Base):
    __tablename__ = "pdf_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    original_filename = Column(String(255), nullable=False)
    # Google account that created the job; NULL on jobs from before sign-in existed
    owner_email = Column(String(255), index=True)
    original_path = Column(String(500))
    processed_path = Column(String(500))
    status = Column(
        Enum(JobStatus, values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.PENDING,
        nullable=False,
    )
    compression_level = Column(
        Enum(CompressionLevel, values_callable=lambda e: [m.value for m in e]),
        default=CompressionLevel.EBOOK,
        nullable=False,
    )
    original_size_bytes = Column(Integer)
    processed_size_bytes = Column(Integer)
    error_message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    processed_at = Column(DateTime)
    downloaded_at = Column(DateTime)


class TusUpload(Base):
    """A resumable (tus) video upload in progress.

    The file grows at `path` one chunk at a time; `offset` is how many bytes are on
    disk. When it reaches `length` the upload turns into a VideoJob (`job_id`) and
    the encode is queued. Incomplete uploads are purged by `cleanup_expired_jobs`.
    """

    __tablename__ = "tus_uploads"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_email = Column(String(255), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    path = Column(String(500), nullable=False)
    length = Column(BigInteger, nullable=False)
    offset = Column(BigInteger, nullable=False, default=0, server_default="0")
    # Encode options, chosen when the upload starts
    preset = Column(String(16), nullable=False)
    codec = Column(String(16), nullable=False)
    target_width = Column(Integer)
    mute = Column(Boolean, nullable=False, default=False, server_default="0")
    job_id = Column(String(36))
    created_at = Column(DateTime, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
