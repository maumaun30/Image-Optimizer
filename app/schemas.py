from datetime import datetime
from typing import Optional

from pydantic import BaseModel, model_validator

from app.models import CompressionLevel, JobStatus, OutputFormat


class JobResponse(BaseModel):
    id: str
    original_filename: str
    status: JobStatus
    output_format: OutputFormat
    resize_width: Optional[int] = None
    original_size_bytes: Optional[int] = None
    processed_size_bytes: Optional[int] = None
    savings_percent: Optional[float] = None
    error_message: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def compute_savings(self):
        if self.original_size_bytes and self.processed_size_bytes:
            self.savings_percent = round(
                (1 - self.processed_size_bytes / self.original_size_bytes) * 100, 2
            )
        return self


class UploadResponse(BaseModel):
    jobs: list[JobResponse]
    total: int


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int


class PdfJobResponse(BaseModel):
    id: str
    original_filename: str
    status: JobStatus
    compression_level: CompressionLevel
    original_size_bytes: Optional[int] = None
    processed_size_bytes: Optional[int] = None
    savings_percent: Optional[float] = None
    error_message: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def compute_savings(self):
        if self.original_size_bytes and self.processed_size_bytes:
            self.savings_percent = round(
                (1 - self.processed_size_bytes / self.original_size_bytes) * 100, 2
            )
        return self


class PdfUploadResponse(BaseModel):
    jobs: list[PdfJobResponse]
    total: int


class PdfJobListResponse(BaseModel):
    jobs: list[PdfJobResponse]
    total: int
