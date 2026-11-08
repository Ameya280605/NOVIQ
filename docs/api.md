# NOVIQ API: audit logging

## Supported audit events

| Action | Resource | When recorded |
| --- | --- | --- |
| `user_created` | user | Initial account registration |
| `member_created` | user | A tenant owner or admin creates a member |
| `role_updated` | user | A member's role changes |
| `project_created` | project | A project is created |
| `project_updated` | project | A project is updated |
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
