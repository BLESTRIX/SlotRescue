from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.session import db_now
from app.models import DurableJob, NotificationAttempt, Offer
from app.services import booking, jobs, waitlist

pytestmark = pytest.mark.integration


def allocation_claim(db, slot_id):
    claims = jobs.claim(db)
    return next(
        (job_id, token)
        for job_id, token in claims
        if db.get(DurableJob, job_id).kind == "allocate"
        and db.get(DurableJob, job_id).payload["slot_id"] == slot_id
    )


def test_pending_allocation_survives_restart_and_duplicate_delivery(db, domain, entry):
    booked = booking.book(db, domain["a"], domain["slot_id"])
    entry()
    booking.cancel(db, domain["a"], booked["id"])
    db.close()  # New process/session can discover the committed work, without Redis.
    job_id, token = allocation_claim(db, domain["slot_id"])
    assert jobs.process(job_id, token)
    assert not jobs.process(job_id, token)
    assert db.scalar(select(func.count()).select_from(Offer)) == 1


def test_abandoned_claim_is_reclaimed_and_fenced(db, domain, entry):
    entry()
    job_id, token = allocation_claim(db, domain["slot_id"])
    job = db.get(DurableJob, job_id)
    job.lease_until = db_now(db) - timedelta(seconds=1)
    db.commit()
    next_claims = jobs.claim(db)
    new_token = next(value for ident, value in next_claims if ident == job_id)
    assert new_token != token
    assert not jobs.process(job_id, token)
    assert jobs.process(job_id, new_token)
    assert db.scalar(select(func.count()).select_from(Offer)) == 1


def test_notification_retries_are_bounded_visible_and_retryable(db, domain, monkeypatch):
    booked = booking.book(db, domain["a"], domain["slot_id"])
    job = db.scalar(
        select(DurableJob).where(DurableJob.dedupe_key == f"email:confirmation:{booked['id']}")
    )
    job.retry_limit = 2
    db.commit()

    def fail(*args, **kwargs):
        raise OSError("SMTP temporarily unavailable")

    monkeypatch.setattr("app.services.jobs.smtplib.SMTP", fail)
    for _attempt in range(2):
        db.refresh(job)
        job.available_at = db_now(db) - timedelta(seconds=1)
        db.commit()
        job_id, token = next(pair for pair in jobs.claim(db) if pair[0] == job.id)
        assert not jobs.process(job_id, token)
    db.refresh(job)
    assert job.state == "FAILED" and job.attempts == 2
    assert (
        db.scalar(
            select(func.count())
            .select_from(NotificationAttempt)
            .where(NotificationAttempt.job_id == job.id)
        )
        == 2
    )
    retried = jobs.retry(db, domain["staff"], job.id)
    assert retried["state"] == "PENDING" and retried["retry_limit"] == 7
    assert retried["attempts"] == 2


def test_stale_offer_email_is_skipped(db, domain, entry):
    entry()
    offered = waitlist.allocate(db, domain["slot_id"])
    waitlist.decline(db, domain["b"], offered["id"])
    claims = jobs.claim(db)
    job_id, token = next(
        (jid, tok)
        for jid, tok in claims
        if db.get(DurableJob, jid).dedupe_key == f"email:offer:{offered['id']}"
    )
    assert jobs.process(job_id, token)
    assert (
        db.scalar(select(NotificationAttempt.state).where(NotificationAttempt.job_id == job_id))
        == "SKIPPED"
    )
