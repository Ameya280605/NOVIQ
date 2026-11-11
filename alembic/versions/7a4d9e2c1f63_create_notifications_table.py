"""Create tenant-scoped notifications table

Revision ID: 7a4d9e2c1f63
Revises: 48ca2bb8ef13
Create Date: 2026-11-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a4d9e2c1f63"
down_revision: Union[str, Sequence[str], None] = "48ca2bb8ef13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("recipient_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.String(length=2000), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "recipient_id"],
            ["users.tenant_id", "users.id"],
            name="fk_notifications_tenant_recipient",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notifications_tenant_recipient_read_created",
        "notifications",
        ["tenant_id", "recipient_id", "is_read", "created_at"],
    )
    op.create_index(
        "ix_notifications_tenant_created_at",
        "notifications",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_notifications_tenant_event_type",
        "notifications",
        ["tenant_id", "event_type"],
    )
    op.create_index(
        "ix_notifications_tenant_entity",
        "notifications",
        ["tenant_id", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_tenant_entity", table_name="notifications")
    op.drop_index(
        "ix_notifications_tenant_event_type", table_name="notifications"
    )
    op.drop_index(
        "ix_notifications_tenant_created_at", table_name="notifications"
    )
    op.drop_index(
        "ix_notifications_tenant_recipient_read_created",
        table_name="notifications",
    )
    op.drop_table("notifications")
