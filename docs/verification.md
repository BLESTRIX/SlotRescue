# Verification record

Verified locally on 16 October 2026. This records observed checks, not performance benchmarks or a production-readiness certification.

- PostgreSQL 17 integration tests use separate connections and real committed transactions. The suite includes the twenty-request booking race, simultaneous same-key requests, competing allocators, acceptance/expiration, and withdrawal/allocation.
- The final pytest run passed **38 tests** with **85% application statement coverage**, including session-bound CSRF recovery after a lost cookie.
- Docker Compose builds the Python 3.12 application and starts PostgreSQL, Redis, Mailpit, web, worker, and Beat with healthy service checks. The migration service exits successfully before dependents start.
- The seven-step live HTTP demo ran through actual Beat, Redis, worker, and Mailpit services. Customer A canceled, B declined the first offer, C accepted the next offer, and the recovered-booking count increased from 0 to 1.
- Chrome exercised real HTMX sign-in, blank-filter submission, booking, cancellation, sign-out, the staff dashboard, and vendored Swagger UI. Mobile home and slot pages fit a 390-pixel viewport without horizontal overflow. No remote asset requests or JavaScript exceptions were observed.
- Alembic reports no schema drift. A complete downgrade to base and upgrade to head succeeded on the disposable test database.
- Ruff lint/format checks, JavaScript syntax checks, local template compilation, and frozen lockfile consistency checks pass.

The GitHub Actions workflow is supplied but has not been run on a remote GitHub repository during this implementation. Starlette currently emits a deprecation warning for its HTTPX-based TestClient; the pinned versions pass the suite.

Reproduce the checks using the exact commands in the README. Browser screenshots are generated under ignored `.runtime/`, and the test suite prints its current test count and coverage report.
