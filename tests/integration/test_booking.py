from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.db.session import db_now
from app.models import (
    AuditEvent,
    Booking,
    DurableJob,
    IdempotencyRecord,
    Offer,
    Slot,
    WaitlistEntry,
)
from app.schemas.requests import ServiceUpdate, SlotCreate, SlotUpdate
from app.services import booking, catalog, waitlist

pytestmark = pytest.mark.integration


def test_overlap_and_duration_guards(db, domain):
    with pytest.raises(DomainError):
        catalog.create_slot(
            db,
            domain["staff"],
            SlotCreate(
                service_id=domain["service"].id,
                staff_id=domain["staff"].id,
                starts_at=domain["start"] + timedelta(minutes=10),
            ),
        )
    with pytest.raises(DomainError, match="Duration"):
        catalog.update_service(
            db, domain["staff"], domain["service"].id, ServiceUpdate(duration_minutes=60)
        )


def test_booking_idempotency_and_atomic_outbox(db, domain):
    original = booking.book(db, domain["a"], domain["slot_id"], "retry-key")
    assert booking.book(db, domain["a"], domain["slot_id"], "retry-key") == original
    with pytest.raises(DomainError, match="different input"):
        booking.book(db, domain["a"], 999, "retry-key")
    assert db.scalar(select(func.count()).select_from(Booking)) == 1
    assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
    assert db.scalar(
        select(DurableJob.id).where(DurableJob.dedupe_key == f"email:confirmation:{original['id']}")
    )
    assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == "booking.confirmed"))
    with pytest.raises(DomainError):
        catalog.update_slot(
            db,
            domain["staff"],
            domain["slot_id"],
            SlotUpdate(
                service_id=domain["service"].id,
                staff_id=domain["staff"].id,
                starts_at=domain["start"] + timedelta(days=1),
            ),
        )


def test_resource_authorization_and_cancellation_retry(db, domain):
    result = booking.book(db, domain["a"], domain["slot_id"])
    with pytest.raises(DomainError) as exc:
        booking.cancel(db, domain["b"], result["id"])
    assert exc.value.status == 404
    canceled = booking.cancel(db, domain["a"], result["id"])
    assert booking.cancel(db, domain["a"], result["id"]) == canceled
    assert canceled["canceled_by_id"] == domain["a"].id
    assert db.get(Slot, domain["slot_id"]).state == "ALLOCATING"


def test_full_recovery_flow(db, domain, entry):
    direct = booking.book(db, domain["a"], domain["slot_id"])
    first, second = entry("b"), entry("c")
    booking.cancel(db, domain["a"], direct["id"])
    with pytest.raises(DomainError):
        booking.book(db, domain["c"], domain["slot_id"])
    offer_b = waitlist.allocate(db, domain["slot_id"])
    assert offer_b["entry_id"] == first["id"]
    with pytest.raises(DomainError) as exc:
        waitlist.accept(db, domain["c"], offer_b["id"])
    assert exc.value.status == 404
    waitlist.decline(db, domain["b"], offer_b["id"])
    offer_c = waitlist.allocate(db, domain["slot_id"])
    assert offer_c["entry_id"] == second["id"]
    accepted = waitlist.accept(db, domain["c"], offer_c["id"], "accept-retry")
    assert accepted["source"] == "WAITLIST"
    assert waitlist.accept(db, domain["c"], offer_c["id"], "accept-retry") == accepted
    assert db.get(WaitlistEntry, second["id"]).state == "FULFILLED"
    assert waitlist.allocate(db, domain["slot_id"]) is None
    assert (
        db.scalar(select(func.count()).select_from(Booking).where(Booking.state == "CONFIRMED"))
        == 1
    )


def test_matching_full_window_staff_fifo_and_no_repeat(db, domain, entry):
    entry("a", latest_end=domain["start"] + timedelta(minutes=29))
    entry("b", preferred_staff_id=domain["otherstaff"].id)
    eligible = entry("c")
    offered = waitlist.allocate(db, domain["slot_id"])
    assert offered["entry_id"] == eligible["id"]
    waitlist.decline(db, domain["c"], offered["id"])
    assert waitlist.allocate(db, domain["slot_id"]) is None
    assert db.get(Slot, domain["slot_id"]).state == "AVAILABLE"


def test_identical_active_entry_rejected_and_withdrawal_revokes(db, domain, entry):
    joined = entry()
    with pytest.raises(DomainError):
        entry()
    offered = waitlist.allocate(db, domain["slot_id"])
    waitlist.withdraw(db, domain["b"], joined["id"])
    assert db.get(Offer, offered["id"]).state == "REVOKED"
    assert db.get(Slot, domain["slot_id"]).state == "ALLOCATING"


def test_expired_offer_rejected_before_worker(db, domain, entry):
    entry()
    offered = waitlist.allocate(db, domain["slot_id"])
    offer = db.get(Offer, offered["id"])
    offer.created_at = db_now(db) - timedelta(minutes=20)
    offer.expires_at = db_now(db) - timedelta(seconds=1)
    db.commit()
    with pytest.raises(DomainError):
        waitlist.accept(db, domain["b"], offer.id)
    assert db.get(Offer, offer.id).state == "ACTIVE"
    waitlist.expire(db, offer.id)
    assert db.get(Offer, offer.id).state == "EXPIRED"


def test_database_cross_table_occupancy_guard(db, domain, entry):
    entry()
    offered = waitlist.allocate(db, domain["slot_id"])
    db.add(
        Booking(
            slot_id=domain["slot_id"],
            customer_id=domain["a"].id,
            state="CONFIRMED",
            source="DIRECT",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.get(Offer, offered["id"]).state == "ACTIVE"


def test_started_slot_cannot_book_or_receive_offer(db, domain):
    slot = db.get(Slot, domain["slot_id"])
    slot.starts_at -= timedelta(days=3)
    slot.ends_at -= timedelta(days=3)
    db.commit()
    with pytest.raises(DomainError):
        booking.book(db, domain["a"], slot.id)
    assert waitlist.allocate(db, slot.id) is None
    assert db.get(Slot, slot.id).state == "CLOSED"
