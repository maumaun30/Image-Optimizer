"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-04-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "image_jobs",
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
            "output_format",
            sa.Enum("webp", "avif", "original", name="outputformat"),
            nullable=False,
            server_default="webp",
        ),
        sa.Column("resize_width", sa.Integer()),
        sa.Column("original_size_bytes", sa.Integer()),
        sa.Column("processed_size_bytes", sa.Integer()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("processed_at", sa.DateTime()),
        sa.Column("downloaded_at", sa.DateTime()),
    )
    op.create_index("ix_image_jobs_status", "image_jobs", ["status"])
    op.create_index("ix_image_jobs_created_at", "image_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_table("image_jobs")
