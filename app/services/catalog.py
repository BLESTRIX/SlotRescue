from datetime import timedelta

from sqlalchemy import select

from app.core.errors import DomainError, missing
from app.core.states import transition
from app.core.transactions import transactional
from app.db.session import db_now
from app.models import Booking, Offer, Service, Slot, User
from app.repositories.queries import data, staff_only
from app.services.common import allocation_job, audit


@transactional
def create_service(db, user, payload):
    staff_only(user)
    record = Service(**payload.model_dump())
    db.add(record)
    db.flush()
    audit(db, user, "service.created", record)
    return data(record)


@transactional
def update_service(db, user, service_id, payload):
    staff_only(user)
    service = db.scalar(select(Service).where(Service.id == service_id).with_for_update())
    if not service:
        raise missing()
    changes = payload.model_dump(exclude_unset=True)
    if "duration_minutes" in changes and changes["duration_minutes"] != service.duration_minutes:
        if db.scalar(
            select(Slot.id)
            .where(Slot.service_id == service.id, Slot.starts_at > db_now(db))
            .limit(1)
        ):
            raise DomainError(
                "duration_has_future_slots",
                "Duration cannot change while future slots exist. Create a new service with the new duration.",
            )
    for key, value in changes.items():
        setattr(service, key, value)
    audit(db, user, "service.updated", service, changes)
    return data(service)


def validate_slot(db, payload):
    service = db.scalar(
        select(Service).where(Service.id == payload.service_id).with_for_update(read=True)
    )
    if not service or not service.active:
        raise DomainError("inactive_service", "Choose an active service.", 422)
    staff = db.get(User, payload.staff_id)
    if not staff or staff.role != "STAFF":
        raise DomainError("invalid_staff", "Choose a staff member.", 422)
    if payload.starts_at <= db_now(db):
        raise DomainError("past_slot", "Appointment slots must start in the future.", 422)
    return service


@transactional
def create_slot(db, user, payload):
    staff_only(user)
    service = validate_slot(db, payload)
    slot = Slot(
        **payload.model_dump(),
        ends_at=payload.starts_at + timedelta(minutes=service.duration_minutes),
        state="ALLOCATING",
    )
    db.add(slot)
    db.flush()
    allocation_job(db, slot, "created")
    audit(db, user, "slot.created", slot)
    return data(slot)


@transactional
def update_slot(db, user, slot_id, payload):
    staff_only(user)
    service = validate_slot(db, payload)
    slot = db.scalar(select(Slot).where(Slot.id == slot_id).with_for_update())
    if not slot:
        raise missing()
    if slot.state not in {"AVAILABLE", "ALLOCATING"}:
        raise DomainError("slot_occupied", "Only available or allocating slots can be edited.")
    if db.scalar(select(Booking.id).where(Booking.slot_id == slot.id).limit(1)) or db.scalar(
        select(Offer.id).where(Offer.slot_id == slot.id).limit(1)
    ):
        raise DomainError(
            "slot_has_history",
            "This slot has appointment history. Create a new slot to preserve those appointment details.",
        )
    for key, value in payload.model_dump().items():
        setattr(slot, key, value)
    slot.ends_at = payload.starts_at + timedelta(minutes=service.duration_minutes)
    if slot.state == "AVAILABLE":
        transition(slot, "ALLOCATING")
    audit(db, user, "slot.updated", slot)
    db.flush()
    allocation_job(db, slot, f"edit:{db_now(db).isoformat()}")
    return data(slot)
