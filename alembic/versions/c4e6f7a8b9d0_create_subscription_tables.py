"""create subscription tables and seed plans

Revision ID: c4e6f7a8b9d0
Revises: b2d7f4a91c38
Create Date: 2026-11-15

"""
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4e6f7a8b9d0"
down_revision: Union[str, Sequence[str], None] = "b2d7f4a91c38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("max_users", sa.Integer(), nullable=True),
        sa.Column("max_projects", sa.Integer(), nullable=True),
        sa.Column("max_tasks", sa.Integer(), nullable=True),
        sa.Column("max_documents", sa.Integer(), nullable=True),
        sa.Column("max_storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("monthly_price_minor", sa.Integer(), nullable=True),
        sa.Column("annual_price_minor", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_users IS NULL OR max_users >= 0",
            name="ck_plans_max_users",
        ),
        sa.CheckConstraint(
            "max_projects IS NULL OR max_projects >= 0",
            name="ck_plans_max_projects",
        ),
        sa.CheckConstraint(
            "max_tasks IS NULL OR max_tasks >= 0",
            name="ck_plans_max_tasks",
        ),
        sa.CheckConstraint(
            "max_documents IS NULL OR max_documents >= 0",
            name="ck_plans_max_documents",
        ),
        sa.CheckConstraint(
            "max_storage_bytes IS NULL OR max_storage_bytes >= 0",
            name="ck_plans_max_storage_bytes",
        ),
        sa.CheckConstraint(
            "monthly_price_minor IS NULL OR monthly_price_minor >= 0",
            name="ck_plans_monthly_price_minor",
        ),
        sa.CheckConstraint(
            "annual_price_minor IS NULL OR annual_price_minor >= 0",
            name="ck_plans_annual_price_minor",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_plans_code"),
    )
    op.create_index("ix_plans_active", "plans", ["is_active"])

    op.create_table(
        "plan_entitlements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("feature_key", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id",
            "feature_key",
            name="uq_plan_entitlements_plan_feature",
        ),
    )
    op.create_index(
        "ix_plan_entitlements_plan_feature",
        "plan_entitlements",
        ["plan_id", "feature_key"],
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("provider_customer_id", sa.String(length=255), nullable=True),
        sa.Column(
            "provider_subscription_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'paused', "
            "'canceled', 'incomplete', 'unpaid')",
            name="ck_subscriptions_status",
        ),
        sa.CheckConstraint(
            "current_period_end > current_period_start",
            name="ck_subscriptions_period_dates",
        ),
        sa.CheckConstraint(
            "canceled_at IS NULL OR status = 'canceled'",
            name="ck_subscriptions_canceled_at",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_subscriptions_tenant_id"),
        sa.UniqueConstraint(
            "provider_subscription_id",
            name="uq_subscriptions_provider_subscription_id",
        ),
    )
    op.create_index(
        "ix_subscriptions_tenant_status",
        "subscriptions",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_subscriptions_status_period_end",
        "subscriptions",
        ["status", "current_period_end"],
    )
    op.create_index(
        "ix_subscriptions_provider_subscription",
        "subscriptions",
        ["provider", "provider_subscription_id"],
    )

    now = datetime.now(timezone.utc)
    plans = [
        {
            "id": 1,
            "code": "free",
            "name": "Free",
            "description": "Essential workspace features",
            "max_users": 3,
            "max_projects": 3,
            "max_tasks": 100,
            "max_documents": 100,
            "max_storage_bytes": 1 * 1024**3,
            "monthly_price_minor": 0,
            "annual_price_minor": 0,
            "currency": "USD",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": 2,
            "code": "starter",
            "name": "Starter",
            "description": "Growing team capabilities",
            "max_users": 10,
            "max_projects": 25,
            "max_tasks": 1000,
            "max_documents": 1000,
            "max_storage_bytes": 10 * 1024**3,
            "monthly_price_minor": None,
            "annual_price_minor": None,
            "currency": "USD",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": 3,
            "code": "professional",
            "name": "Professional",
            "description": "Advanced features for established teams",
            "max_users": 50,
            "max_projects": 100,
            "max_tasks": 10000,
            "max_documents": 10000,
            "max_storage_bytes": 100 * 1024**3,
            "monthly_price_minor": None,
            "annual_price_minor": None,
            "currency": "USD",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": 4,
            "code": "enterprise",
            "name": "Enterprise",
            "description": "Unlimited enterprise workspace",
            "max_users": None,
            "max_projects": None,
            "max_tasks": None,
            "max_documents": None,
            "max_storage_bytes": None,
            "monthly_price_minor": None,
            "annual_price_minor": None,
            "currency": "USD",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
    ]
    op.bulk_insert(
        sa.table(
            "plans",
            sa.column("id", sa.Integer),
            sa.column("code", sa.String),
            sa.column("name", sa.String),
            sa.column("description", sa.String),
            sa.column("max_users", sa.Integer),
            sa.column("max_projects", sa.Integer),
            sa.column("max_tasks", sa.Integer),
            sa.column("max_documents", sa.Integer),
            sa.column("max_storage_bytes", sa.BigInteger),
            sa.column("monthly_price_minor", sa.Integer),
            sa.column("annual_price_minor", sa.Integer),
            sa.column("currency", sa.String),
            sa.column("is_active", sa.Boolean),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
        ),
        plans,
    )

    entitlement_rows = []
    feature_sets = {
        1: {"document_upload", "basic_analytics"},
        2: {"document_upload", "basic_analytics", "assistant", "audit_logs"},
        3: {
            "document_upload",
            "basic_analytics",
            "assistant",
            "audit_logs",
            "advanced_analytics",
            "project_risk_analysis",
            "custom_integrations",
        },
        4: {
            "document_upload",
            "basic_analytics",
            "assistant",
            "audit_logs",
            "advanced_analytics",
            "project_risk_analysis",
            "custom_integrations",
            "sso",
            "priority_support",
        },
    }
    entitlement_id = 1
    for plan_id, feature_keys in feature_sets.items():
        for feature_key in sorted(feature_keys):
            entitlement_rows.append(
                {
                    "id": entitlement_id,
                    "plan_id": plan_id,
                    "feature_key": feature_key,
                    "enabled": True,
                    "created_at": now,
                }
            )
            entitlement_id += 1
    op.bulk_insert(
        sa.table(
            "plan_entitlements",
            sa.column("id", sa.Integer),
            sa.column("plan_id", sa.Integer),
            sa.column("feature_key", sa.String),
            sa.column("enabled", sa.Boolean),
            sa.column("created_at", sa.DateTime),
        ),
        entitlement_rows,
    )

    tenant_table = sa.table("tenants", sa.column("id", sa.Integer))
    tenant_ids = op.get_bind().execute(
        sa.select(tenant_table.c.id)
    ).scalars()
    op.bulk_insert(
        sa.table(
            "subscriptions",
            sa.column("tenant_id", sa.Integer),
            sa.column("plan_id", sa.Integer),
            sa.column("status", sa.String),
            sa.column("current_period_start", sa.DateTime),
            sa.column("current_period_end", sa.DateTime),
            sa.column("trial_end", sa.DateTime),
            sa.column("cancel_at_period_end", sa.Boolean),
            sa.column("canceled_at", sa.DateTime),
            sa.column("provider", sa.String),
            sa.column("provider_customer_id", sa.String),
            sa.column("provider_subscription_id", sa.String),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
        ),
        [
            {
                "tenant_id": tenant_id,
                "plan_id": 1,
                "status": "active",
                "current_period_start": now,
                "current_period_end": now + timedelta(days=30),
                "trial_end": None,
                "cancel_at_period_end": False,
                "canceled_at": None,
                "provider": None,
                "provider_customer_id": None,
                "provider_subscription_id": None,
                "created_at": now,
                "updated_at": now,
            }
            for tenant_id in tenant_ids
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscriptions_provider_subscription",
        table_name="subscriptions",
    )
    op.drop_index(
        "ix_subscriptions_status_period_end",
        table_name="subscriptions",
    )
    op.drop_index(
        "ix_subscriptions_tenant_status",
        table_name="subscriptions",
    )
    op.drop_table("subscriptions")
    op.drop_index(
        "ix_plan_entitlements_plan_feature",
        table_name="plan_entitlements",
    )
    op.drop_table("plan_entitlements")
    op.drop_index("ix_plans_active", table_name="plans")
    op.drop_table("plans")
