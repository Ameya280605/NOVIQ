"""Add outbox claim timestamp

Revision ID: 9c6f2a4e8b17
Revises: 8b5e1f3a7c92
Create Date: 2026-11-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c6f2a4e8b17"
down_revision: Union[str, Sequence[str], None] = "8b5e1f3a7c92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "claimed_at")
