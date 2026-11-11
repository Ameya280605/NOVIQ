"""Create transactional outbox events table

Revision ID: 8b5e1f3a7c92
Revises: 7a4d9e2c1f63
Create Date: 2026-11-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8b5e1f3a7c92"
down_revision: Union[str, Sequence[str], None] = "7a4d9e2c1f63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("event_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_details", sa.String(length=2000), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'processed', 'failed')",
            name="ck_outbox_events_status",
        ),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="ck_outbox_events_retry_count",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_tenant_status_created",
        "outbox_events",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_outbox_events_status_created",
        "outbox_events",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_outbox_events_tenant_event_type",
        "outbox_events",
        ["tenant_id", "event_type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_events_tenant_event_type", table_name="outbox_events"
    )
    op.drop_index(
        "ix_outbox_events_status_created", table_name="outbox_events"
    )
    op.drop_index(
        "ix_outbox_events_tenant_status_created",
        table_name="outbox_events",
    )
    op.drop_table("outbox_events")
