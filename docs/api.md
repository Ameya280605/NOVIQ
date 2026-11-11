# NOVIQ API and background processing

## Notifications

Authenticated users can access only notifications addressed to their own user
ID within their authenticated tenant. Notifications are created from
successfully processed domain events; currently `TaskAssigned` creates a
notification for the event's `assigned_to` user. Other domain events do not
yet identify a recipient and therefore do not create notifications.

| Method and path | Purpose |
| --- | --- |
| `GET /notifications` | List the current user's notifications, newest first. Supports `page` (default 1) and `page_size` (default 50, maximum 100). |
| `GET /notifications/unread-count` | Return the current user's unread notification count. |
| `PATCH /notifications/{id}/read` | Mark one owned notification as read. |
| `PATCH /notifications/read-all` | Mark all current-user notifications as read. |

List responses contain `items`, `page`, `page_size`, `total`, and
`total_pages`. Notification responses include event type, title, message,
entity reference, read status, and creation time; event payloads are never
returned.

## Module 13: domain events and notifications

### Supported domain events

The internal event publisher supports these typed events:

| Event | Emitted by | Payload identifiers |
| --- | --- | --- |
| `TaskAssigned` | Task creation and assignment changes | Tenant, task, assignee, actor |
| `TaskStatusChanged` | Task status changes | Tenant, task, previous/new status, actor |
| `ProjectStatusChanged` | Project status changes | Tenant, project, previous/new status, actor |
| `DocumentUploaded` | Successful document upload completion | Tenant, document, project, uploader |
| `DocumentDeleted` | Successful physical document cleanup | Tenant, document, project, actor |

Each event is serialized into an `outbox_events` row in the same SQLAlchemy
transaction as the business mutation. A transaction rollback removes both the
business change and its event. Event payload tenant IDs must match the outbox
row tenant ID.

### Outbox lifecycle

Outbox rows use these states:

`pending` -> `processing` -> `processed`

Handler or deserialization failures return a row to `pending` with incremented
`retry_count` and safe `error_details`. After three attempts, the row becomes
`failed`. A worker crash leaves a `processing` row with `claimed_at`; claims
older than five minutes are recovered by the polling task. Recovery increments
the retry count, returns the row to `pending`, or marks it `failed` when the
limit is reached. A row at the retry limit cannot remain permanently
`processing`.

PostgreSQL workers claim rows with `FOR UPDATE SKIP LOCKED`, so concurrent
workers do not claim the same pending row. Processing is idempotent: processed
rows are ignored, and TaskAssigned notification creation is deduplicated by
`source_event_id` and recipient. Handler database writes are rolled back before
retry state is committed. Failure logs include event ID, tenant ID, event type,
retry count, status, and exception class only; payloads, credentials, URLs, and
other sensitive data are not logged.

### Celery worker and Beat

The outbox task is registered as
`backend.app.tasks.outbox.process_pending_events`. It polls up to 50 events
per invocation and is scheduled by Beat every minute. Start both processes
separately:

```powershell
celery -A backend.app.celery_app:celery_app beat --loglevel=INFO
celery -A backend.app.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

The worker uses late acknowledgements and rejects tasks when a worker is lost.
The task has a 30-second soft and 45-second hard limit. Temporary database
polling failures retry up to three times. If Redis or the broker is unavailable,
the committed business transaction and outbox row remain durable; the next
successful Beat poll can process the pending row. The outbox is the source of
truth, not Redis.

### Notification delivery limitation

Only `TaskAssigned` currently creates a notification, addressed to the
event's explicit `assigned_to` user after tenant validation. The other four
events contain actor or uploader IDs but do not identify an intended
notification recipient, so their handlers record successful processing without
creating notifications. Recipients must not be inferred from tenant membership,
project managers, actors, or arbitrary users.

## Document metadata foundation (Module 12, Phase 1)

The database now has a tenant-scoped `documents` metadata table related to its
tenant, project, and uploading user. It stores the original filename, storage
key, content type, size, optional checksum, lifecycle status, and timestamps.
Lifecycle statuses are `pending_upload`, `available`, `deletion_pending`,
`failed`, and `deleted`. Database constraints prevent a document's tenant from
differing from its project's tenant or its uploader's tenant.

`DocumentMetadata` is the response metadata schema and intentionally omits the
internal storage key. Document access helpers scope project and document
lookups by tenant; API routes must obtain role-verified user data through the
existing `require_roles` dependency before calling these helpers. The current
role conventions allow all authenticated project roles to read, owner/admin/
manager to manage, and owner/admin to delete.

No document endpoints are added in this phase. Apply the schema with
`alembic upgrade head`.

### Private S3 storage foundation (Phase 2)

The isolated storage service is `backend.app.services.s3_storage.S3Storage`.
It creates short-lived presigned PUT/GET URLs, returns selected object metadata
from `HeadObject`, and deletes objects. Uploads use server-generated opaque
keys, do not set an ACL, and request SSE-S3 (`AES256`) by default. When a KMS
key is configured, uploads request SSE-KMS and return the corresponding
required upload headers. Storage errors are logged without URLs, keys, or AWS
error details and surfaced as a generic service error. Missing required
configuration fails explicitly.

Configure these values in the environment (for local development, `.env`):

| Variable | Required | Purpose |
| --- | --- | --- |
| `AWS_REGION` | Yes | AWS region (also accepts the standard `AWS_DEFAULT_REGION`) |
| `AWS_S3_BUCKET` | Yes | Existing private bucket name |
| `AWS_S3_ENDPOINT_URL` | No | S3-compatible endpoint, for example local development |
| `AWS_S3_KMS_KEY_ID` | No | KMS key ID/ARN/alias; enables SSE-KMS for uploads |
| `S3_PRESIGNED_URL_TTL_SECONDS` | No | URL lifetime, 1–900 seconds; defaults to 300 |
| `DOCUMENT_MAX_SIZE_BYTES` | No | Maximum accepted upload size; defaults to 25 MiB |

Install dependencies with `python -m pip install -r requirements.txt`.
Credentials are not configured in application settings: boto3 uses its standard
credential provider chain (environment, shared config/profile, container
credentials, or instance/workload role). Prefer a deployment role and least
privilege over static credentials. The application does not create buckets.
Before use, configure the bucket with Block Public Access enabled, object
ownership enforced, TLS-only access, and default encryption. If using KMS, the
runtime role needs the relevant KMS permissions as well as bucket access.

Phase 2 does not create endpoints, upload files, or invoke S3. Callers must
authorize the current tenant/project/document before creating a presigned
download URL; a presigned URL is a bearer credential and must not be logged or
persisted. The upload response includes the exact content-type and encryption
headers that the client must send with the PUT request.

### Document upload and retrieval API (Phase 3)

All document paths are nested under `/projects/{project_id}/documents`.
The tenant is taken from the authenticated user after the existing
database-backed role check; tenant IDs and S3 keys are never accepted from the
client. Cross-tenant projects/documents return `404`.

| Method and path | Access | Purpose |
| --- | --- | --- |
| `POST /projects/{project_id}/documents` | owner/admin/manager | Validate metadata, create a `pending_upload` record, and return a presigned PUT URL. |
| `POST /projects/{project_id}/documents/{document_id}/complete` | owner/admin/manager | Verify the private S3 object size and content type, mark it `available`, and audit completion. No request body. |
| `GET /projects/{project_id}/documents` | owner/admin/manager/member | List non-deleted document metadata, sorted newest first. |
| `GET /projects/{project_id}/documents/{document_id}` | owner/admin/manager/member | Get tenant-scoped metadata. |
| `POST /projects/{project_id}/documents/{document_id}/download-url` | owner/admin/manager/member | Verify an available object exists and return a short-lived presigned GET URL. |
| `DELETE /projects/{project_id}/documents/{document_id}` | owner/admin | Soft-delete metadata and queue asynchronous private-object cleanup. |

Initiation request:

```json
{
  "original_filename": "project-plan.pdf",
  "content_type": "application/pdf",
  "size_bytes": 2048
}
```

Allowed content types are PDF, plain text, JPEG, PNG, and OpenXML Word,
Spreadsheet, and Presentation formats. Filenames must be plain basenames with
no path separators/control characters. Size must be positive and no greater
than `DOCUMENT_MAX_SIZE_BYTES` (default 25 MiB). Unknown request fields are
rejected.

Initiation response:

```json
{
  "document": {
    "id": 17,
    "project_id": 4,
    "uploaded_by": 8,
    "original_filename": "project-plan.pdf",
    "content_type": "application/pdf",
    "size_bytes": 2048,
    "checksum": null,
    "status": "pending_upload",
    "created_at": "2026-11-09T12:00:00Z",
    "updated_at": "2026-11-09T12:00:00Z",
    "deleted_at": null
  },
  "upload_url": "<short-lived presigned URL>",
  "headers": {
    "Content-Type": "application/pdf",
    "x-amz-server-side-encryption": "AES256"
  },
  "expires_in": 300
}
```

The client must PUT the bytes directly to `upload_url`, sending the returned
headers. Then call the bodyless completion endpoint. Missing objects and
content-type/size mismatches return `409` and leave the record pending; an S3
or database outage returns a generic `503` without recording a success event.
Repeating completion for an already available record returns its metadata
without another S3 operation or audit event.

List responses contain `items`, `page`, `page_size`, `total`, and
`total_pages`; query defaults are `page=1` and `page_size=50` (maximum 100).
Metadata responses intentionally omit the storage key.

Download URL responses have the shape:

```json
{
  "download_url": "<short-lived presigned URL>",
  "expires_in": 300
}
```

Only available documents can be downloaded. Presigned URLs are returned only
after authorization and existence checks; treat them as bearer credentials.
Successful initiation, completion, listing, metadata access, and download URL
creation produce audit events. Audit metadata contains safe document/project
identifiers, status, content type, and size only; it excludes filenames,
storage keys, URLs, credentials, and file contents.

### Document deletion and cleanup reliability (Phase 4)

`DELETE /projects/{project_id}/documents/{document_id}` soft-deletes the
document immediately by setting `deleted_at` and `deletion_pending` in one
database transaction with the `document_deletion_requested` audit event.
Deleted/pending records are excluded from normal listing and retrieval. The
route returns `202 Accepted` with the document ID, current cleanup status, and
whether the cleanup job was queued. Repeated deletion requests are
idempotent: they do not create duplicate audits or enqueue duplicate work.

After commit, a tenant/document-scoped Celery job deletes the S3 object. Only
after S3 confirms deletion does it transition the record to `deleted` and write
`document_deleted`. Retries repeat the idempotent S3 delete; a database failure
after storage deletion is safe to retry. Transient S3 and database operational
failures retry up to five times with exponential delays capped at 300 seconds.
Permanent S3 errors fail the Celery delivery but leave the database state
`deletion_pending`, never falsely reporting physical cleanup completion.

Celery Beat runs document cleanup recovery every 15 minutes. It re-enqueues
stale `deletion_pending` work and marks/cleans abandoned `pending_upload`
records after `DOCUMENT_PENDING_UPLOAD_TTL_SECONDS` (default 24 hours).
`DOCUMENT_CLEANUP_RECOVERY_GRACE_SECONDS` (default 900 seconds) prevents a
recovery scan from immediately duplicating recently started cleanup. If the
broker is unavailable after the database commit, the soft deletion remains
committed and the periodic recovery scan queues the cleanup later.

Run the scheduler alongside the worker using the existing Celery commands:

```powershell
celery -A backend.app.celery_app:celery_app beat --loglevel=INFO
celery -A backend.app.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

Cleanup logs contain tenant/document/task identifiers and error classes only;
they never include storage keys, presigned URLs, file data, or AWS credentials.

## Redis and Celery foundation

The application uses Redis for the Celery broker and result backend. Redis
connections in the API are created lazily and closed at application shutdown.
The test task is `backend.app.tasks.health.echo`; it returns the supplied
message and a success status.

Copy `.env.example` to `.env` and set `DATABASE_URL` and `JWT_SECRET_KEY` to
deployment-specific values. Redis/Celery defaults are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis client and default broker/result backend |
| `CELERY_BROKER_URL` | `REDIS_URL` | Celery message broker |
| `CELERY_RESULT_BACKEND` | `REDIS_URL` | Celery task results |

Module 13 uses no additional broker or event-stream service. The following
database/outbox settings are currently code constants: three processing
attempts, five-minute stale-claim recovery, and a 50-event polling batch.
Document cleanup has separate settings for pending-upload TTL and recovery
grace, documented above.

Keep credentials in environment configuration; do not commit `.env`. Install
Python dependencies from the repository root:

```powershell
python -m pip install -r requirements.txt
```

Start Redis locally with Docker:

```powershell
docker run --name noviq-redis -p 6379:6379 -d redis:7-alpine
```

In separate terminals from the repository root, start the API and worker:

```powershell
uvicorn backend.app.main:app --reload
```

```powershell
celery -A backend.app.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

`--pool=solo` is suitable for the local Windows development environment. On
supported Unix deployments, omit that option to use Celery's default worker
pool.

Submit and inspect the test task:

```powershell
python -c "from backend.app.tasks.health import echo_task; print(echo_task.delay('phase-1-test').id)"
celery -A backend.app.celery_app:celery_app result <task-id>
```

`GET /health/redis` pings Redis. `GET /health/worker` pings Celery workers
through the broker. Both return `200` with `{"status":"ok"}` when healthy and
`503` with a generic unavailable status when the dependency is unavailable.
These checks do not expose connection URLs or exception details.

Focused foundation tests (task execution tests use Celery eager mode and do
not need a running Redis service):

```powershell
python -m unittest backend.tests.test_redis_celery -v
```

Validate the full backend and migration state from the repository root:

```powershell
python -m pytest backend/tests -q
python -m compileall -q backend alembic
alembic heads
alembic upgrade head --sql
git diff --check
```

## Project risk caching

Project risk responses are cached in Redis using keys containing tenant ID,
project ID, and the risk scoring version:
`noviq:project-risk:tenant:{tenant_id}:project:{project_id}:version:{version}`.
The default expiration is 60 seconds and can be changed with
`PROJECT_RISK_CACHE_TTL_SECONDS` in the environment. The value must be a
positive integer.

Project authorization and tenant-scoped lookup happen before the cache is
accessed. Successful project and task creates, updates, and deletes invalidate
the affected project's current-version risk key after the database transaction
commits. Moving a task invalidates both its old and new project keys. A
no-op task/project update does not invalidate.

If Redis is unavailable or a cache entry is unreadable, risk analysis falls
back to the PostgreSQL calculation. Cache write and invalidation failures are
logged without request data or connection details; the underlying mutation is
not rolled back after its database commit. Because an invalidation can fail
during a Redis outage, a previously cached value may remain visible until its
configured TTL expires. Keep this TTL short enough for the application's
freshness requirements.

## Background project risk processing

Successful project creation/update and task creation/update/deletion queue a
tenant-scoped recalculation after the database commit. Project deletion removes
its cache entry but does not queue a calculation for a project that no longer
exists. A failed or no-op mutation does not queue a job. If the broker is
unavailable, the completed API mutation remains committed and a safe failure
event is logged; the periodic scan can enqueue that project again.

The Celery Beat schedule enqueues a check for every existing project every 15
minutes. Run the scheduler separately from the worker and API:

```powershell
celery -A backend.app.celery_app:celery_app beat --loglevel=INFO
```

The worker can run with the Windows-compatible pool shown in the foundation
instructions. On Windows, use separate terminals:

```powershell
celery -A backend.app.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

Each recalculation fetches a project by both `project_id` and `tenant_id`
before reading its tasks or audit history, including on every retry. Jobs retry
transient SQLAlchemy operational errors and Redis access/write errors up to
three times with exponential backoff (1, 2, then 4 seconds). After Redis retries
are exhausted, the worker can still calculate from PostgreSQL and skips Redis
cache writes. Database errors that remain after retries and unexpected
permanent errors are logged and surfaced as failed Celery tasks; they do not
modify project/task records. Recalculation has a 30-second soft and 45-second
hard limit. Periodic scans retry transient database errors up to three times
and have a 270-second soft / 300-second hard limit.

Redis stores the risk cache, not the source-of-truth data. Each invalidation or
queued job receives a version token. Cache writes use an atomic Redis
compare-and-set against that token, so superseded jobs cannot overwrite
newer-version results. A short-lived Redis lock prevents simultaneous
execution, and a per-job completion marker (retained for 24 hours) makes
completed deliveries idempotent.
Duplicate deliveries return without recalculating when a result for the
version already exists. If a lock remains busy with no result, a delivery is
retried; after retries, it is deferred to the periodic scan.

Risk tasks use late acknowledgement and are rejected for redelivery if a worker
process is lost. Celery's publisher retries broker sends up to three times.
Mutation requests commit PostgreSQL changes before attempting cache
invalidation or task publication, so broker/worker failure does not roll back
or lose committed business data. Publication failures emit a structured
`project_risk_job_enqueue_failed` event; the next 15-minute scan can enqueue
the missed calculation. There is no durable transactional outbox, so
recalculation may be delayed until that scan. Structured task logs identify
success, retry, duplicate, deferred, and permanent-failure events using tenant,
project, task, and retry identifiers only; prompts, credentials, and project
content are not logged.

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

## Tenant-aware assistant

### `POST /assistant/query`

Requires a bearer JWT for a current database user with role `owner`, `admin`,
`manager`, or `member`. The request accepts a question and an optional project
ID; extra fields (including `tenant_id`) are rejected. Tenant and actor identity
come only from the authenticated user's current database record.

```json
{
  "question": "Explain the risk for project 31",
  "project_id": 31
}
```

The assistant currently runs in local, deterministic rules mode. It supports
project status, overdue tasks, open task workload by user ID, project risk
explanations, and project summaries. Only explicit read-only tools for these
operations are available. Project-specific tools query the project by both
project ID and authenticated tenant ID. Task lists are tenant-scoped through
their projects. Responses include the source records and their IDs; the
assistant does not infer unavailable details. Results are limited to the
first 50 records for list-style tools.

Questions that do not match a supported operation return an `unknown` intent
and describe supported topics. Project-specific risk/summary questions without
a project ID ask for one. Requests to disclose instructions, credentials, or
secrets are refused without querying the data tools. This is a defense-in-depth
guard; local-only execution means user text is never inserted into an external
model prompt.

No AI provider or AI-specific package is configured in this repository.
`NOVIQ_AI_PROVIDER` defaults to `local`; any non-local value fails closed with
HTTP `503`. No prompts, project data, or credentials are sent externally. An
external provider must not be enabled until data handling, retention, and
tenant-isolation requirements have been explicitly approved and implemented.
Safe usage logs contain only the selected intent, outcome, and local provider
label; prompts and answers are not logged or persisted.

Example response:

```json
{
  "answer": "Project 31 has operational risk score 20 (watch). 2 open tasks are due within 7 days.",
  "intent": "risk_explanation",
  "sources": [
    {
      "project_id": 31,
      "risk_score": 20,
      "risk_label": "watch",
      "reasons": [
        {
          "code": "due_soon_tasks",
          "message": "2 open tasks are due within 7 days.",
          "count": 2,
          "points": 12
        },
        {
          "code": "stale_project",
          "message": "The project has not been updated in at least 14 days.",
          "count": 1,
          "points": 8
        }
      ],
      "bottlenecks": [],
      "scoring_version": "1.0"
    }
  ],
  "generated_at": "2026-11-08T10:02:49Z",
  "assistant_version": "1.0",
  "provider": "local_rules"
}
```

The assistant is not a general-purpose chatbot and does not execute SQL, mutate
records, or answer questions outside the approved tools. Risk information uses
the Module 9 operational score and inherits its audit-history limitations.

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
