# NOVIQ API: audit logging

## Dashboard and analytics

All dashboard endpoints require a bearer JWT for a user whose current
database role is `owner`, `admin`, `manager`, or `member`. The tenant is taken
from the authenticated user's database record; tenant identifiers supplied by
clients are ignored. Results are tenant-scoped, including task aggregates
through each task's project. No dashboard endpoint changes data.

### `GET /dashboard/overview`

Returns project and task totals, active/completed projects, completed/pending/
overdue tasks, and completion percentages. `start_date` and `end_date` are
optional ISO `YYYY-MM-DD` dates that filter project and task creation dates
inclusively. With neither supplied, the overview covers all records. When both
are supplied, the range may span at most 366 days. Project completion
percentage is completed projects divided by projects in scope; task completion
percentage is completed tasks divided by tasks in scope. Pending tasks are
all tasks not in `completed`, including blocked tasks. Overdue tasks are
non-completed tasks whose due timestamp is before the current UTC time.

### `GET /dashboard/tasks`

Returns task counts grouped by status and priority, overdue and upcoming task
deadlines, and a daily completed-task trend. `start_date` and `end_date` are
optional inclusive `YYYY-MM-DD` dates, with a maximum span of 366 days.
Without them, the analytical range is the trailing 30 calendar days including
today. Status/priority counts and team workload use task creation dates;
completion trends use the task's last update date for tasks currently in the
completed state. Overdue deadlines use due dates within the analytical range.
Upcoming deadlines start today and end at `end_date`, or 30 days from today if
`end_date` is omitted. Each deadline list is ordered by date and capped at 100.

### `GET /dashboard/projects`

Returns project counts grouped by status, task completion progress for each
project created in the analytical date range, and upcoming project deadlines.
The date parameters and default range match `/dashboard/tasks`. Progress uses
all current tasks in each included project, not only tasks created in the
date range. Upcoming deadlines include non-completed/non-cancelled projects
from today through `end_date`, or the next 30 days when `end_date` is omitted.
The deadline list is ordered by date and capped at 100.

### `GET /dashboard/team`

Returns one entry for each user in the authenticated tenant, including members
with no assigned tasks. Assigned, completed, and pending counts include tasks
created in the analytical date range; workload percentage is that member's
share of all assigned tasks in the returned team. The date parameters and
default range match `/dashboard/tasks`.

Example response from `GET /dashboard/overview`:

```json
{
  "start_date": null,
  "end_date": null,
  "projects": {
    "total": 12,
    "active": 5,
    "completed": 3,
    "completion_percentage": 25.0
  },
  "tasks": {
    "total": 80,
    "completed": 30,
    "pending": 50,
    "overdue": 7,
    "completion_percentage": 37.5
  }
}
```

Analytics responses include the effective `start_date` and `end_date`,
distribution arrays of `{ "name", "count" }`, and the corresponding
project/team/deadline arrays. Empty datasets return zero counts and
percentages, with empty arrays where there are no matching records.

## Project risk and bottleneck analysis

### `GET /projects/{project_id}/risk-analysis`

Requires a bearer JWT for a current database user with a tenant role of
`owner`, `admin`, `manager`, or `member`. The project is looked up using both
its ID and the authenticated user's tenant ID; a missing or cross-tenant
project returns `404`. The response contains a `risk_score` from 0 to 100,
`risk_label` (`healthy`, `watch`, or `at_risk`), evidence-based `reasons`,
`bottlenecks` with recommended actions, current `metrics`, UTC `generated_at`,
and `scoring_version`.

The deterministic, rule-based scoring configuration is centrally versioned as
`1.0`. It is an operational indicator, not an ML prediction, probability, or
guarantee. `healthy` is 0–19, `watch` is 20–49, and `at_risk` is 50–100.
Contributions are added and the final total is capped at 100:

| Signal | Weight |
| --- | --- |
| Each open overdue task | 12 points, at most 36 |
| Each open task due within the next 7 days | 6 points, at most 18 |
| Each currently blocked open task | 8 points, at most 24 |
| Each open task not updated for at least 14 days | 3 points, at most 12 |
| Active/planned/on-hold project not updated for at least 14 days | 8 points |
| Highest assignee share at or above 60% of at least 3 open assigned tasks | 10 points |
| Highest assignee share at or above 75% of at least 3 open assigned tasks | 15 points instead |

“Open” means any task not in `completed`; a task can contribute to more than
one signal. Missing due dates do not incur deadline points. All timestamps are
compared in UTC; a naive stored timestamp is interpreted as UTC. Staleness uses
the existing `updated_at` values and does not infer that work was inactive for
reasons beyond the absence of updates.

Bottlenecks include tasks that entered `blocked` at least twice according to
`task_status_changed` audit records and an assignee with at least 5 open tasks
in the project. Recommendations are tied to those observable conditions.
Assignee IDs are used only to describe task allocation; no personal,
demographic, or protected attributes are used. The current schema has no task
dependency/predecessor relationship, so delayed predecessor detection is not
implemented. Audit history only reflects events recorded since audit logging
was enabled; older blocked-state repetitions may not be detectable.

Example:

```json
{
  "project_id": 31,
  "project_name": "Website refresh",
  "project_status": "active",
  "risk_score": 60,
  "risk_label": "at_risk",
  "reasons": [
    {
      "code": "overdue_tasks",
      "message": "3 open tasks are overdue.",
      "count": 3,
      "points": 36
    },
    {
      "code": "blocked_tasks",
      "message": "2 open tasks are currently blocked.",
      "count": 2,
      "points": 16
    },
    {
      "code": "stale_project",
      "message": "The project has not been updated in at least 14 days.",
      "count": 1,
      "points": 8
    }
  ],
  "bottlenecks": [],
  "metrics": {
    "open_tasks": 8,
    "overdue_tasks": 3,
    "due_soon_tasks": 0,
    "blocked_tasks": 2,
    "stale_tasks": 0,
    "assigned_open_tasks": 0,
    "top_assignee_open_tasks": 0
  },
  "generated_at": "2026-11-08T09:51:02Z",
  "scoring_version": "1.0"
}
```

## Supported audit events

| Action | Resource | When recorded |
| --- | --- | --- |
| `user_created` | user | Initial account registration |
| `member_created` | user | A tenant owner or admin creates a member |
| `role_updated` | user | A member's role changes |
| `project_created` | project | A project is created |
| `project_updated` | project | A project is updated |
| `project_status_changed` | project | A valid project lifecycle status transition occurs |
| `project_manager_assigned` | project | A project manager is assigned, changed, or unassigned |
| `project_deleted` | project | A project is deleted |
| `task_created` | task | A task is created |
| `task_updated` | task | One or more task fields are updated |
| `task_assigned` | task | A task is assigned, reassigned, or unassigned |
| `task_status_changed` | task | A task's status changes |
| `task_deleted` | task | A task is deleted |

Member deactivation/removal is not currently supported. Events contain the actor
user ID, tenant ID, action, resource type and ID, UTC creation time, and a
limited set of operation metadata (changed field names, role/status transitions,
project/assignee IDs). The audit utility drops metadata outside its allowlist and
discards secret-like keys. Passwords and user-provided descriptions are not
copied into audit metadata.

## Task management

Every task belongs to one project. Task endpoints use the project's tenant for
scoping and require a valid bearer JWT for a current user in that tenant.
Tenant members may list and view tasks. Creating and updating tasks requires
the current database role `owner`, `admin`, or `manager`; only `owner` and
`admin` may delete tasks.

### `POST /tasks/`

Creates a task in an existing tenant project. `project_id` and `title` are
required. Optional fields are `description`, `status`, `priority`, `start_date`,
`due_date`, and `assigned_to`. Status defaults to `todo`; priority defaults to
`medium`. Status values: `todo`, `in_progress`, `in_review`, `completed`,
`blocked`. Priority values: `low`, `medium`, `high`, `critical`. A supplied
assignee must be a user in the same tenant as the project.

```json
{
  "project_id": 31,
  "title": "Review homepage",
  "description": "Check the responsive layout",
  "status": "todo",
  "priority": "high",
  "start_date": "2026-11-10",
  "due_date": "2026-11-20T17:00:00Z",
  "assigned_to": 12
}
```

### `GET /tasks/` and `GET /tasks/{task_id}`

The list endpoint returns `items`, `page`, `limit`, `total`, and `total_pages`.
It supports these query parameters:

| Parameter | Meaning |
| --- | --- |
| `project_id` | Filter to a project in the authenticated tenant |
| `assigned_to` | Filter by assignee user ID |
| `status` | Exact task status |
| `priority` | Exact task priority |
| `search` | Case-insensitive substring in task title |
| `page` | 1-based page number (default `1`) |
| `limit` | Results per page, from `1` to `100` (default `10`) |
| `sort_by` | `created_at`, `updated_at`, `title`, `status`, `priority`, `start_date`, or `due_date` |
| `sort_order` | `asc` or `desc` (default `desc`) |

`assigned_to` is retained as the assignee filter name for compatibility with
the existing API. Tasks or projects from another tenant are returned as `404`
for item lookups and are never included in list results.

### `PUT /tasks/{task_id}`

Updates only the supplied fields. The project can be changed to another
project in the same tenant. Assignees can be changed or cleared with
`"assigned_to": null`. Status and priority must match the supported values.
`start_date` is a `YYYY-MM-DD` date and must not be after `due_date` (an
ISO-8601 datetime). Invalid dates or enum values return `422`; a disallowed
status transition returns `409`; unknown or cross-tenant project/assignee
references return `404`.

Allowed status transitions:

| Current status | Next status |
| --- | --- |
| `todo` | `in_progress`, `blocked` |
| `in_progress` | `todo`, `in_review`, `completed`, `blocked` |
| `in_review` | `in_progress`, `completed`, `blocked` |
| `blocked` | `todo`, `in_progress` |
| `completed` | No further transition |

Example response:

```json
{
  "id": 45,
  "project_id": 31,
  "title": "Review homepage",
  "description": "Check the responsive layout",
  "status": "in_progress",
  "priority": "high",
  "start_date": "2026-11-10",
  "due_date": "2026-11-20T17:00:00Z",
  "assigned_to": 12,
  "created_at": "2026-11-08T06:08:00Z",
  "updated_at": "2026-11-08T06:10:00Z"
}
```

### `DELETE /tasks/{task_id}`

Only owners and admins may delete a task. Successful task changes and their
audit entries are committed in one database transaction. Unchanged updates
produce no audit records; status and assignment changes use dedicated events.

## Project management

All project endpoints require a valid bearer JWT for a current user in the
project's tenant. Any tenant member may view projects. Creating and updating
projects requires the current database role `owner`, `admin`, or `manager`;
deletion requires `owner` or `admin`. A client-supplied tenant ID is never used.

### `POST /projects/`

Creates a project. `name` is required. `description`, `start_date`,
`target_deadline`, `completion_date`, and `project_manager_id` are optional.
Status values are `planned`, `active`, `on_hold`, `completed`, and `cancelled`;
new projects default to `planned`. A project manager must be a `manager` user in
the same tenant. Dates use `YYYY-MM-DD`.

```json
{
  "name": "Website refresh",
  "description": "Rework the company website",
  "status": "planned",
  "start_date": "2026-11-10",
  "target_deadline": "2027-01-31",
  "project_manager_id": 12
}
```

The response includes the project ID, tenant ID, lifecycle status and dates,
manager ID, and creation/update timestamps.

### `GET /projects/` and `GET /projects/{project_id}`

Project listing is tenant-scoped and paginated. Optional list filters are:

| Parameter | Meaning |
| --- | --- |
| `status` | Exact lifecycle status |
| `manager_id` | Assigned project manager's user ID |
| `search` | Case-insensitive match in project name or description |
| `page` | 1-based page number (default `1`) |
| `page_size` | Results per page, from `1` to `100` (default `50`) |

The list response contains `items`, `page`, `page_size`, `total`, and
`total_pages`. A project outside the caller's tenant is returned as `404`.

### `PUT /projects/{project_id}`

Updates only the supplied fields. An empty update or unchanged field values do
not create audit events. A completion date is set to the current UTC date when a
project transitions to `completed` without an explicit date.

Allowed transitions:

| Current status | Next status |
| --- | --- |
| `planned` | `active`, `on_hold`, `cancelled` |
| `active` | `on_hold`, `completed`, `cancelled` |
| `on_hold` | `active`, `completed`, `cancelled` |
| `completed` | No further status transition |
| `cancelled` | No further status transition |

`start_date` must not follow `target_deadline`. Completion dates are permitted
only for completed projects and cannot precede the start date. Invalid dates or
status values return `422`; a disallowed transition returns `409`. Invalid or
cross-tenant project managers return `404`.

### `DELETE /projects/{project_id}`

Only owners and admins may delete. Projects with existing tasks cannot be
deleted; the endpoint returns `409` rather than deleting related task data.
Successful creates, updates, status changes, manager assignments, and deletes
write their audit events in the same transaction as the project mutation.

Example list response:

```json
{
  "items": [
    {
      "id": 31,
      "tenant_id": 7,
      "name": "Website refresh",
      "description": "Rework the company website",
      "status": "planned",
      "start_date": "2026-11-10",
      "target_deadline": "2027-01-31",
      "completion_date": null,
      "project_manager_id": 12,
      "created_at": "2026-11-08T06:08:00Z",
      "updated_at": "2026-11-08T06:08:00Z"
    }
  ],
  "page": 1,
  "page_size": 50,
  "total": 1,
  "total_pages": 1
}
```

## `GET /audit-logs`

Requires a valid bearer JWT and a user whose current database role is `owner` or
`admin`. A stale token for a demoted/deleted user is not sufficient. The tenant
scope is obtained from the authenticated user's database record; clients cannot
select or override a tenant.

Optional query parameters:

| Parameter | Meaning |
| --- | --- |
| `action` | Exact action name |
| `resource_type` | Exact resource type (`user`, `project`, or `task`) |
| `user_id` | Actor user ID, limited to the authenticated tenant |
| `start_date` | Inclusive ISO-8601 datetime with UTC offset |
| `end_date` | Inclusive ISO-8601 datetime with UTC offset |
| `page` | 1-based page number (default `1`) |
| `page_size` | Results per page, from `1` to `100` (default `50`) |

Example:

```http
GET /audit-logs?action=task_status_changed&resource_type=task&page=1&page_size=20
Authorization: Bearer <access-token>
```

Example response:

```json
{
  "items": [
    {
      "id": 42,
      "tenant_id": 7,
      "user_id": 12,
      "action": "task_status_changed",
      "resource_type": "task",
      "resource_id": 31,
      "details": null,
      "metadata": {
        "old_status": "todo",
        "new_status": "in_progress"
      },
      "created_at": "2026-11-08T06:08:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1,
  "total_pages": 1
}
```

Audit records are written in the same database transaction as their associated
mutations. The API exposes no audit create, update, or delete operation.
