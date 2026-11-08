"""Add structured audit metadata and tenant-scoped indexes

Revision ID: b7130a9ef124
Revises: 24cab5a69179
Create Date: 2026-11-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7130a9ef124"
down_revision: Union[str, Sequence[str], None] = "24cab5a69179"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("metadata", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_audit_logs_tenant_created_at",
        "audit_logs",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_audit_logs_tenant_action",
        "audit_logs",
        ["tenant_id", "action"],
    )
    op.create_index(
        "ix_audit_logs_tenant_resource_type",
        "audit_logs",
        ["tenant_id", "resource_type"],
    )
    op.create_index(
        "ix_audit_logs_tenant_user",
        "audit_logs",
        ["tenant_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_tenant_user", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_resource_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_created_at", table_name="audit_logs")
    op.drop_column("audit_logs", "metadata")
