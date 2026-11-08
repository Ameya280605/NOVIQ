import os
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.app.services import assistant_tools

ASSISTANT_VERSION = "1.0"
_INJECTION_OR_SECRET_REQUEST = re.compile(
    r"\b(ignore|disregard|override|bypass)\b.{0,60}\b("
    r"instructions?|rules?|polic(?:y|ies)|system prompt|safety)\b"
    r"|\b(reveal|show|print|repeat|disclose)\b.{0,50}\b("
    r"(system|hidden|developer)\s+prompt|instructions?|"
    r"(api|access|refresh|jwt|auth(?:entication)?)\s*"
    r"(key|token)|passwords?|credentials?|secrets?)\b"
    r"|\b(system|hidden|developer)\s+prompt\b"
    r"|\b(api\s*key|password|credentials?|secret|jwt|access token)\b",
    re.IGNORECASE,
)


class AssistantUnavailable(Exception):
    pass


def _provider_mode() -> str:
    provider = os.getenv("NOVIQ_AI_PROVIDER", "local").strip().lower()
    if provider != "local":
        raise AssistantUnavailable(
            "External AI providers are not configured for this application"
        )
    return provider


def _intent(question: str) -> str:
    normalized = question.lower()
    if "risk" in normalized or "bottleneck" in normalized:
        return "risk_explanation"
    if "overdue" in normalized or "past due" in normalized:
        return "overdue_tasks"
    if any(term in normalized for term in ("workload", "assigned tasks", "assignment")):
        return "workload"
    if any(term in normalized for term in ("summary", "summarize", "overview")):
        return "project_summary"
    if "project" in normalized and any(
        term in normalized for term in ("status", "state")
    ):
        return "project_status"
    return "unknown"


def _project_reference(question: str, supplied_id: int | None) -> int | None:
    if supplied_id is not None:
        return supplied_id
    match = re.search(r"\bproject\s+#?(\d+)\b", question, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _render_answer(intent: str, sources: list[dict[str, Any]]) -> str:
    if intent == "project_status":
        if not sources:
            return "No projects are available in your tenant."
        rows = [
            f"Project {item['project_id']} ({item['name']}): {item['status']}."
            for item in sources
        ]
        return " ".join(rows)
    if intent == "overdue_tasks":
        if not sources:
            return "No overdue open tasks were found in your tenant."
        rows = [
            f"Task {item['task_id']} ({item['title']}) in project "
            f"{item['project_id']} is overdue; due {item['due_date']}."
            for item in sources
        ]
        return " ".join(rows)
    if intent == "workload":
        if not sources:
            return "No team members were found in your tenant."
        rows = [
            f"User {item['user_id']} has {item['open_assigned_tasks']} open assigned tasks."
            for item in sources
        ]
        return " ".join(rows)
    if intent == "risk_explanation":
        if not sources:
            return "Provide a project ID to get a project risk explanation."
        item = sources[0]
        explanations = " ".join(reason["message"] for reason in item["reasons"])
        if not explanations:
            explanations = "No configured risk signals were detected."
        return (
            f"Project {item['project_id']} has operational risk score "
            f"{item['risk_score']} ({item['risk_label']}). {explanations}"
        )
    if intent == "project_summary":
        if not sources:
            return "Provide a project ID to get a project summary."
        item = sources[0]
        return (
            f"Project {item['project_id']} ({item['name']}) is {item['status']}; "
            f"it has {item['task_count']} tasks, {item['completed_tasks']} completed, "
            f"and {len(item['overdue_tasks'])} overdue. Risk is "
            f"{item['risk']['risk_score']} ({item['risk']['risk_label']})."
        )
    return (
        "I can answer questions about project status, overdue tasks, team workload, "
        "project risk explanations, and project summaries. Include a project ID "
        "for project-specific results."
    )


def answer_question(
    db: Session,
    tenant_id: int,
    user_id: int,
    question: str,
    project_id: int | None = None,
) -> dict[str, Any]:
    _provider_mode()
    generated_at = datetime.now(timezone.utc)
    if _INJECTION_OR_SECRET_REQUEST.search(question):
        return {
            "answer": (
                "I can’t disclose system instructions, credentials, or secrets. "
                "I can answer approved questions using your tenant’s project and task data."
            ),
            "intent": "refused",
            "sources": [],
            "generated_at": generated_at,
            "assistant_version": ASSISTANT_VERSION,
            "provider": "local_rules",
        }

    intent = _intent(question)
    selected_project_id = _project_reference(question, project_id)
    sources: list[dict[str, Any]]
    if intent == "project_status":
        sources = assistant_tools.project_status(
            db, tenant_id, user_id, selected_project_id
        )
    elif intent == "overdue_tasks":
        sources = assistant_tools.overdue_tasks(
            db, tenant_id, user_id, selected_project_id
        )
    elif intent == "workload":
        sources = assistant_tools.team_workload(db, tenant_id, user_id)
    elif intent == "risk_explanation":
        if selected_project_id is None:
            sources = []
        else:
            sources = [
                assistant_tools.project_risk(
                    db, tenant_id, user_id, selected_project_id
                )
            ]
    elif intent == "project_summary":
        if selected_project_id is None:
            sources = []
        else:
            sources = [
                assistant_tools.project_summary(
                    db, tenant_id, user_id, selected_project_id
                )
            ]
    else:
        intent = "unknown"
        sources = []

    return {
        "answer": _render_answer(intent, sources),
        "intent": intent,
        "sources": sources,
        "generated_at": generated_at,
        "assistant_version": ASSISTANT_VERSION,
        "provider": "local_rules",
    }
