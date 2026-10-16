import logging
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.errors import DomainError, missing
from app.core.logging import configure_logging
from app.core.transactions import transactional
from app.db.session import db_now, session_factory
from app.models import DurableJob, NotificationAttempt, Offer, Service, Slot, User
from app.repositories.queries import data, staff_only
from app.services.common import audit, request_id
from app.services.waitlist import allocate_locked, expire_locked

logger = logging.getLogger("slotrescue.jobs")
configure_logging()


@transactional
def claim(db, limit=20):
    now = db_now(db)
    jobs = db.scalars(
        select(DurableJob)
        .where(
            or_(
                (DurableJob.state == "PENDING") & (DurableJob.available_at <= now),
                (DurableJob.state == "RUNNING") & (DurableJob.lease_until <= now),
            )
        )
        .order_by(DurableJob.available_at, DurableJob.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claimed = []
    for job in jobs:
        if job.attempts >= job.retry_limit:
            job.state, job.last_error = "FAILED", "Retry limit reached after an abandoned claim."
            job.lease_until, job.lease_token = None, None
            continue
        job.state, job.lease_token = "RUNNING", secrets.token_hex(16)
        job.lease_until = now + timedelta(seconds=settings().job_lease_seconds)
        job.attempts += 1
        claimed.append((job.id, job.lease_token))
    return claimed


def deliver_email(db, job):
    payload = job.payload
    user, slot = db.get(User, payload["customer_id"]), db.get(Slot, payload["slot_id"])
    service = db.get(Service, slot.service_id)
    kind = payload["type"]
    offer = db.get(Offer, payload["offer_id"]) if payload.get("offer_id") else None
    if offer and (offer.state != "ACTIVE" or offer.expires_at <= db_now(db)):
        db.add(NotificationAttempt(job_id=job.id, attempt=job.attempts, state="SKIPPED"))
        return
    message = EmailMessage()
    message["From"], message["To"] = settings().mail_from, user.email
    message["Subject"] = {
        "confirmation": "Your appointment is confirmed",
        "cancellation": "Your appointment was canceled",
        "offer": "An appointment is available for you",
    }[kind]
    message["Message-ID"] = f"<slotrescue-job-{job.id}@slotrescue.test>"
    local = slot.starts_at.astimezone(ZoneInfo(settings().business_timezone))
    body = f"Hello {user.name},\n\n{service.name}: {local:%A, %d %B %Y at %H:%M %Z}.\n"
    if offer:
        expiration = offer.expires_at.astimezone(ZoneInfo(settings().business_timezone))
        body += f"Offer expires {expiration:%d %B %Y at %H:%M:%S %Z}. Email delays do not extend this deadline.\n{settings().base_url}/offers/{offer.id}\n"
    else:
        body += f"View your appointments: {settings().base_url}/dashboard\n"
    message.set_content(body + "\nSlotRescue\n")
    with smtplib.SMTP(settings().smtp_host, settings().smtp_port, timeout=10) as smtp:
        smtp.send_message(message)
    db.add(NotificationAttempt(job_id=job.id, attempt=job.attempts, state="SENT"))


def process(job_id, token):
    # Locking and fencing the claim, work, and success in one transaction prevents
    # an expired claimant from committing business work after a new claimant wins.
    context_token = request_id.set(f"job:{job_id}")
    with session_factory()() as db:
        try:
            job = db.scalar(select(DurableJob).where(DurableJob.id == job_id).with_for_update())
            if (
                not job
                or job.state != "RUNNING"
                or job.lease_token != token
                or job.lease_until <= db_now(db)
            ):
                return False
            if job.kind == "allocate":
                allocate_locked(db, job.payload["slot_id"])
            elif job.kind == "expire":
                expire_locked(db, job.payload["offer_id"])
            elif job.kind == "email":
                deliver_email(db, job)
            else:
                raise ValueError(f"Unknown durable job kind: {job.kind}")
            job.state, job.finished_at = "SUCCEEDED", db_now(db)
            job.lease_token, job.lease_until = None, None
            db.commit()
            logger.info("job.completed", extra={"job_id": job_id})
            return True
        except Exception as exc:
            db.rollback()
            job = db.scalar(select(DurableJob).where(DurableJob.id == job_id).with_for_update())
            if job and job.state == "RUNNING" and job.lease_token == token:
                job.state = "FAILED" if job.attempts >= job.retry_limit else "PENDING"
                job.available_at = db_now(db) + timedelta(
                    seconds=min(300, 5 * 2 ** (job.attempts - 1))
                )
                job.last_error = f"{type(exc).__name__}: {str(exc)[:400]}"
                job.lease_until, job.lease_token = None, None
                if job.kind == "email":
                    db.add(
                        NotificationAttempt(
                            job_id=job.id,
                            attempt=job.attempts,
                            state="FAILED",
                            error=job.last_error,
                        )
                    )
                db.commit()
            logger.exception("job.failed", extra={"job_id": job_id})
            return False
        finally:
            request_id.reset(context_token)


def drain(limit=20):
    with session_factory()() as db:
        claims = claim(db, limit)
    for job_id, token in claims:
        process(job_id, token)
    return len(claims)


@transactional
def retry(db, user, job_id):
    staff_only(user)
    job = db.scalar(select(DurableJob).where(DurableJob.id == job_id).with_for_update())
    if not job:
        raise missing()
    if job.state != "FAILED":
        raise DomainError("job_not_failed", "Only failed jobs can be retried.")
    # Attempt numbers remain monotonic so notification history stays unique.
    job.state, job.available_at = "PENDING", db_now(db)
    job.last_error, job.lease_token, job.lease_until = None, None, None
    job.retry_limit = job.attempts + settings().job_max_attempts
    audit(db, user, "job.retry_requested", job)
    return data(job)
