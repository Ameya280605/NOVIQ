"""Add document deletion state and requester attribution

Revision ID: 48ca2bb8ef13
Revises: f2a0c9d8417b
Create Date: 2026-11-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "48ca2bb8ef13"
down_revision: Union[str, Sequence[str], None] = "f2a0c9d8417b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(
            sa.Column("deletion_requested_by", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_documents_tenant_deletion_requester",
            "users",
            ["tenant_id", "deletion_requested_by"],
            ["tenant_id", "id"],
        )
        batch_op.drop_constraint("ck_documents_status", type_="check")
        batch_op.create_check_constraint(
            "ck_documents_status",
            "status IN ('pending_upload', 'available', 'deletion_pending', 'failed', 'deleted')",
        )


def downgrade() -> None:
    op.execute(
        "UPDATE documents SET status = 'deletion_pending' WHERE status = 'deleted'"
    )
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_constraint("ck_documents_status", type_="check")
        batch_op.create_check_constraint(
            "ck_documents_status",
            "status IN ('pending_upload', 'available', 'deletion_pending', 'failed')",
        )
        batch_op.drop_constraint(
            "fk_documents_tenant_deletion_requester", type_="foreignkey"
        )
        batch_op.drop_column("deletion_requested_by")
