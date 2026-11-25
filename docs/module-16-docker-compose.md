# Module 16: Docker Compose

This Compose setup runs PostgreSQL, Redis, the NOVIQ API, one Celery worker, one
Celery Beat scheduler, and a one-shot Alembic migration service. The API,
worker, and Beat wait until the migration service exits successfully. The
application containers use the existing non-root image built from `Dockerfile`.

## Prerequisites and environment

Install Docker Desktop with Docker Compose v2. Create `.env` from `.env.example`
only if you do not already have a `.env`:

```powershell
Copy-Item .env.example .env
```

Keep `.env` local and untracked. Set a strong `JWT_SECRET_KEY` and a
`POSTGRES_PASSWORD` containing only URL-safe alphanumeric characters, since
Compose uses it in the SQLAlchemy connection URL. Set the same PostgreSQL
database, user, and password in `POSTGRES_DB`, `POSTGRES_USER`, and
`POSTGRES_PASSWORD`; the example defaults for database and user are `noviq`.
Do not put secrets in the image or commit them.

Compose overrides `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`, and
`CELERY_RESULT_BACKEND` inside the application containers to use the internal
`postgres` and `redis` hostnames. PostgreSQL and Redis are not published to the
host. The API is available on host port 8000.

## Build and start

Build the application image:

```powershell
docker compose build
```

Start the stack:

```powershell
docker compose up -d
docker compose ps
```

`docker compose up` starts PostgreSQL and Redis, waits for their health checks,
runs `alembic upgrade head`, and starts the API, worker, and the single Beat
service only if migrations succeed. Beat stores its schedule file at
`/tmp/celerybeat-schedule` inside its container, outside the repository.

## Logs

Follow all service logs:

```powershell
docker compose logs -f
```

Follow selected logs or inspect the one-shot migration result:

```powershell
docker compose logs -f api worker beat
docker compose logs migrate
```

## Health checks

Check container health and migration completion:

```powershell
docker compose ps
```

PostgreSQL uses `pg_isready`; Redis uses `redis-cli ping`. The API container
health check calls `GET /health/live` at `http://localhost:8000/health/live`.
For dependency readiness, which also requires a responding worker, run:

```powershell
Invoke-WebRequest http://localhost:8000/health/ready
```

The readiness endpoint returns HTTP 503 until PostgreSQL, Redis, and Celery are
available.

## Migrations

Migrations run automatically on `docker compose up`. Check their result with:

```powershell
docker compose ps -a migrate
docker compose logs migrate
```

To run the migration job explicitly, for example after a new image build:

```powershell
docker compose run --rm migrate
```

The job must exit successfully before the API, worker, or Beat is started.

## Stop and data handling

Stop and remove the containers while preserving database and Redis data:

```powershell
docker compose down
```

Start the stack again with `docker compose up -d`. The named
`postgres_data` and `redis_data` volumes persist across container removal and
recreation. A normal stop or restart does not reset application data.

Only for an intentional, irreversible full data reset, first make and verify
independent backups of both PostgreSQL and Redis data. Then remove the
containers and named volumes:

```powershell
docker compose down --volumes
```

This deletes all data in those Compose-managed volumes. Do not use
`--volumes` as routine cleanup, and do not run this command if you need to
preserve existing data. The Beat schedule is separate and is kept in the
container's temporary directory.
