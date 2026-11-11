"""resumable (tus) video uploads

Revision ID: 006_tus_uploads
Revises: 005_job_owner
Create Date: 2026-11-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_tus_uploads"
down_revision: Union[str, None] = "005_job_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tus_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_email", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        # BigInteger: uploads run to gigabytes
        sa.Column("length", sa.BigInteger(), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("preset", sa.String(16), nullable=False),
        sa.Column("codec", sa.String(16), nullable=False),
        sa.Column("target_width", sa.Integer()),
        sa.Column("mute", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("job_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_tus_uploads_owner_email", "tus_uploads", ["owner_email"])
    op.create_index("ix_tus_uploads_created_at", "tus_uploads", ["created_at"])


def downgrade() -> None:
    op.drop_table("tus_uploads")
