# A short demonstration

Start and seed the stack using the README. Open three separate browser profiles/private contexts for Customers A, B, and C, and another for staff. All account details are development-only in the README. Open Mailpit at <http://localhost:8025>.

1. A signs in, chooses **Find an appointment**, and books a future slot. Show the confirmation page and email.
2. B opens **Waitlist**, chooses the same service, and enters a window containing the entire appointment. C repeats after B. Keep preferred staff empty or match the appointment.
3. A opens the booking, expands **Cancel this appointment**, and confirms. Staff can show the canceled booking and the ALLOCATING/HELD slot.
4. After a discovery pass, B opens **Offers** or the Mailpit offer link. Show the exact appointment, expiration deadline, countdown, and FIFO selection explanation. C has no offer for this slot yet.
5. B declines. After the next pass C receives the next offer.
6. C accepts. The offer is ACCEPTED, its entry is FULFILLED, and the booking identifies its waitlist source.
7. Staff opens **Overview**, selects a range containing the appointment, and shows the recovered booking count. Audit history shows the transitions and Jobs shows notification outcomes.

For a repeatable automated version, run:

```sh
docker compose exec web python scripts/demo.py --base-url http://web:8000
```

The script isolates each run with a new demo service and uses actual HTTP, Redis, Beat, workers, and PostgreSQL. It waits in short polls rather than assuming a worker timing guarantee.

It keeps B's and C's offers active until their email jobs finish, so Mailpit retains inspectable offer messages before the demonstration resolves them.

To demonstrate expiration, set `OFFER_LIFETIME_SECONDS=30`, recreate web/worker/Beat, and leave B's offer unanswered. B cannot accept after the deadline even if the expiration worker is stopped. Start the worker again and C will receive the next offer. An expired offer may appear ACTIVE in historical listings until processing catches up; its details page disables acceptance based on database time and the API rejects it independently.

## Interview talking points

- **Why PostgreSQL locks and constraints?** A row lock serializes business decisions, partial unique indexes defend individual tables, and a deferred constraint trigger checks the combined booking/offer occupancy at commit. Redis availability cannot change the booking invariant.
- **Why an outbox instead of calling Celery immediately?** Inserting work in the business transaction eliminates the commit/publish crash window. Beat's discovery messages are replaceable because pending work lives in PostgreSQL.
- **Where does idempotency live?** Successful results and fingerprints are transactional, scoped by customer and operation, and protected against simultaneous same-key requests with a PostgreSQL transaction advisory lock.
- **How is fairness defined?** Earliest eligible `(created_at, id)`, full interval containment, optional staff preference, no active entry offer, and no repeat of the same entry/slot pair. It is a transparent FIFO rule, not a promise to optimize global utilization.
- **What happens at the deadline?** Acceptance and expiration lock the same slot. Acceptance reads database wall-clock time after locking, so waiting for a lock cannot extend eligibility.
- **What does at least once mean here?** Duplicate job delivery produces no duplicate offer/booking through transactional state and constraints. SMTP's ambiguous acceptance cannot be rolled back, so occasional duplicate email remains possible.
- **How was concurrency tested?** Twenty real connections race to book one slot; exactly one wins. Separate connections also race allocators, same-key requests, offer acceptance/expiration, and withdrawal/allocation. No SQLite substitution or invented throughput numbers.
- **What would change next?** Add identity recovery and monitoring, measure workload, move toward stronger operational isolation if justified, and preserve the database invariants throughout.
