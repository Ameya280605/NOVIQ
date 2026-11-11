"""Add notification source event identity

Revision ID: b2d7f4a91c38
Revises: 9c6f2a4e8b17
Create Date: 2026-11-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d7f4a91c38"
down_revision: Union[str, Sequence[str], None] = "9c6f2a4e8b17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("source_event_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_notifications_source_event",
        "notifications",
        "outbox_events",
        ["source_event_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_notifications_source_event_recipient",
        "notifications",
        ["source_event_id", "recipient_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_notifications_source_event_recipient",
        "notifications",
        type_="unique",
    )
    op.drop_constraint(
        "fk_notifications_source_event",
        "notifications",
        type_="foreignkey",
    )
    op.drop_column("notifications", "source_event_id")
