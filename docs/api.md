# NOVIQ API: audit logging

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
