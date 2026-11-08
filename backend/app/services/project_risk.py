from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import AuditLog, Project, Task, User

SCORING_VERSION = "1.0"
HEALTHY_MAX_SCORE = 19
WATCH_MAX_SCORE = 49
STALE_AFTER = timedelta(days=14)
DUE_SOON_WITHIN = timedelta(days=7)
WORKLOAD_MIN_TASKS = 3
WORKLOAD_BOTTLENECK_TASKS = 5

WEIGHTS = {
    "overdue_task": 12,
    "overdue_task_cap": 36,
    "due_soon_task": 6,
    "due_soon_task_cap": 18,
    "blocked_task": 8,
    "blocked_task_cap": 24,
    "stale_task": 3,
    "stale_task_cap": 12,
    "stale_project": 8,
    "workload_concentration_60_percent": 10,
    "workload_concentration_75_percent": 15,
}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_points(count: int, weight: int, cap: int) -> int:
    return min(count * weight, cap)


def _risk_label(score: int) -> str:
    if score <= HEALTHY_MAX_SCORE:
        return "healthy"
    if score <= WATCH_MAX_SCORE:
        return "watch"
    return "at_risk"


def score_project_risk(
    project: Project,
    tasks: list[Task],
    blocked_transition_counts: dict[int, int],
    tenant_user_ids: set[int],
    now: datetime,
) -> dict[str, Any]:
    """Score a project from its current tasks and audited status transitions."""
    now = _as_utc(now)
    stale_cutoff = now - STALE_AFTER
    due_soon_cutoff = now + DUE_SOON_WITHIN
    open_tasks = [task for task in tasks if task.status != "completed"]
    overdue_tasks = [
        task
        for task in open_tasks
        if task.due_date is not None and _as_utc(task.due_date) < now
    ]
    due_soon_tasks = [
        task
        for task in open_tasks
        if task.due_date is not None
        and now <= _as_utc(task.due_date) <= due_soon_cutoff
    ]
    blocked_tasks = [task for task in open_tasks if task.status == "blocked"]
    stale_tasks = [
        task
        for task in open_tasks
        if task.updated_at is not None and _as_utc(task.updated_at) <= stale_cutoff
    ]
    stale_project = (
        project.status in {"planned", "active", "on_hold"}
        and project.updated_at is not None
        and _as_utc(project.updated_at) <= stale_cutoff
    )

    assigned_open_counts = Counter(
        task.assigned_to
        for task in open_tasks
        if task.assigned_to is not None and task.assigned_to in tenant_user_ids
    )
    assigned_open_total = sum(assigned_open_counts.values())
    top_assignee_id, top_assignee_tasks = (
        max(assigned_open_counts.items(), key=lambda item: (item[1], -item[0]))
        if assigned_open_counts
        else (None, 0)
    )
    workload_share = (
        top_assignee_tasks / assigned_open_total if assigned_open_total else 0.0
    )
    if assigned_open_total >= WORKLOAD_MIN_TASKS and workload_share >= 0.75:
        workload_points = WEIGHTS["workload_concentration_75_percent"]
    elif assigned_open_total >= WORKLOAD_MIN_TASKS and workload_share >= 0.60:
        workload_points = WEIGHTS["workload_concentration_60_percent"]
    else:
        workload_points = 0

    reasons: list[dict[str, Any]] = []
    score = 0
    reason_specs = (
        (
            "overdue_tasks",
            len(overdue_tasks),
            WEIGHTS["overdue_task"],
            WEIGHTS["overdue_task_cap"],
            "open tasks are overdue",
        ),
        (
            "due_soon_tasks",
            len(due_soon_tasks),
            WEIGHTS["due_soon_task"],
            WEIGHTS["due_soon_task_cap"],
            "open tasks are due within 7 days",
        ),
        (
            "blocked_tasks",
            len(blocked_tasks),
            WEIGHTS["blocked_task"],
            WEIGHTS["blocked_task_cap"],
            "open tasks are currently blocked",
        ),
        (
            "stale_tasks",
            len(stale_tasks),
            WEIGHTS["stale_task"],
            WEIGHTS["stale_task_cap"],
            "open tasks have not been updated in at least 14 days",
        ),
    )
    for code, count, weight, cap, description in reason_specs:
        if count:
            points = _bounded_points(count, weight, cap)
            score += points
            reasons.append(
                {
                    "code": code,
                    "message": f"{count} {description}.",
                    "count": count,
                    "points": points,
                }
            )
    if stale_project:
        score += WEIGHTS["stale_project"]
        reasons.append(
            {
                "code": "stale_project",
                "message": "The project has not been updated in at least 14 days.",
                "count": 1,
                "points": WEIGHTS["stale_project"],
            }
        )
    if workload_points:
        score += workload_points
        reasons.append(
            {
                "code": "workload_concentration",
                "message": (
                    f"Assignee {top_assignee_id} owns {top_assignee_tasks} of "
                    f"{assigned_open_total} open assigned tasks "
                    f"({workload_share:.0%})."
                ),
                "count": top_assignee_tasks,
                "points": workload_points,
            }
        )

    repeated_blocked: dict[int, int] = {
        task_id: count
        for task_id, count in blocked_transition_counts.items()
        if count >= 2
    }
    task_by_id = {task.id: task for task in tasks}
    bottlenecks: list[dict[str, Any]] = []
    for task_id, transition_count in sorted(repeated_blocked.items()):
        task = task_by_id.get(task_id)
        if task is None:
            continue
        bottlenecks.append(
            {
                "type": "repeated_blocked_task",
                "title": f"Task {task_id} has repeatedly entered the blocked state.",
                "task_ids": [task_id],
                "assignee_id": (
                    task.assigned_to
                    if task.assigned_to in tenant_user_ids
                    else None
                ),
                "count": transition_count,
                "recommendation": (
                    "Review the recorded blocker with the project owner, agree "
                    "on a concrete next action, and update the task status when resolved."
                ),
            }
        )

    if top_assignee_id is not None and top_assignee_tasks >= WORKLOAD_BOTTLENECK_TASKS:
        bottleneck_task_ids = sorted(
            task.id
            for task in open_tasks
            if task.assigned_to == top_assignee_id and task.id is not None
        )
        bottlenecks.append(
            {
                "type": "concentrated_workload",
                "title": (
                    f"Assignee {top_assignee_id} has {top_assignee_tasks} open tasks "
                    "in this project."
                ),
                "task_ids": bottleneck_task_ids,
                "assignee_id": top_assignee_id,
                "count": top_assignee_tasks,
                "recommendation": (
                    "Review priorities and available capacity; reassign work only "
                    "after confirming other team members can take it."
                ),
            }
        )

    if not reasons:
        reasons.append(
            {
                "code": "no_signals",
                "message": "No configured project risk signals were detected.",
                "count": 0,
                "points": 0,
            }
        )
    final_score = min(score, 100)
    return {
        "risk_score": final_score,
        "risk_label": _risk_label(final_score),
        "reasons": reasons,
        "bottlenecks": bottlenecks,
        "metrics": {
            "open_tasks": len(open_tasks),
            "overdue_tasks": len(overdue_tasks),
            "due_soon_tasks": len(due_soon_tasks),
            "blocked_tasks": len(blocked_tasks),
            "stale_tasks": len(stale_tasks),
            "assigned_open_tasks": assigned_open_total,
            "top_assignee_open_tasks": top_assignee_tasks,
        },
    }


def analyze_project_risk(
    db: Session,
    project: Project,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _as_utc(now or datetime.now(timezone.utc))
    tasks = db.query(Task).filter(Task.project_id == project.id).all()
    tenant_user_ids = {
        row[0]
        for row in db.query(User.id).filter(User.tenant_id == project.tenant_id).all()
    }
    task_ids = [task.id for task in tasks]
    blocked_transition_counts: dict[int, int] = defaultdict(int)
    if task_ids:
        audit_rows = (
            db.query(AuditLog.resource_id, AuditLog.metadata_json)
            .filter(
                AuditLog.tenant_id == project.tenant_id,
                AuditLog.resource_type == "task",
                AuditLog.action == "task_status_changed",
                AuditLog.resource_id.in_(task_ids),
            )
            .all()
        )
        for task_id, metadata in audit_rows:
            if isinstance(metadata, dict) and metadata.get("new_status") == "blocked":
                blocked_transition_counts[task_id] += 1

    result = score_project_risk(
        project,
        tasks,
        blocked_transition_counts,
        tenant_user_ids,
        generated_at,
    )
    return {
        "project_id": project.id,
        "project_name": project.name,
        "project_status": project.status,
        **result,
        "generated_at": generated_at,
        "scoring_version": SCORING_VERSION,
    }
