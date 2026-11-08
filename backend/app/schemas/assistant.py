from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssistantRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    project_id: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized


class AssistantResponse(BaseModel):
    answer: str
    intent: Literal[
        "project_status",
        "overdue_tasks",
        "workload",
        "risk_explanation",
        "project_summary",
        "unknown",
        "refused",
    ]
    sources: list[dict[str, Any]]
    generated_at: datetime
    assistant_version: str
    provider: Literal["local_rules"]
