# Module 17: Proposed Zero-Cost Student Deployment

## Scope

This is a proposal for a non-production student/demo deployment using Render,
Supabase, Upstash, Cloudflare R2, and GitHub Container Registry (GHCR). It is
not the production AWS architecture in
[module-17-aws-architecture.md](./module-17-aws-architecture.md), does not
change the NOVIQ application, and is not a promise that this stack can run
continuously for ₹0.

Provider plans, free allowances, eligibility, service availability, usage
metering, and terms change. Verify current official plan documentation and
account-specific limits immediately before deployment. No pricing or free-tier
guarantee is asserted here. If any required component needs a paid plan or
creates billable usage, stop rather than upgrading.

For every provider, verify whether a payment card or other payment method is
required, whether a trial converts automatically, whether automatic upgrades
are enabled, and whether exceeding any allowance is billed, throttled, or
stopped. Disable automatic upgrades and billable overages where possible.
Notifications and budget alerts are not hard spending caps. If the payment
requirements or overage outcome cannot be established, do not deploy.

## Proposed service mapping

| Provider | Proposed use | Checks before attempting deployment |
| --- | --- | --- |
| Render | Deploy the FastAPI container as the public web service; use separate process services for Celery worker and Celery Beat if the plan supports them. | Verify free-plan service types, sleep/scale-to-zero behavior, deployment limits, outbound networking, health checks, and whether a worker can run continuously without a paid plan. |
| Supabase | Hosted PostgreSQL for NOVIQ relational data. | Verify project pause/compute behavior, connection limits, supported PostgreSQL features/version, SSL mode, backup/restore access, and whether use remains within no-cost limits. Check direct versus pooled connection strings and pooler mode for SQLAlchemy/psycopg compatibility; confirm Alembic migrations work with the selected connection and use a direct or migration-compatible connection if needed. Keep database access server-side; do not expose privileged credentials to a browser. |
| Upstash | Managed Redis for Celery broker/result state and the application's rate-limit/cache usage. | Verify protocol/TLS settings, connection limits, persistence/eviction behavior, throughput/request quota, and what happens when an allowance is exhausted. Confirm Celery-compatible Redis behavior for the selected plan. |
| Cloudflare R2 | Private object storage for documents using its S3-compatible API. | Verify current no-cost limits and overage behavior, private bucket controls, R2 S3 credentials and how boto3's standard credential chain receives them, the account-specific S3 endpoint, and the required signing region. Test boto3 signature version and presigned upload/download URL behavior from the actual client, including URL expiry, content-type and encryption headers. Confirm support for the `AES256` server-side encryption header used by NOVIQ and do not configure a KMS key unless the provider supports that request. |
| GitHub Container Registry (GHCR) | Store the built container image used by Render. | Verify package visibility, storage and transfer limits, organization/account permissions, retention, and any charges or terms for the intended use. Keep private images private and configure registry credentials through the hosting provider's secret facility. |

Use the existing application environment-variable contract where supported:
`DATABASE_URL`, `JWT_SECRET_KEY`, `REDIS_URL`, `CELERY_BROKER_URL`,
`CELERY_RESULT_BACKEND`, `AWS_REGION`, `S3_BUCKET_NAME`, and
`AWS_S3_ENDPOINT_URL`. The S3 client uses boto3's standard AWS credential
provider chain and emits S3-style presigned requests; confirm the correct
provider credential names and API compatibility for R2 before relying on
document uploads. Never bake these values into an image or commit them.

### Supabase database and migrations

- Use the provider's recommended SSL connection mode and verify certificates
  when the selected connection method supports it. Keep the connection string
  and database password only in the host's secret store.
- Verify the selected direct or pooled DSN, pooler mode, connection limits,
  SQLAlchemy/psycopg behavior, and connection lifecycle under the app's
  `create_engine` usage. Do not assume every pooler mode supports every driver
  feature.
- Run Alembic as a serialized, one-off release step before starting or
  switching API and worker processes. Test `alembic upgrade head` against the
  selected Supabase connection method; use a migration-compatible connection
  (often the direct connection when available) rather than assuming a
  transaction pool supports DDL/migration behavior.

## Celery Worker and Beat considerations

The Compose stack runs the API, one Celery worker, and one Beat scheduler as
separate services. Keep the same process separation in this proposal if the
hosting plan permits it:

- The API handles requests; a Celery worker consumes asynchronous jobs.
- Beat is a singleton scheduler for the periodic project-risk,
  document-recovery, outbox, and subscription-expiry jobs. Running more than
  one Beat process can schedule duplicate jobs.
- Confirm each process type is permitted to run continuously on the selected
  free plan. A sleeping web service does not keep a worker or Beat alive.
- The current Beat configuration writes a local schedule file. Confirm whether
  the host filesystem persists across restarts/redeploys; a reset schedule may
  cause jobs to be re-enqueued. Do not add a second Beat process as a
  workaround.
- Free compute can sleep, restart, or impose resource limits. Jobs may be
  delayed, interrupted, or retried. Verify task idempotency and recovery
  behavior before using real data. NOVIQ readiness expects PostgreSQL, Redis,
  and a responding Celery worker, so readiness may remain unavailable if a
  provider sleeps a dependency or worker.

If the free plan cannot host the API, worker, and one Beat process reliably,
record that as a limitation and defer deployment or use only a local Compose
demo. Do not combine processes in a way that masks liveness or silently omits
scheduled work.

## Independent health checks

Check each dependency and process independently; an API readiness result alone
does not identify which component is unavailable:

- **API:** Probe `GET /health/live` for process liveness and
  `GET /health/ready` for application readiness. The current readiness check
  covers PostgreSQL, Redis, and Celery worker availability.
- **Database:** From a trusted backend/operations check, establish an SSL
  connection using the configured DSN and execute a lightweight `SELECT 1`.
  Confirm migrations completed successfully separately.
- **Redis:** Check TLS/authenticated connectivity and a `PING` from the
  application network. Do not expose Redis publicly just to enable probing.
- **Celery worker:** Use a broker-compatible worker ping or equivalent
  process/queue check, and confirm the worker can consume a harmless test job.
- **Celery Beat:** Check the Beat process is running and inspect scheduler
  logs/heartbeat or verify that an expected periodic task is enqueued. Beat is
  a scheduler, not a worker, so a worker ping does not establish Beat health.

Confirm these probes are supported by the selected free plans, do not consume
significant quotas, and do not keep an otherwise-sleeping service awake in a
way that triggers billing.

## Student/demo safety boundary

- Use synthetic/demo records only; do not upload sensitive, personal, customer,
  or production documents.
- Keep database, Redis, storage, and registry credentials in provider-managed
  secrets. Use separate, least-privilege credentials and rotate them after
  accidental exposure.
- Restrict the database to backend use and use TLS. Make the R2 bucket private;
  document delivery should use the existing short-lived presigned URL flow.
- Apply provider-appropriate health checks and inspect usage dashboards
  frequently. Free quotas and their counters are provider-specific.
- Do not assume provider backups, durability, uptime, or support are included
  at no cost. Export any data needed before a service pauses or is removed.

## Verification gates

Before deployment, verify plan eligibility, account-specific usage and
payment-card requirements, trial conversion, automatic upgrades and overage
settings, region/network compatibility, TLS and connection strings, Render
process availability, Supabase SSL, connection pooling, SQLAlchemy/psycopg and
Alembic compatibility, database access and backups, Upstash limits and Celery
compatibility, R2 boto3 credentials/endpoint/signing region/presigned URL
compatibility, GHCR image limits/authentication, and separate API, database,
Redis, worker, and Beat health checks. Do not proceed if any required service
has uncertain billable overage behavior.
