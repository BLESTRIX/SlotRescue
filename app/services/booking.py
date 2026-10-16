from sqlalchemy import select

from app.core.errors import DomainError, missing
from app.core.states import transition
from app.core.transactions import transactional
from app.db.session import db_now
from app.models import Booking, Service, Slot
from app.repositories.queries import customer_only, data, staff_only
from app.services.common import (
    allocation_job,
    audit,
    email_job,
    idempotency_begin,
    idempotency_finish,
)


def lock_slot(db, slot_id):
    slot = db.scalar(
        select(Slot)
        .where(Slot.id == slot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not slot:
        raise missing()
    return slot


def check_future_active(db, slot):
    if slot.starts_at <= db_now(db):
        raise DomainError("slot_started", "This appointment has already started.")
    if not db.get(Service, slot.service_id).active:
        raise DomainError("inactive_service", "This service is no longer accepting bookings.")


def make_booking(db, user, slot, source="DIRECT"):
    booking = Booking(slot_id=slot.id, customer_id=user.id, source=source, state="CONFIRMED")
    db.add(booking)
    transition(slot, "BOOKED")
    db.flush()
    audit(db, user, "booking.confirmed", booking, {"source": source})
    email_job(db, "confirmation", booking.id, user.id, slot.id)
    return booking


@transactional
def book(db, user, slot_id, key=None):
    customer_only(user)
    existing, fingerprint = idempotency_begin(db, user, "book", key, {"slot_id": slot_id})
    if existing:
        return existing.response
    slot = lock_slot(db, slot_id)
    check_future_active(db, slot)
    if slot.state != "AVAILABLE":
        raise DomainError(
            "slot_unavailable",
            "This slot became unavailable. Refresh availability or join the waitlist.",
        )
    result = data(make_booking(db, user, slot))
    return idempotency_finish(db, user, "book", key, fingerprint, result)


@transactional
def cancel(db, user, booking_id, staff=False):
    if staff:
        staff_only(user)
    booking = db.get(Booking, booking_id)
    if not booking or (not staff and booking.customer_id != user.id):
        raise missing()
    slot = lock_slot(db, booking.slot_id)
    db.refresh(booking, with_for_update=True)
    if booking.state == "CANCELED":
        return data(booking)
    if booking.state != "CONFIRMED":
        raise DomainError("booking_final", "This booking can no longer be canceled.")
    if slot.starts_at <= db_now(db) and not staff:
        raise DomainError("past_booking", "Customers cannot cancel appointments that have started.")
    transition(booking, "CANCELED")
    booking.canceled_at, booking.canceled_by_id = db_now(db), user.id
    transition(slot, "ALLOCATING" if slot.starts_at > db_now(db) else "CLOSED")
    db.flush()
    if slot.state == "ALLOCATING":
        allocation_job(db, slot, f"cancel:{booking.id}")
    email_job(db, "cancellation", booking.id, booking.customer_id, slot.id)
    audit(db, user, "booking.canceled", booking)
    return data(booking)
