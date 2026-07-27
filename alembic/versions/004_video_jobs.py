"""video jobs

Revision ID: 004_video_jobs
Revises: 003_image_quality
Create Date: 2026-07-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_video_jobs"
down_revision: Union[str, None] = "003_image_quality"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "video_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("original_path", sa.String(500)),
        sa.Column("processed_path", sa.String(500)),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "processing", "ready", "downloaded", "failed", "expired",
                name="jobstatus",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "preset",
            sa.Enum("low", "balanced", "high", name="videopreset"),
            nullable=False,
            server_default="balanced",
        ),
        sa.Column(
            "codec",
            sa.Enum("h264", "vp9", "av1", name="videocodec"),
            nullable=False,
            server_default="h264",
        ),
        sa.Column("target_width", sa.Integer()),
        sa.Column("mute", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        # BigInteger: a 2 GB upload overflows MySQL's signed INT
        sa.Column("original_size_bytes", sa.BigInteger()),
        sa.Column("processed_size_bytes", sa.BigInteger()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("processed_at", sa.DateTime()),
        sa.Column("downloaded_at", sa.DateTime()),
    )
    op.create_index("ix_video_jobs_status", "video_jobs", ["status"])
    op.create_index("ix_video_jobs_created_at", "video_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_table("video_jobs")
