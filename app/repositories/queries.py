from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.inspection import inspect

from app.core.config import settings
from app.core.errors import DomainError, missing
from app.db.session import db_now
from app.models import Booking, Offer, Service, Slot, User, WaitlistEntry


def data(record):
    return jsonable_encoder(
        {column.key: getattr(record, column.key) for column in inspect(record).mapper.column_attrs}
    )


def staff_only(user):
    if user.role != "STAFF":
        raise DomainError("forbidden", "Staff access is required.", 403)


def customer_only(user):
    if user.role != "CUSTOMER":
        raise DomainError("forbidden", "Use a customer account for this action.", 403)


def owned(db, model, resource_id, user, staff=False):
    record = db.get(model, resource_id)
    if not record or (not (staff and user.role == "STAFF") and record.customer_id != user.id):
        raise missing()
    return record


def local_day(day: date):
    zone = ZoneInfo(settings().business_timezone)
    start = datetime.combine(day, time.min, zone)
    end = datetime.combine(day + timedelta(days=1), time.min, zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def paginate(db, query, page=1, page_size=25):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    records = db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "items": [data(record) for record in records],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def slots(
    db,
    service_id=None,
    day=None,
    page=1,
    page_size=25,
    staff_id=None,
    state="AVAILABLE",
    staff=False,
):
    query = select(Slot).join(Service)
    if not staff:
        query = query.where(Service.active, Slot.starts_at > db_now(db))
    if state:
        query = query.where(Slot.state == state)
    if service_id:
        query = query.where(Slot.service_id == service_id)
    if staff_id:
        query = query.where(Slot.staff_id == staff_id)
    if day:
        start, end = local_day(day)
        query = query.where(Slot.starts_at >= start, Slot.starts_at < end)
    result = paginate(db, query.order_by(Slot.starts_at, Slot.id), page, page_size)
    for item in result["items"]:
        service, member = db.get(Service, item["service_id"]), db.get(User, item["staff_id"])
        item.update(
            service_name=service.name,
            staff_name=member.name,
            duration_minutes=service.duration_minutes,
        )
    return result


def bookings(
    db,
    user,
    staff=False,
    day=None,
    service_id=None,
    staff_id=None,
    state=None,
    page=1,
    page_size=25,
):
    query = select(Booking).join(Slot)
    if staff:
        staff_only(user)
    else:
        query = query.where(Booking.customer_id == user.id)
    if day:
        start, end = local_day(day)
        query = query.where(Slot.starts_at >= start, Slot.starts_at < end)
    if service_id:
        query = query.where(Slot.service_id == service_id)
    if staff_id:
        query = query.where(Slot.staff_id == staff_id)
    if state:
        query = query.where(Booking.state == state)
    return paginate(db, query.order_by(Slot.starts_at.desc(), Booking.id.desc()), page, page_size)


def customer_collection(db, model, user, page=1, page_size=25, staff=False, state=None):
    query = select(model)
    if staff:
        staff_only(user)
    else:
        query = query.where(model.customer_id == user.id)
    if state:
        query = query.where(model.state == state)
    return paginate(db, query.order_by(model.created_at.desc(), model.id.desc()), page, page_size)


def dashboard(db, user, start, end):
    staff_only(user)
    if end <= start or (end - start).days > 366:
        raise DomainError("invalid_range", "Choose a date range between 1 and 366 days.", 422)

    def count(query):
        return db.scalar(select(func.count()).select_from(query.subquery()))

    confirmed = (
        select(Booking.id)
        .join(Slot)
        .where(Booking.state == "CONFIRMED", Slot.starts_at >= start, Slot.starts_at < end)
    )
    return {
        "range_start": start.isoformat(),
        "range_end_exclusive": end.isoformat(),
        "timezone": settings().business_timezone,
        "upcoming_confirmed": count(confirmed.where(Slot.starts_at > db_now(db))),
        "cancellations": count(
            select(Booking.id).where(Booking.canceled_at >= start, Booking.canceled_at < end)
        ),
        "active_waitlist": count(
            select(WaitlistEntry.id).where(
                WaitlistEntry.state == "ACTIVE",
                WaitlistEntry.created_at >= start,
                WaitlistEntry.created_at < end,
            )
        ),
        "active_offers": count(
            select(Offer.id).where(
                Offer.state == "ACTIVE",
                Offer.expires_at > db_now(db),
                Offer.created_at >= start,
                Offer.created_at < end,
            )
        ),
        "recovered_bookings": count(confirmed.where(Booking.source == "WAITLIST")),
    }
