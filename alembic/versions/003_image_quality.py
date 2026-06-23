"""image quality column

Revision ID: 003_image_quality
Revises: 002_pdf_jobs
Create Date: 2026-06-23

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_image_quality"
down_revision: Union[str, None] = "002_pdf_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "image_jobs",
        sa.Column("quality", sa.Integer(), nullable=False, server_default="85"),
    )


def downgrade() -> None:
    op.drop_column("image_jobs", "quality")
