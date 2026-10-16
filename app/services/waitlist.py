from datetime import timedelta

from sqlalchemy import exists, or_, select

from app.core.config import settings
from app.core.errors import DomainError, missing
from app.core.states import transition
from app.core.transactions import transactional
from app.db.session import db_now
from app.models import Offer, Service, Slot, User, WaitlistEntry
from app.repositories.queries import customer_only, data
from app.services.booking import check_future_active, lock_slot, make_booking
from app.services.common import (
    allocation_job,
    audit,
    email_job,
    enqueue,
    idempotency_begin,
    idempotency_finish,
)

SELECTION_REASON = (
    "Earliest eligible waitlist entry matching service, time window, and staff preference."
)


@transactional
def join(db, user, payload):
    customer_only(user)
    service = db.scalar(
        select(Service).where(Service.id == payload.service_id).with_for_update(read=True)
    )
    if not service or not service.active:
        raise DomainError("inactive_service", "Choose an active service.", 422)
    if payload.latest_end <= db_now(db) or payload.latest_end - payload.earliest_start < timedelta(
        minutes=service.duration_minutes
    ):
        raise DomainError(
            "invalid_window", "Your window must fit a future appointment's full duration.", 422
        )
    if payload.preferred_staff_id:
        member = db.get(User, payload.preferred_staff_id)
        if not member or member.role != "STAFF":
            raise DomainError("invalid_staff", "Choose a staff member.", 422)
    entry = WaitlistEntry(customer_id=user.id, **payload.model_dump(), state="ACTIVE")
    db.add(entry)
    db.flush()
    audit(db, user, "waitlist.joined", entry)
    # A new entry can also fill an already available slot. Workers lock before rechecking.
    for slot_id in db.scalars(
        select(Slot.id).where(
            Slot.service_id == entry.service_id,
            Slot.state.in_(["AVAILABLE", "ALLOCATING"]),
            Slot.starts_at >= entry.earliest_start,
            Slot.ends_at <= entry.latest_end,
            Slot.starts_at > db_now(db),
        )
    ):
        enqueue(db, "allocate", f"allocate:entry:{entry.id}:slot:{slot_id}", {"slot_id": slot_id})
    return data(entry)


def candidate_query(slot, excluded=()):
    return (
        select(WaitlistEntry)
        .where(
            WaitlistEntry.state == "ACTIVE",
            WaitlistEntry.service_id == slot.service_id,
            WaitlistEntry.earliest_start <= slot.starts_at,
            WaitlistEntry.latest_end >= slot.ends_at,
            or_(
                WaitlistEntry.preferred_staff_id.is_(None),
                WaitlistEntry.preferred_staff_id == slot.staff_id,
            ),
            ~exists(
                select(Offer.id).where(Offer.entry_id == WaitlistEntry.id, Offer.state == "ACTIVE")
            ),
            ~exists(
                select(Offer.id).where(Offer.entry_id == WaitlistEntry.id, Offer.slot_id == slot.id)
            ),
            WaitlistEntry.id.not_in(excluded),
        )
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        .limit(1)
        .with_for_update()
    )


def allocate_locked(db, slot_id):
    slot = lock_slot(db, slot_id)
    if slot.state not in {"AVAILABLE", "ALLOCATING"}:
        return None
    if slot.starts_at <= db_now(db):
        transition(slot, "CLOSED")
        return None
    if slot.state == "AVAILABLE":
        transition(slot, "ALLOCATING")
    if not db.get(Service, slot.service_id).active:
        transition(slot, "AVAILABLE")
        return None
    excluded = []
    while True:
        entry = db.scalar(candidate_query(slot, excluded))
        if not entry:
            transition(slot, "AVAILABLE")
            return None
        # A different slot's allocator may have offered this entry while we waited for its lock.
        db.refresh(entry)
        if entry.state != "ACTIVE" or db.scalar(
            select(Offer.id).where(Offer.entry_id == entry.id, Offer.state == "ACTIVE").limit(1)
        ):
            excluded.append(entry.id)
            continue
        now = db_now(db)
        if slot.starts_at <= now:
            transition(slot, "CLOSED")
            return None
        offer = Offer(
            slot_id=slot.id,
            entry_id=entry.id,
            customer_id=entry.customer_id,
            state="ACTIVE",
            expires_at=min(
                now + timedelta(seconds=settings().offer_lifetime_seconds), slot.starts_at
            ),
            explanation=SELECTION_REASON,
        )
        db.add(offer)
        transition(slot, "HELD")
        db.flush()
        enqueue(db, "expire", f"expire:{offer.id}", {"offer_id": offer.id}, offer.expires_at)
        email_job(db, "offer", offer.id, offer.customer_id, slot.id, offer.id)
        audit(db, None, "offer.created", offer, {"selection": SELECTION_REASON})
        return data(offer)


@transactional
def allocate(db, slot_id):
    return allocate_locked(db, slot_id)


def lock_offer_context(db, offer_id, user=None):
    initial = db.get(Offer, offer_id)
    if not initial or (user and initial.customer_id != user.id):
        raise missing()
    slot = lock_slot(db, initial.slot_id)
    entry = db.scalar(
        select(WaitlistEntry).where(WaitlistEntry.id == initial.entry_id).with_for_update()
    )
    db.refresh(initial, with_for_update=True)
    return slot, entry, initial


def release_offer(db, slot, offer, state, actor=None):
    transition(offer, state)
    offer.responded_at = db_now(db)
    transition(slot, "ALLOCATING" if slot.starts_at > db_now(db) else "CLOSED")
    db.flush()
    if slot.state == "ALLOCATING":
        allocation_job(db, slot, f"offer:{offer.id}:{state}")
    audit(db, actor, f"offer.{state.lower()}", offer)


@transactional
def accept(db, user, offer_id, key=None):
    customer_only(user)
    existing, fingerprint = idempotency_begin(db, user, "accept", key, {"offer_id": offer_id})
    if existing:
        return existing.response
    slot, entry, offer = lock_offer_context(db, offer_id, user)
    if offer.state == "ACCEPTED":
        from app.models import Booking

        response = data(db.get(Booking, offer.booking_id))
        return idempotency_finish(db, user, "accept", key, fingerprint, response)
    if offer.state != "ACTIVE" or offer.expires_at <= db_now(db):
        # No mutation on rejection: scheduled durable expiration remains responsible for release.
        raise DomainError(
            "offer_expired_or_final", "This offer has expired or was already resolved."
        )
    check_future_active(db, slot)
    if slot.state != "HELD" or entry.state != "ACTIVE":
        raise DomainError("offer_unavailable", "This offer is no longer available.")
    transition(offer, "ACCEPTED")
    offer.responded_at = db_now(db)
    transition(entry, "FULFILLED")
    db.flush()  # Remove active offer before inserting the confirmed booking.
    booking = make_booking(db, user, slot, source="WAITLIST")
    offer.booking_id = booking.id
    audit(db, user, "offer.accepted", offer)
    return idempotency_finish(db, user, "accept", key, fingerprint, data(booking))


@transactional
def decline(db, user, offer_id):
    slot, entry, offer = lock_offer_context(db, offer_id, user)
    if offer.state == "DECLINED":
        return data(offer)
    if offer.state != "ACTIVE":
        raise DomainError("offer_final", "This offer was already resolved.")
    release_offer(
        db, slot, offer, "EXPIRED" if offer.expires_at <= db_now(db) else "DECLINED", user
    )
    return data(offer)


def expire_locked(db, offer_id):
    slot, entry, offer = lock_offer_context(db, offer_id)
    if offer.state == "ACTIVE" and offer.expires_at <= db_now(db):
        release_offer(db, slot, offer, "EXPIRED")
    return data(offer)


@transactional
def expire(db, offer_id):
    return expire_locked(db, offer_id)


@transactional
def withdraw(db, user, entry_id):
    for _attempt in range(3):
        entry = db.get(WaitlistEntry, entry_id)
        if not entry or entry.customer_id != user.id:
            raise missing()
        active = db.scalar(select(Offer).where(Offer.entry_id == entry_id, Offer.state == "ACTIVE"))
        if active:
            slot = lock_slot(db, active.slot_id)
        db.refresh(entry, with_for_update=True)
        if entry.state == "WITHDRAWN":
            return data(entry)
        if entry.state != "ACTIVE":
            raise DomainError("waitlist_final", "This waitlist entry has already been fulfilled.")
        # A racing allocator may have created an offer while we waited for the entry.
        # Retry from a fresh snapshot to acquire its slot before the entry lock.
        current = db.scalar(
            select(Offer).where(Offer.entry_id == entry_id, Offer.state == "ACTIVE")
        )
        if current and (not active or current.id != active.id):
            db.rollback()
            continue
        transition(entry, "WITHDRAWN")
        if current:
            db.refresh(current, with_for_update=True)
            if current.state == "ACTIVE":
                release_offer(db, slot, current, "REVOKED", user)
        audit(db, user, "waitlist.withdrawn", entry)
        return data(entry)
    raise DomainError("concurrent_change", "An offer changed while withdrawing. Please retry.")
