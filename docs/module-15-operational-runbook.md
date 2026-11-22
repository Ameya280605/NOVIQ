# NOVIQ Module 15 Operational Runbook

## Environment

Set these variables before starting the API or worker:

- `DATABASE_URL`: PostgreSQL SQLAlchemy connection URL.
- `JWT_SECRET_KEY`: high-entropy secret of at least 32 characters with at least 16 distinct characters.
- `REDIS_URL`: Redis connection URL.
- `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`: optional Celery Redis overrides.
- `RATE_LIMIT_AUTH_REQUESTS`: authentication requests allowed per auth window.
- `RATE_LIMIT_AUTH_WINDOW_SECONDS`: authentication window duration.
- `RATE_LIMIT_API_REQUESTS`: regular API requests allowed per API window.
- `RATE_LIMIT_API_WINDOW_SECONDS`: regular API window duration.

AWS, S3, document-size, subscription, cache, and cleanup variables documented in `.env.example` are also required for the corresponding features.

Use a secret manager in production. Never copy credentials into logs or source control. Rotate the JWT secret according to incident and key-management policies.

## Startup

1. Configure environment variables and secrets.
2. Run database migrations with Alembic.
3. Start Redis.
4. Start the Celery worker and beat scheduler.
5. Start the FastAPI application with Uvicorn.

The application fails startup when `JWT_SECRET_KEY` is absent or weak.

## Health Endpoints

- `GET /health/live`: process liveness only; it does not check dependencies.
- `GET /health/ready`: readiness check for PostgreSQL, Redis, and Celery worker availability.

Readiness failures return HTTP 503 with the safe `{"detail":{"status":"unavailable"}}` shape. They do not expose credentials or provider errors.

## Metrics

Scrape `GET /metrics` using Prometheus text format.

Available metrics include:

- `noviq_http_requests_total`
- `noviq_http_request_errors_total`
- `noviq_http_request_duration_seconds`

Use low-cardinality labels: method, route template, status, and status class. `/metrics` is excluded from request metrics. Prometheus scrapes are process-local; aggregate across replicas or use a shared metrics layer.

## Request Logging and IDs

Every API response includes `X-Request-ID`. Clients may supply a valid ID; malformed IDs are replaced.

Request logs include request ID, method, route path, status code, duration, outcome, and authenticated tenant/user IDs where available. Do not log request bodies or authorization headers.

## Rate Limiting

Authentication endpoints use the stricter auth limits and are keyed primarily by client IP.
Regular API requests use tenant-keyed limits from verified JWT claims and fall back to a hashed client-IP identity.

Limits are Redis fixed windows. `429 Too Many Requests` responses contain `Retry-After` and `X-Request-ID`. Redis outages fail open to preserve availability.

## Monitoring

Alert on:

- Readiness 503s or sustained dependency health failures.
- Elevated 5xx rates and 429 rates.
- Request-duration histogram regressions.
- Missing JWT validation, Redis connectivity failures, or repeated worker ping failures.
- Unexpected request-ID, log, or metrics errors.

Correlate incidents using `X-Request-ID` and structured request logs.

## Troubleshooting

- `/health/live` fails: inspect the API process, Uvicorn configuration, and startup logs.
- `/health/ready` fails: check PostgreSQL, Redis, and Celery worker status independently.
- JWT 401s: verify clock synchronization and the deployed `JWT_SECRET_KEY`.
- 429s: inspect auth/API window variables and tenant/IP key behavior.
- Redis unavailable: verify `REDIS_URL`, network access, TLS/authentication, and client timeouts; rate limiting fails open.
- Metrics missing: verify `/metrics` reachability and Prometheus scrape configuration.

## Production Deployment

- Deploy behind TLS-terminating infrastructure.
- Restrict `/metrics` to monitoring networks or authenticated scraping.
- Restrict health endpoints if infrastructure does not require public access.
- Use separate production secrets and Redis namespaces where applicable.
- Validate database migrations and JWT secrets before traffic switches.
- Monitor readiness before adding an instance to the load-balancer pool.
- Rotate Redis credentials, JWT secrets, database credentials, AWS credentials, and S3 keys through the organization's incident/key-management process.
