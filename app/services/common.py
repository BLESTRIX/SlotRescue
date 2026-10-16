import hashlib
import json
from contextvars import ContextVar

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.errors import DomainError
from app.db.session import db_now
from app.models import AuditEvent, DurableJob, IdempotencyRecord

request_id = ContextVar("request_id", default=None)


def audit(db, actor, action, record, details=None):
    db.add(
        AuditEvent(
            actor_id=actor.id if actor else None,
            action=action,
            resource_type=type(record).__name__,
            resource_id=record.id,
            details=details or {},
            request_id=request_id.get(),
        )
    )


def enqueue(db, kind, key, payload, due=None):
    db.execute(
        insert(DurableJob)
        .values(
            kind=kind,
            dedupe_key=key,
            payload=payload,
            available_at=due or db_now(db),
            state="PENDING",
            attempts=0,
            retry_limit=settings().job_max_attempts,
        )
        .on_conflict_do_nothing(index_elements=[DurableJob.dedupe_key])
    )


def allocation_job(db, slot, event):
    enqueue(db, "allocate", f"allocate:{slot.id}:{event}", {"slot_id": slot.id})


def email_job(db, kind, record_id, customer_id, slot_id, offer_id=None):
    enqueue(
        db,
        "email",
        f"email:{kind}:{record_id}",
        {"type": kind, "customer_id": customer_id, "slot_id": slot_id, "offer_id": offer_id},
    )


def idempotency_begin(db, user, operation, key, payload):
    if key is None:
        return None, None
    if not key.strip() or len(key) > 128:
        raise DomainError(
            "invalid_idempotency_key", "Idempotency-Key must contain 1–128 characters.", 422
        )
    scope = f"{user.id}:{operation}:{key}"
    lock = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock)))
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user.id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == key,
        )
    )
    if record and record.fingerprint != fingerprint:
        raise DomainError(
            "idempotency_mismatch", "This idempotency key was already used with different input."
        )
    return record, fingerprint


def idempotency_finish(db, user, operation, key, fingerprint, response):
    if key:
        db.add(
            IdempotencyRecord(
                user_id=user.id,
                operation=operation,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
        )
    return response
