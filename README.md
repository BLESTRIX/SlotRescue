# SlotRescue

A single-business appointment platform that fills canceled appointments through a fair, time-limited waitlist. Built as a Python 3.12 modular monolith with a separate Celery worker. Customer pages and the JSON API use the same transactional services.

## Run with Docker Compose

Requires Docker Engine/Desktop with Compose v2. From this directory:

```sh
cp .env.example .env
docker compose up -d --build --wait
docker compose exec web python -m app.cli seed
```

PowerShell: use `Copy-Item .env.example .env` for the first command. Web: <http://localhost:8000>; interactive API docs: <http://localhost:8000/docs>; email inbox: <http://localhost:8025>. Browser dependencies, including Swagger UI, are vendored locally. The demo needs no CDN.

Compose starts PostgreSQL **17**, Redis, Mailpit, a migration process, web, worker, and Beat. The migration process must finish before the application and workers start. PostgreSQL uses the `pgdata` named volume. `docker compose down` stops the stack and preserves data. Never add `-v` unless you intend to erase development data.

The seed command is idempotent and runs only with `APP_ENV=development`. It adds a clearly labeled demonstration business: three services, two staff, three customers, and future slots. It preserves existing accounts and passwords. Rerunning it on a later day adds that day's missing future demo slots.

| Role | Email | Initial development password |
| --- | --- | --- |
| Staff | `staff@slotrescue.test` | `DemoPassphrase!2026` |
| Staff | `omar@slotrescue.test` | `DemoPassphrase!2026` |
| Customer A | `a@slotrescue.test` | `DemoPassphrase!2026` |
| Customer B | `b@slotrescue.test` | `DemoPassphrase!2026` |
| Customer C | `c@slotrescue.test` | `DemoPassphrase!2026` |

These accounts are created only by the explicit development seed. Public registration always creates customers and rejects a `role` field.

Create a real initial staff account without seeding:

```sh
docker compose exec web python -m app.cli create-staff --email owner@example.com --name "Business owner"
```

The command prompts for a password of 12–128 characters. For automation supply `STAFF_PASSWORD` through the process environment/secret manager, never a password argument. Existing accounts are not promoted or overwritten.

Explicit migration command:

```sh
docker compose run --rm migrate
```

## Develop locally

Install Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/). All direct Python dependencies are pinned; `uv.lock` pins the complete dependency graph, with stable releases only.

```sh
cp .env.example .env
uv sync --frozen
docker compose up -d db redis mailpit
uv run alembic upgrade head
uv run python -m app.cli seed
uv run uvicorn app.main:app --reload --port 8000 --no-proxy-headers
```

In two other terminals:

```sh
uv run celery -A app.tasks.celery_app:celery worker --loglevel=INFO --concurrency=2
uv run celery -A app.tasks.celery_app:celery beat --loglevel=INFO
```

Use the Compose worker on Windows; Celery's prefork pool targets Linux. For debugging a single local worker on Windows, append `--pool=solo` and omit `--concurrency`. The web server and tests run natively on Windows.

## Tests and checks

Integration tests **erase all rows** in a dedicated PostgreSQL database whose name must end in `_test`. They use real commits and independent connections; SQLite is never used. Do not point them at application data. Keep the demo worker connected to the application database, not the test database.

```sh
docker compose up -d db
uv run python -m app.cli create-test-db
```

Then on macOS/Linux:

```sh
export TEST_DATABASE_URL=postgresql+psycopg://slotrescue:slotrescue@127.0.0.1:5432/slotrescue_test
export REQUIRE_POSTGRES_TESTS=1
uv run pytest --cov=app --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run djlint app/templates --check
```

PowerShell:

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://slotrescue:slotrescue@127.0.0.1:5432/slotrescue_test'
$env:REQUIRE_POSTGRES_TESTS='1'
uv run pytest --cov=app --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
```

`uv run pytest tests/unit` runs without external services. Without `TEST_DATABASE_URL`, database tests explicitly skip; `REQUIRE_POSTGRES_TESTS=1` turns missing configuration into a failure. CI always sets both. Tests require PostgreSQL major version 17 and migrate the test database through Alembic.

Important acceptance tests cover 20 concurrent bookings with one winner, concurrent use of the same idempotency key, duplicate allocation, two slots competing for one waitlist entry, acceptance versus expiration, allocation versus withdrawal, expired acceptance before the worker runs, CSRF and ownership, overlap rejection, durable recovery, and bounded email retries.

Optional browser smoke check against the seeded running stack:

```sh
uv run python scripts/browser_smoke.py
```

It uses installed Chrome, performs real HTMX login/filter/booking/cancellation, checks staff and local API docs, and writes desktop/mobile screenshots under ignored `.runtime/`. If Chrome is unavailable: `uv run playwright install chromium`, then set `BROWSER_CHANNEL=chromium`.

## Demonstrate cancellation recovery

With Compose running and seeded:

```sh
docker compose exec web python scripts/demo.py --base-url http://web:8000
```

Or `uv run python scripts/demo.py` from the host. The script calls the real HTTP API with four independent cookie sessions and waits for the real Beat/worker processes. It creates an isolated demo service and slot, then shows:

1. A books the slot.
2. B and C join eligible waitlists in order.
3. A cancels; direct booking is blocked while allocation is pending.
4. B receives the first offer.
5. B declines.
6. C receives and accepts the next offer.
7. The staff metric increases by exactly one recovered confirmed booking.

See [the walkthrough](docs/demo.md) for a manual browser demonstration and expiration variant. To shorten offers, set `OFFER_LIFETIME_SECONDS=30` in `.env`, then run `docker compose up -d --force-recreate web worker beat`. Existing offer deadlines retain their stored values. Automated tests move database deadlines directly instead of sleeping.

## Design and correctness

- [Architecture, diagrams, states, database relationships, and locking](docs/architecture.md)
- [Retries, recovery, notification semantics, and operations](docs/operations.md)
- [Demo and interview talking points](docs/demo.md)
- [Observed verification results](docs/verification.md)

Handlers validate transport input. Services authorize mutations and own transaction boundaries. Repositories contain reusable queries. Each request/task has its own synchronous SQLAlchemy session. Templates contain presentation logic; the server never calls its HTTP API to render pages.

PostgreSQL row locks serialize slot changes. Partial unique indexes prevent duplicate occupancy; deferred constraint triggers also prevent a slot from having both an active offer and a confirmed booking. A GiST exclusion constraint prevents overlapping staff intervals. Allocation and notification work is written transactionally into PostgreSQL. Redis carries discovery tasks and is never the booking lock authority.

All API collections paginate with `page` (1+) and `page_size` (1–100). IDs, status filters, dates, timezone-aware timestamps, and request bodies are validated. API errors use `{"error":{"code":"...","message":"...","request_id":"..."}}`; validation errors also identify fields. `X-Request-ID` connects the response to structured logs and audit events. See `/openapi.json` for response schemas and the full endpoint list.

For cookie-authenticated API clients, fetch `GET /api/v1/auth/csrf`, preserve cookies, and send its `csrf_token` as `X-CSRF-Token` on all POST/PATCH requests, including registration/login. Login rotates both cookies: use the new `csrf` cookie afterward. The local Swagger UI adds this header automatically. `Idempotency-Key` is supported for direct booking and offer acceptance and is scoped by user and operation.

## Assumptions and limits

One business; no payments, SMS, external calendars, recurring schedules, AI, or microservices. Timezone defaults to `Asia/Karachi`; the API requires explicit UTC offsets, and web forms convert business-local times to UTC. Ambiguous/nonexistent DST wall times are rejected by web forms. Adjacent staff slots are allowed; overlapping slots, including historical/closed slots, are rejected. A slot with any booking/offer history retains its interval permanently; create a new slot to change it.

Login limits: 10 attempts per normalized email/IP pair and 100 per IP in fixed 15-minute database-time windows, counting successes. Argon2id hashes passwords; random opaque session tokens are hashed server-side and expire after seven days by default. Logout invalidates the server session. SameSite=Lax and HttpOnly session cookies are set; HTTPS mode uses Secure cookies. CSRF tokens are signed, compared to the submitted token, and bound to authenticated sessions. Supplied Origin headers must match `BASE_URL`. Forwarded IP headers are deliberately not trusted by default.

This is a tested reference application, **not a claim of production readiness**. No throughput benchmarks are claimed. There is no email verification, password reset, MFA, automatic completion of past appointments, archival/retention policy, or external alerting. A past confirmed booking remains historical CONFIRMED; dashboard “upcoming” excludes it. SMTP delivery can duplicate after an ambiguous failure. The small job batches favor understandable recovery over high throughput. Staff can inspect and retry terminal jobs, but an operator must respond to persistent failures. Sensible next steps are verified identity recovery, retention tooling, monitoring, deployment-specific TLS/proxy configuration, backups with restore drills, and workload measurements before tuning.
