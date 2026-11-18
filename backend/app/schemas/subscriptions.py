from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PlanResponse(BaseModel):
    id: int
    code: str
    name: str
    description: str | None
    max_users: int | None
    max_projects: int | None
    max_tasks: int | None
    max_documents: int | None
    max_storage_bytes: int | None
    monthly_price_minor: int | None
    annual_price_minor: int | None
    currency: str | None

    model_config = ConfigDict(from_attributes=True)


class SubscriptionResponse(BaseModel):
    id: int
    tenant_id: int
    plan: PlanResponse
    status: str
    current_period_start: datetime
    current_period_end: datetime
    trial_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class SubscriptionPlanUpdate(BaseModel):
    plan_code: str


class SubscriptionCancelResponse(BaseModel):
    subscription: SubscriptionResponse


class UsageMetric(BaseModel):
    used: int
    limit: int | None


class SubscriptionUsageResponse(BaseModel):
    users: UsageMetric
    projects: UsageMetric
    tasks: UsageMetric
    documents: UsageMetric
    storage_bytes: UsageMetric
