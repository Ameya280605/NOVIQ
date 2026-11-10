"""Create tenant-scoped documents table

Revision ID: f2a0c9d8417b
Revises: d68f0ce35a91
Create Date: 2026-11-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2a0c9d8417b"
down_revision: Union[str, Sequence[str], None] = "d68f0ce35a91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_users_tenant_id_id",
        "users",
        ["tenant_id", "id"],
        unique=True,
    )
    op.create_index(
        "uq_projects_tenant_id_id",
        "projects",
        ["tenant_id", "id"],
        unique=True,
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_upload', 'available', 'deletion_pending', 'failed')",
            name="ck_documents_status",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_documents_size_bytes"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_documents_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_documents_tenant_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "uploaded_by"],
            ["users.tenant_id", "users.id"],
            name="fk_documents_tenant_uploader",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_documents_storage_key"),
    )
    op.create_index(
        "ix_documents_tenant_project",
        "documents",
        ["tenant_id", "project_id"],
    )
    op.create_index(
        "ix_documents_tenant_status", "documents", ["tenant_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_documents_tenant_status", table_name="documents")
    op.drop_index("ix_documents_tenant_project", table_name="documents")
    op.drop_table("documents")
    op.drop_index("uq_projects_tenant_id_id", table_name="projects")
    op.drop_index("uq_users_tenant_id_id", table_name="users")
