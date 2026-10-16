# Durable work and operations

## Recovery model

Business transactions insert durable jobs with unique deduplication keys into PostgreSQL. They never need to publish directly to Redis. Celery Beat sends a discovery task every five seconds. A worker claims a small batch using `FOR UPDATE SKIP LOCKED`, marks RUNNING, assigns a random lease token, increments the lifetime attempt count, and commits a 120-second lease by default.

Each processor starts a fresh session, locks its job, checks state/token/lease against database time, performs the work, and commits SUCCEEDED with any business changes. A stale delivery cannot act after another claimant acquired a new token. A duplicate delivery sees the existing lock/state. Expired RUNNING claims become discoverable again. At the retry limit abandoned claims become FAILED.

Default batches contain five jobs in Celery (20 for the manual CLI pass); SMTP timeout is ten seconds. A Celery task has 80/90-second soft/hard limits. A killed task leaves recoverable leases. Database transitions and SMTP are deliberately separate durable jobs so a mail outage does not roll back a committed booking.

| Failure | Recovery |
| --- | --- |
| App stops after commit, before publication | Job was already written in PostgreSQL; next discovery finds it |
| Redis is down | Web commits pending jobs; Beat and workers reconnect; a later scan catches up |
| Worker stops before/during work | Database rolls back open work; the committed claim expires and is reclaimed |
| Discovery delivered twice | Claim locks, lease tokens, job state, and business uniqueness prevent duplicate business effects |
| Business or SMTP processing fails | Job returns to PENDING with increasing delay, then FAILED at its budget |
| Offer email is delayed past resolution/deadline | Delivery is recorded SKIPPED; stored expiration is never extended |

Retries use delays of 5, 10, 20, 40… seconds, capped at 300 seconds, with five attempts by default. The staff Jobs page shows state, attempt count, next due time, deduplication identity, and the last error. Staff can grant another five-attempt budget to a FAILED job; lifetime attempt numbers and notification history remain intact. Failed allocation/expiration jobs appear alongside failed email jobs, so operational failures are visible.

**Email is at least once under ambiguous failures.** If SMTP accepts a message and the connection or worker fails before PostgreSQL records success, a retry may send it again. A stable Message-ID helps correlate these deliveries but is not a promise that an SMTP server deduplicates them. No exactly-once email guarantee is claimed. NotificationAttempt records committed SENT, FAILED, or SKIPPED outcomes; a crash before recording an outcome can leave an unrecorded SMTP delivery.

## Commands

```sh
docker compose ps
docker compose logs --tail=100 web worker beat
docker compose exec web alembic current
docker compose exec web python -m app.cli drain-jobs
docker compose exec web python -m app.cli create-staff --email owner@example.com --name "Owner"
```

The manual drain command uses the same claims and processors, useful for recovery even without Redis. Repeat it to process newly generated jobs. To demonstrate pending work surviving a worker stop:

```sh
docker compose stop worker
# Cancel a future booking in the browser. Its slot stays ALLOCATING and a job stays pending.
docker compose start worker
# Within the next discovery pass the earliest eligible customer receives the offer.
```

To demonstrate Redis recovery, stop Redis, perform the cancellation, then start Redis; workers and Beat reconnect automatically. Do not run the development seed with a test database or production configuration.

Health: `/health/live` proves the process can serve; `/health/ready` checks database access and the migration-version table. Compose has database, Redis, Mailpit, web, worker, and Beat health checks. Beat's schedule file is synced at least every five minutes; its health check allows six minutes. Use `docker compose up -d --build --wait` after source changes. Uvicorn and Celery receive graceful termination through Compose's init process and a 30-second shutdown grace period. Interrupted jobs can be reclaimed afterward.

## Configuration

| Variable | Default / purpose |
| --- | --- |
| `APP_ENV` | `development`; `production` enforces HTTPS, secure cookies, and a non-development secret |
| `DATABASE_URL` | Psycopg SQLAlchemy PostgreSQL URL; Compose sets its internal hostname |
| `DATABASE_PASSWORD` | Compose PostgreSQL password; default is for development only |
| `REDIS_URL` | Celery broker; Compose sets `redis:6379` |
| `BUSINESS_TIMEZONE` | `Asia/Karachi`, validated through zoneinfo |
| `OFFER_LIFETIME_SECONDS` | 900; demo may use 30 |
| `BASE_URL` | Public origin used in mail links and CSRF Origin checks |
| `COOKIE_SECURE` | false locally; true with HTTPS |
| `SECRET_KEY` | Random 32+ characters for production CSRF signing |
| `SESSION_HOURS` | 168 |
| `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM` | Local Mailpit delivery; no external integration is configured |
| `JOB_LEASE_SECONDS`, `JOB_MAX_ATTEMPTS` | 120 / 5 for native runs; Compose defaults use these values |
| `TEST_DATABASE_URL` | Dedicated database ending `_test`, never the application database |

Compose overrides database, broker, and SMTP hosts to container names. Native runs load `.env`. Changing a variable requires restarting affected processes. PostgreSQL connections set the session timezone to UTC; interfaces display business-local times. Database migrations need permission to create `btree_gist`. A restricted application database role can be used after migrations; this development stack uses one database owner for simplicity.

Application events use JSON logs with request IDs or `job:<id>` correlations. HTTP responses include `X-Request-ID`; audit events preserve the corresponding identifier. Process infrastructure may also emit Uvicorn/Celery lifecycle logs. Tokens, passwords, and full form bodies are not logged.

For a real deployment, set up backups, tested restores, TLS termination, host/origin rules, trusted proxy IP handling, SMTP authentication/TLS as required, and monitoring. The included SMTP transport intentionally targets local Mailpit. No production deployment or external account has been configured.
