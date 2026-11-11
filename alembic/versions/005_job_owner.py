"""job owner email

Revision ID: 005_job_owner
Revises: 004_video_jobs
Create Date: 2026-11-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_job_owner"
down_revision: Union[str, None] = "004_video_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("image_jobs", "pdf_jobs", "video_jobs")


def upgrade() -> None:
    # Nullable: existing jobs have no owner and become visible to admins only
    for table in TABLES:
        op.add_column(table, sa.Column("owner_email", sa.String(255)))
        op.create_index(f"ix_{table}_owner_email", table, ["owner_email"])


def downgrade() -> None:
    for table in TABLES:
        op.drop_index(f"ix_{table}_owner_email", table_name=table)
        op.drop_column(table, "owner_email")
