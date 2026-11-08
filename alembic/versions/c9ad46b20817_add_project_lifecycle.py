"""Add project lifecycle and manager fields

Revision ID: c9ad46b20817
Revises: b7130a9ef124
Create Date: 2026-11-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9ad46b20817"
down_revision: Union[str, Sequence[str], None] = "b7130a9ef124"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=50),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.add_column(sa.Column("start_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("target_deadline", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("completion_date", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column("project_manager_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_projects_project_manager_id_users",
            "users",
            ["project_manager_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_projects_status",
            "status IN ('planned', 'active', 'on_hold', 'completed', 'cancelled')",
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=50),
            server_default="planned",
            existing_nullable=False,
        )

    op.execute("UPDATE projects SET status = 'active'")
    op.create_index(
        "ix_projects_tenant_status", "projects", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_projects_tenant_manager",
        "projects",
        ["tenant_id", "project_manager_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_projects_tenant_manager", table_name="projects")
    op.drop_index("ix_projects_tenant_status", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("ck_projects_status", type_="check")
        batch_op.drop_constraint(
            "fk_projects_project_manager_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("project_manager_id")
        batch_op.drop_column("completion_date")
        batch_op.drop_column("target_deadline")
        batch_op.drop_column("start_date")
        batch_op.drop_column("status")
