"""Add task start date, lifecycle constraints, and indexes

Revision ID: d68f0ce35a91
Revises: c9ad46b20817
Create Date: 2026-11-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d68f0ce35a91"
down_revision: Union[str, Sequence[str], None] = "c9ad46b20817"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("start_date", sa.Date(), nullable=True))

    op.execute(
        """
        UPDATE tasks
        SET status = CASE lower(status)
            WHEN 'to do' THEN 'todo'
            WHEN 'in progress' THEN 'in_progress'
            WHEN 'in review' THEN 'in_review'
            ELSE lower(status)
        END
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET status = 'todo'
        WHERE status NOT IN ('todo', 'in_progress', 'in_review', 'completed', 'blocked')
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET priority = lower(priority)
        WHERE lower(priority) IN ('low', 'medium', 'high', 'critical')
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET priority = 'medium'
        WHERE priority NOT IN ('low', 'medium', 'high', 'critical')
        """
    )

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_check_constraint(
            "ck_tasks_status",
            "status IN ('todo', 'in_progress', 'in_review', 'completed', 'blocked')",
        )
        batch_op.create_check_constraint(
            "ck_tasks_priority",
            "priority IN ('low', 'medium', 'high', 'critical')",
        )

    op.create_index(
        "ix_tasks_project_status", "tasks", ["project_id", "status"]
    )
    op.create_index(
        "ix_tasks_assigned_status", "tasks", ["assigned_to", "status"]
    )
    op.create_index("ix_tasks_due_date", "tasks", ["due_date"])


def downgrade() -> None:
    op.drop_index("ix_tasks_due_date", table_name="tasks")
    op.drop_index("ix_tasks_assigned_status", table_name="tasks")
    op.drop_index("ix_tasks_project_status", table_name="tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("ck_tasks_priority", type_="check")
        batch_op.drop_constraint("ck_tasks_status", type_="check")
        batch_op.drop_column("start_date")
