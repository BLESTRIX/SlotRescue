from datetime import date, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select

from app.api.dependencies import staff, user
from app.core.config import settings
from app.core.errors import DomainError
from app.db.session import db_now, get_db
from app.models import AuditEvent, Booking, DurableJob, Offer, Service, User, WaitlistEntry
from app.repositories import queries
from app.schemas.requests import (
    AuditOut,
    Book,
    BookingOut,
    DashboardOut,
    ErrorResponse,
    JobOut,
    Login,
    OfferOut,
    Page,
    Register,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
    SlotCreate,
    SlotOut,
    SlotRecordOut,
    SlotUpdate,
    UserOut,
    WaitlistCreate,
    WaitlistOut,
)
from app.services import auth, booking, catalog, jobs, waitlist

router = APIRouter(
    prefix="/api/v1",
    responses={status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 429)},
)
DB = Annotated[object, Depends(get_db)]
Account = Annotated[User, Depends(user)]
Staff = Annotated[User, Depends(staff)]
Number = Annotated[int, Query(ge=1)]
Size = Annotated[int, Query(ge=1, le=100)]
Key = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


def set_cookies(response, token, csrf):
    common = {
        "secure": settings().cookie_secure,
        "samesite": "lax",
        "path": "/",
        "max_age": settings().session_hours * 3600,
    }
    response.set_cookie("session", token, httponly=True, **common)
    response.set_cookie("csrf", csrf, httponly=False, **common)
    response.headers["Cache-Control"] = "no-store"


@router.get("/auth/csrf", tags=["Authentication"])
def csrf(request: Request, response: Response, db: DB):
    """Fetch first; send csrf_token as X-CSRF-Token on every unsafe request."""
    from app.core.security import csrf_token, valid_csrf

    token = request.cookies.get("csrf", "")
    if request.cookies.get("session"):
        try:
            user(request, db)
            token = getattr(request.state, "new_csrf", token)
        except DomainError as exc:
            if exc.status != 401:
                raise
    if not valid_csrf(token):
        token = csrf_token()
    response.set_cookie(
        "csrf",
        token,
        httponly=False,
        secure=settings().cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings().session_hours * 3600,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": token}


@router.post("/auth/register", status_code=201, response_model=UserOut, tags=["Authentication"])
def register(payload: Register, db: DB):
    return auth.register(db, payload)


@router.post("/auth/login", response_model=UserOut, tags=["Authentication"])
def login(payload: Login, request: Request, response: Response, db: DB):
    result, token, csrf_value = auth.login(db, payload, request.client.host)
    set_cookies(response, token, csrf_value)
    return result


@router.post("/auth/logout", tags=["Authentication"])
def logout(request: Request, response: Response, db: DB, account: Account):
    result = auth.logout(db, request.cookies["session"])
    response.delete_cookie("session")
    response.delete_cookie("csrf")
    return result


@router.get("/auth/me", response_model=UserOut, tags=["Authentication"])
def me(account: Account):
    return auth.public_user(account)


@router.get("/services", response_model=Page[ServiceOut], tags=["Customer"])
def services(db: DB, page: Number = 1, page_size: Size = 25):
    return queries.paginate(
        db,
        select(Service).where(Service.active).order_by(Service.name, Service.id),
        page,
        page_size,
    )


@router.get("/slots", response_model=Page[SlotOut], tags=["Customer"])
def slots(
    db: DB,
    service_id: Annotated[int | None, Query(gt=0)] = None,
    day: date | None = None,
    page: Number = 1,
    page_size: Size = 25,
):
    return queries.slots(db, service_id, day, page, page_size)


@router.post("/bookings", status_code=201, response_model=BookingOut, tags=["Customer"])
def book(payload: Book, db: DB, account: Account, key: Key = None):
    return booking.book(db, account, payload.slot_id, key)


@router.get("/bookings/me", response_model=Page[BookingOut], tags=["Customer"])
def my_bookings(db: DB, account: Account, page: Number = 1, page_size: Size = 25):
    return queries.bookings(db, account, page=page, page_size=page_size)


@router.get("/bookings/{booking_id}", response_model=BookingOut, tags=["Customer"])
def booking_details(booking_id: int, db: DB, account: Account):
    return queries.data(queries.owned(db, Booking, booking_id, account))


@router.post("/bookings/{booking_id}/cancel", response_model=BookingOut, tags=["Customer"])
def cancel(booking_id: int, db: DB, account: Account):
    return booking.cancel(db, account, booking_id)


@router.post("/waitlist", status_code=201, response_model=WaitlistOut, tags=["Customer"])
def join(payload: WaitlistCreate, db: DB, account: Account):
    return waitlist.join(db, account, payload)


@router.get("/waitlist/me", response_model=Page[WaitlistOut], tags=["Customer"])
def my_waitlist(db: DB, account: Account, page: Number = 1, page_size: Size = 25):
    return queries.customer_collection(db, WaitlistEntry, account, page, page_size)


@router.post("/waitlist/{entry_id}/withdraw", response_model=WaitlistOut, tags=["Customer"])
def withdraw(entry_id: int, db: DB, account: Account):
    return waitlist.withdraw(db, account, entry_id)


@router.get("/offers/me", response_model=Page[OfferOut], tags=["Customer"])
def my_offers(db: DB, account: Account, page: Number = 1, page_size: Size = 25):
    return queries.customer_collection(db, Offer, account, page, page_size)


@router.get("/offers/{offer_id}", response_model=OfferOut, tags=["Customer"])
def offer_details(offer_id: int, db: DB, account: Account):
    return queries.data(queries.owned(db, Offer, offer_id, account))


@router.post("/offers/{offer_id}/accept", response_model=BookingOut, tags=["Customer"])
def accept(offer_id: int, db: DB, account: Account, key: Key = None):
    return waitlist.accept(db, account, offer_id, key)


@router.post("/offers/{offer_id}/decline", response_model=OfferOut, tags=["Customer"])
def decline(offer_id: int, db: DB, account: Account):
    return waitlist.decline(db, account, offer_id)


@router.get("/staff/services", response_model=Page[ServiceOut], tags=["Staff"])
def staff_services(db: DB, account: Staff, page: Number = 1, page_size: Size = 25):
    return queries.paginate(db, select(Service).order_by(Service.id), page, page_size)


@router.post("/staff/services", response_model=ServiceOut, status_code=201, tags=["Staff"])
def create_service(payload: ServiceCreate, db: DB, account: Staff):
    return catalog.create_service(db, account, payload)


@router.patch("/staff/services/{service_id}", response_model=ServiceOut, tags=["Staff"])
def update_service(service_id: int, payload: ServiceUpdate, db: DB, account: Staff):
    return catalog.update_service(db, account, service_id, payload)


@router.post("/staff/slots", status_code=201, response_model=SlotRecordOut, tags=["Staff"])
def create_slot(payload: SlotCreate, db: DB, account: Staff):
    return catalog.create_slot(db, account, payload)


@router.patch("/staff/slots/{slot_id}", response_model=SlotRecordOut, tags=["Staff"])
def update_slot(slot_id: int, payload: SlotUpdate, db: DB, account: Staff):
    return catalog.update_slot(db, account, slot_id, payload)


@router.get("/staff/slots", response_model=Page[SlotOut], tags=["Staff"])
def staff_slots(
    db: DB,
    account: Staff,
    day: date | None = None,
    service_id: Annotated[int | None, Query(gt=0)] = None,
    staff_id: Annotated[int | None, Query(gt=0)] = None,
    state: Literal["AVAILABLE", "ALLOCATING", "HELD", "BOOKED", "CLOSED"] | None = None,
    page: Number = 1,
    page_size: Size = 25,
):
    return queries.slots(db, service_id, day, page, page_size, staff_id, state, staff=True)


@router.get("/staff/bookings", response_model=Page[BookingOut], tags=["Staff"])
def staff_bookings(
    db: DB,
    account: Staff,
    day: date | None = None,
    service_id: Annotated[int | None, Query(gt=0)] = None,
    staff_id: Annotated[int | None, Query(gt=0)] = None,
    state: Literal["CONFIRMED", "CANCELED", "COMPLETED"] | None = None,
    page: Number = 1,
    page_size: Size = 25,
):
    return queries.bookings(db, account, True, day, service_id, staff_id, state, page, page_size)


@router.post("/staff/bookings/{booking_id}/cancel", response_model=BookingOut, tags=["Staff"])
def staff_cancel(booking_id: int, db: DB, account: Staff):
    return booking.cancel(db, account, booking_id, staff=True)


@router.get("/staff/waitlist", response_model=Page[WaitlistOut], tags=["Staff"])
def staff_waitlist(
    db: DB,
    account: Staff,
    state: Literal["ACTIVE", "FULFILLED", "WITHDRAWN"] | None = None,
    page: Number = 1,
    page_size: Size = 25,
):
    return queries.customer_collection(db, WaitlistEntry, account, page, page_size, True, state)


@router.get("/staff/offers", response_model=Page[OfferOut], tags=["Staff"])
def staff_offers(
    db: DB,
    account: Staff,
    state: Literal["ACTIVE", "ACCEPTED", "DECLINED", "EXPIRED", "REVOKED"] | None = None,
    page: Number = 1,
    page_size: Size = 25,
):
    return queries.customer_collection(db, Offer, account, page, page_size, True, state)


@router.get("/staff/audit-events", response_model=Page[AuditOut], tags=["Staff"])
def audit_events(db: DB, account: Staff, page: Number = 1, page_size: Size = 25):
    return queries.paginate(db, select(AuditEvent).order_by(AuditEvent.id.desc()), page, page_size)


@router.get("/staff/jobs", response_model=Page[JobOut], tags=["Staff"])
def staff_jobs(
    db: DB,
    account: Staff,
    state: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"] | None = None,
    page: Number = 1,
    page_size: Size = 25,
):
    query = select(DurableJob)
    if state:
        query = query.where(DurableJob.state == state)
    return queries.paginate(db, query.order_by(DurableJob.id.desc()), page, page_size)


@router.post("/staff/jobs/{job_id}/retry", response_model=JobOut, tags=["Staff"])
def retry(job_id: int, db: DB, account: Staff):
    return jobs.retry(db, account, job_id)


@router.get("/staff/dashboard", response_model=DashboardOut, tags=["Staff"])
def dashboard(db: DB, account: Staff, start: date | None = None, end: date | None = None):
    today = db_now(db).astimezone(ZoneInfo(settings().business_timezone)).date()
    return queries.dashboard(
        db,
        account,
        queries.local_day(start or today - timedelta(days=30))[0],
        queries.local_day(end or today + timedelta(days=30))[0],
    )
