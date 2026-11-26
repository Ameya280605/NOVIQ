# Module 17: Security Mapping

This mapping connects NOVIQ's current service boundaries to the proposed
production AWS and student/demo deployment targets. Provider-specific controls
must be verified during deployment; this document does not provision them.
“Existing code” describes controls visible in the repository; “Deployment
requirement” describes controls that must be configured or verified outside
the application.

| Security area | NOVIQ behavior and required deployment controls |
| --- | --- |
| HTTPS and network edge | **Existing code:** The application does not configure the hosting TLS edge or AWS network rules. **Deployment requirement:** Production must terminate HTTPS at the ALB using ACM and allow origin traffic only from the ALB security group. Student/demo deployments must use the host's HTTPS endpoint. Configure secure headers and trust proxy headers only from the known proxy. Do not expose PostgreSQL or Redis publicly. |
| Secrets | **Existing code:** Startup requires `JWT_SECRET_KEY` and checks for at least 32 characters and 16 distinct characters; it does not generate the secret or measure its cryptographic entropy. **Deployment requirement:** Generate the value using a cryptographically secure random-number generator outside the repository, then store it, database credentials, Redis credentials, and object-storage credentials in Secrets Manager or the hosting provider's secret store. Restrict access by workload, never commit or log secrets or include them in images, and use a planned rotation procedure. |
| Database | **Existing code:** SQLAlchemy obtains its connection from `DATABASE_URL`; this alone does not establish private networking, TLS, least privilege, backup, or recovery settings. **Deployment requirement:** Use private networking/security groups on AWS or the managed provider's access controls and SSL connection mode. Keep credentials server-side, apply least privilege, protect backups, verify recovery/retention, and run migrations as a controlled release step. |
| Object storage | **Existing code:** NOVIQ generates opaque `documents/<uuid>` keys and short-lived presigned PUT/GET URLs (configured TTL 1–900 seconds; default 300). The upload request validates a declared size against `DOCUMENT_MAX_SIZE_BYTES` (default 25 MiB) and an allowed MIME-type string; completion checks stored size and content-type metadata against those declarations. The direct presigned PUT does not enforce the actual transfer size before upload or inspect file bytes, so metadata checks are not file-content validation. Upload signing requests server-side encryption (`AES256` by default, or KMS when configured), but completion does not verify the returned encryption metadata. **Deployment requirement:** Decide how to enforce an actual size cap during transfer and validate file content as appropriate. Configure bucket/account public-access blocking, deny public access and non-TLS requests, enforce default encryption and least-privilege bucket permissions, and verify presigned URL and encryption behavior with the selected provider. |
| JWT and authorization | **Existing code:** NOVIQ signs HS256 access tokens with a one-hour expiry, validates them on protected routes, and uses role checks. Its startup check enforces length and character diversity, not cryptographic randomness. **Deployment requirement:** Require TLS, generate the shared signing secret with a CSPRNG, keep it consistent among API replicas and token-validating processes, and coordinate key rotation (which invalidates tokens signed with the old key) with reauthentication. Do not treat a client-supplied tenant ID as authorization. |
| Tenant isolation | **Existing code:** Tenant IDs are present in the schema, and API route examples use tenant-scoped lookups/filters and tenant-aware relationships. This is not a claim that every route or background path has been exhaustively audited. **Deployment requirement:** Verify tenant predicates and authorization in every API, worker, storage, audit, and background-task path. For uploads, downloads, deletion, queue payloads, and admin actions, confirm the referenced resource belongs to the tenant. Retain cross-tenant authorization tests. Provider-level RLS is not assumed to exist or replace application authorization. |
| Redis and rate limiting | **Existing code:** Rate limits use Redis fixed windows. Redis errors in the limiter, and exceptions in the middleware rate-limit path, fail open: the request is allowed so Redis failure does not take the API offline. The application's readiness check also checks Redis. **Deployment requirement:** Restrict Redis to application access, use TLS/authentication where supported, monitor it independently, and test the outage behavior. Recognize that rate limiting is not enforced during a Redis outage; configure and verify any compensating edge controls rather than assuming fail-closed behavior. |
| Logging and audit | **Existing code:** Request middleware emits request ID, method, path, status, duration, outcome, and authenticated tenant/user IDs when available; the API returns `X-Request-ID`. The middleware does not add request bodies or authorization headers to these request logs. Audit-log writes exist in application flows. **Deployment requirement:** Export application, worker, Beat, database, and infrastructure logs with access controls and defined retention. Do not log passwords, tokens, authorization headers, request bodies, secret values, or presigned URLs. Review path fields and provider logs for sensitive identifiers, minimize personal data, and restrict access. |
| Monitoring and admin | **Existing code:** Provider alarms, retention policies, and administration access are not configured by the application. **Deployment requirement:** On AWS, use CloudWatch alarms/log retention and SSM Session Manager; do not open public SSH. Restrict metrics and health endpoints according to load-balancer/monitoring needs. On student providers, restrict dashboards and registry packages, enable account protections, and monitor quota/usage notifications. |

## Operational checks

- Confirm HTTPS and certificate renewal at both application and storage
  boundaries, including presigned URL behavior.
- Generate `JWT_SECRET_KEY` with a CSPRNG, keep it out of source control, and
  verify the configured value passes startup validation.
- Confirm bucket public-access blocking, TLS-only access, default encryption,
  and least-privilege permissions. Test that a declared upload size above the
  configured limit is rejected; separately test actual oversized uploads and
  document the transfer-time limit, since completion metadata checks happen
  after upload. Verify that disallowed MIME declarations are rejected and do
  not treat MIME metadata as proof of file content.
- Confirm database and Redis connections use TLS where supported and that
  secrets are not present in build logs or process diagnostics. Simulate a
  Redis outage and confirm the documented fail-open rate-limit behavior and
  any compensating edge controls.
- Test authorized and cross-tenant denied access for API routes, document
  presign flows, and asynchronous tasks.
- Verify log retention, access, request correlation, and redaction using
  representative failures before enabling production traffic.
- Confirm emergency credential rotation and recovery procedures without
  placing credentials in this repository.
