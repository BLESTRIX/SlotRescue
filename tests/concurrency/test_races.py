from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.db.session import db_now
from app.models import Booking, Offer, Slot, User
from app.schemas.requests import SlotCreate
from app.services import booking, catalog, waitlist

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


def parallel(count, function):
    barrier = Barrier(count)

    def run(index):
        barrier.wait(timeout=15)
        return function(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def test_twenty_booking_attempts_exactly_one_winner(pg, db, domain):
    customer_id, slot_id = domain["a"].id, domain["slot_id"]

    def attempt(index):
        with pg() as session:
            user = session.get(User, customer_id)
            try:
                return booking.book(session, user, slot_id, f"attempt-{index}")["id"]
            except DomainError as exc:
                assert exc.status == 409
                return None

    results = parallel(20, attempt)
    assert sum(result is not None for result in results) == 1
    assert (
        db.scalar(select(func.count()).select_from(Booking).where(Booking.state == "CONFIRMED"))
        == 1
    )


def test_concurrent_same_key_is_stable(pg, db, domain):
    customer_id, slot_id = domain["a"].id, domain["slot_id"]

    def attempt(index):
        with pg() as session:
            return booking.book(session, session.get(User, customer_id), slot_id, "shared-key")

    results = parallel(10, attempt)
    assert all(result == results[0] for result in results)
    assert db.scalar(select(func.count()).select_from(Booking)) == 1


def test_concurrent_allocators_create_one_offer(pg, db, domain, entry):
    entry()

    def attempt(index):
        with pg() as session:
            return waitlist.allocate(session, domain["slot_id"])

    results = parallel(10, attempt)
    assert sum(result is not None for result in results) == 1
    assert db.scalar(select(func.count()).select_from(Offer).where(Offer.state == "ACTIVE")) == 1


def test_two_slots_cannot_offer_one_entry_twice(pg, db, domain, entry):
    entry(latest_end=domain["start"] + timedelta(hours=4))
    second = catalog.create_slot(
        db,
        domain["staff"],
        SlotCreate(
            service_id=domain["service"].id,
            staff_id=domain["staff"].id,
            starts_at=domain["start"] + timedelta(hours=2),
        ),
    )
    slot_ids = [domain["slot_id"], second["id"]]

    def attempt(index):
        with pg() as session:
            return waitlist.allocate(session, slot_ids[index])

    results = parallel(2, attempt)
    assert sum(result is not None for result in results) == 1
    assert db.scalar(select(func.count()).select_from(Offer).where(Offer.state == "ACTIVE")) == 1


@pytest.mark.parametrize("seconds", [-1, 60])
def test_acceptance_races_expiration(pg, db, domain, entry, seconds):
    entry()
    offered = waitlist.allocate(db, domain["slot_id"])
    offer = db.get(Offer, offered["id"])
    offer.created_at = db_now(db) - timedelta(hours=1)
    offer.expires_at = db_now(db) + timedelta(seconds=seconds)
    db.commit()

    def attempt(index):
        with pg() as session:
            if index:
                return waitlist.expire(session, offered["id"])
            try:
                return waitlist.accept(session, session.get(User, domain["b"].id), offered["id"])
            except DomainError as exc:
                assert exc.status == 409
                return None

    parallel(2, attempt)
    db.expire_all()
    final = db.get(Offer, offered["id"])
    confirmed = db.scalar(
        select(func.count()).select_from(Booking).where(Booking.state == "CONFIRMED")
    )
    active = db.scalar(select(func.count()).select_from(Offer).where(Offer.state == "ACTIVE"))
    assert active == 0
    assert (final.state, confirmed, db.get(Slot, domain["slot_id"]).state) == (
        ("ACCEPTED", 1, "BOOKED") if seconds > 0 else ("EXPIRED", 0, "ALLOCATING")
    )


def test_withdrawal_races_allocation(pg, db, domain, entry):
    joined = entry()

    def attempt(index):
        with pg() as session:
            if index:
                return waitlist.allocate(session, domain["slot_id"])
            return waitlist.withdraw(session, session.get(User, domain["b"].id), joined["id"])

    parallel(2, attempt)
    assert db.scalar(select(func.count()).select_from(Offer).where(Offer.state == "ACTIVE")) == 0
