import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import staff, user
from app.api.routes import set_cookies
from app.core.config import settings
from app.core.errors import DomainError
from app.core.security import csrf_token, valid_csrf
from app.db.session import db_now, get_db
from app.models import AuditEvent, Booking, DurableJob, Offer, Service, Slot, User, WaitlistEntry
from app.repositories import queries
from app.schemas.requests import (
    Login,
    Register,
    ServiceCreate,
    ServiceUpdate,
    SlotCreate,
    SlotUpdate,
    WaitlistCreate,
)
from app.services import auth, booking, catalog, jobs, waitlist

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory="app/templates")


def local(value, fmt="%a, %d %b · %H:%M"):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(ZoneInfo(settings().business_timezone)).strftime(fmt)


templates.env.filters["local"] = local
templates.env.globals["new_key"] = lambda: str(uuid.uuid4())


def render(request, template, context=None, status=200):
    token = getattr(request.state, "new_csrf", request.cookies.get("csrf", ""))
    if not valid_csrf(token):
        token = csrf_token()
    context = {
        "user": None,
        "timezone": settings().business_timezone,
        "csrf": token,
        **(context or {}),
    }
    response = templates.TemplateResponse(
        request=request, name=template, context=context, status_code=status
    )
    response.set_cookie(
        "csrf",
        token,
        secure=settings().cookie_secure,
        samesite="lax",
        httponly=False,
        path="/",
        max_age=settings().session_hours * 3600,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def render_error(request, error, status):
    return render(
        request,
        "feedback.html" if request.headers.get("HX-Request") else "error.html",
        {"error": error},
        status,
    )


def optional_user(request, db):
    try:
        return user(request, db)
    except DomainError:
        return None


def choices(db):
    return {
        "services": db.scalars(select(Service).order_by(Service.name)).all(),
        "staff_members": db.scalars(
            select(User).where(User.role == "STAFF").order_by(User.name)
        ).all(),
    }


def lookups(db, records, kind):
    """Load only related rows used on the current page."""
    result = {"service_map": {}, "slot_map": {}, "customer_map": {}}
    for record in records:
        if "slot_id" in record:
            slot = db.get(Slot, record["slot_id"])
            result["slot_map"][slot.id] = slot
            result["service_map"][slot.service_id] = db.get(Service, slot.service_id)
        if "service_id" in record:
            result["service_map"][record["service_id"]] = db.get(Service, record["service_id"])
        if "customer_id" in record:
            result["customer_map"][record["customer_id"]] = db.get(User, record["customer_id"])
    return result


@router.get("/")
def home(request: Request, db=Depends(get_db)):
    return render(request, "home.html", {"user": optional_user(request, db)})


@router.get("/docs")
def api_docs(request: Request, db=Depends(get_db)):
    optional_user(request, db)
    return render(request, "docs.html")


@router.get("/login")
@router.get("/register")
def auth_page(request: Request):
    return render(request, "auth.html", {"register": request.url.path == "/register"})


async def form_payload(request):
    values = dict(await request.form())
    values.pop("csrf_token", None)
    return values


def validate(schema, values):
    try:
        return schema.model_validate(values)
    except ValidationError as exc:
        message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise DomainError("validation_error", message, 422) from exc


@router.post("/register")
async def register(request: Request, db=Depends(get_db)):
    payload = validate(Register, await form_payload(request))
    await run_in_threadpool(auth.register, db, payload)
    response = RedirectResponse("/login?registered=1", status_code=303)
    if request.headers.get("HX-Request"):
        response.status_code = 200
        response.headers["HX-Redirect"] = "/login?registered=1"
    return response


@router.post("/login")
async def login(request: Request, db=Depends(get_db)):
    payload = validate(Login, await form_payload(request))
    result, token, csrf = await run_in_threadpool(auth.login, db, payload, request.client.host)
    target = "/staff" if result["role"] == "STAFF" else "/dashboard"
    response = RedirectResponse(target, status_code=303)
    set_cookies(response, token, csrf)
    if request.headers.get("HX-Request"):
        response.status_code = 200
        response.headers["HX-Redirect"] = target
    return response


@router.post("/logout")
def logout(request: Request, db=Depends(get_db), account=Depends(user)):
    auth.logout(db, request.cookies["session"])
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("session")
    response.delete_cookie("csrf")
    return response


@router.get("/slots")
def browse(
    request: Request,
    service_id: int | None = Query(None, gt=0),
    day: date | None = None,
    page: int = Query(1, ge=1),
    db=Depends(get_db),
):
    result = queries.slots(db, service_id, day, page)
    return render(
        request,
        "slots.html",
        {
            "user": optional_user(request, db),
            "result": result,
            "service_id": service_id,
            "day": day,
            **choices(db),
        },
    )


@router.get("/dashboard")
def dashboard(
    request: Request, db=Depends(get_db), account=Depends(user), page: int = Query(1, ge=1)
):
    if account.role == "STAFF":
        return RedirectResponse("/staff", status_code=303)
    bookings = queries.bookings(db, account, page=page)
    offers = queries.customer_collection(db, Offer, account, state="ACTIVE")
    return render(
        request,
        "dashboard.html",
        {
            "user": account,
            "result": bookings,
            "offers": offers,
            **lookups(db, bookings["items"] + offers["items"], "dashboard"),
        },
    )


@router.get("/bookings/{booking_id}")
def booking_detail(booking_id: int, request: Request, db=Depends(get_db), account=Depends(user)):
    record = queries.owned(db, Booking, booking_id, account, staff=True)
    slot = db.get(Slot, record.slot_id)
    return render(
        request,
        "booking.html",
        {
            "user": account,
            "booking": record,
            "slot": slot,
            "service": db.get(Service, slot.service_id),
            "member": db.get(User, slot.staff_id),
            "future": slot.starts_at > db_now(db),
        },
    )


@router.get("/waitlist")
def waitlist_page(
    request: Request, db=Depends(get_db), account=Depends(user), page: int = Query(1, ge=1)
):
    result = queries.customer_collection(db, WaitlistEntry, account, page=page)
    return render(
        request,
        "waitlist.html",
        {
            "user": account,
            "result": result,
            **choices(db),
            **lookups(db, result["items"], "waitlist"),
        },
    )


@router.get("/offers")
def offers_page(
    request: Request, db=Depends(get_db), account=Depends(user), page: int = Query(1, ge=1)
):
    result = queries.customer_collection(db, Offer, account, page=page)
    return render(
        request,
        "offers.html",
        {"user": account, "result": result, **lookups(db, result["items"], "offers")},
    )


@router.get("/offers/{offer_id}")
def offer_detail(offer_id: int, request: Request, db=Depends(get_db), account=Depends(user)):
    record = queries.owned(db, Offer, offer_id, account)
    slot = db.get(Slot, record.slot_id)
    return render(
        request,
        "offer.html",
        {
            "user": account,
            "offer": record,
            "slot": slot,
            "service": db.get(Service, slot.service_id),
            "member": db.get(User, slot.staff_id),
            "active": record.state == "ACTIVE" and record.expires_at > db_now(db),
            "server_now": db_now(db).isoformat(),
        },
    )


@router.get("/staff")
def staff_dashboard(
    request: Request,
    start: date | None = None,
    end: date | None = None,
    db=Depends(get_db),
    account=Depends(staff),
):
    today = db_now(db).astimezone(ZoneInfo(settings().business_timezone)).date()
    start, end = start or today - timedelta(days=30), end or today + timedelta(days=30)
    metrics = queries.dashboard(db, account, queries.local_day(start)[0], queries.local_day(end)[0])
    recent = queries.bookings(db, account, staff=True, page_size=5)
    return render(
        request,
        "staff_dashboard.html",
        {
            "user": account,
            "metrics": metrics,
            "start": start,
            "end": end,
            "result": recent,
            **lookups(db, recent["items"], "bookings"),
        },
    )


@router.get("/staff/{section}")
def staff_page(
    section: Literal["services", "slots", "bookings", "waitlist", "offers", "jobs", "audit"],
    request: Request,
    day: date | None = None,
    service_id: int | None = Query(None, gt=0),
    staff_id: int | None = Query(None, gt=0),
    state: str | None = Query(None, max_length=20),
    page: int = Query(1, ge=1),
    db=Depends(get_db),
    account=Depends(staff),
):
    states = {
        "bookings": ["CONFIRMED", "CANCELED", "COMPLETED"],
        "slots": ["AVAILABLE", "ALLOCATING", "HELD", "BOOKED", "CLOSED"],
        "offers": ["ACTIVE", "ACCEPTED", "DECLINED", "EXPIRED", "REVOKED"],
        "waitlist": ["ACTIVE", "FULFILLED", "WITHDRAWN"],
        "jobs": ["PENDING", "RUNNING", "SUCCEEDED", "FAILED"],
    }.get(section, [])
    if state and state not in states:
        raise DomainError("invalid_state", "Choose a valid status filter.", 422)
    if section == "bookings":
        result = queries.bookings(db, account, True, day, service_id, staff_id, state, page)
    elif section == "slots":
        result = queries.slots(
            db, service_id, day, page, staff_id=staff_id, state=state, staff=True
        )
    elif section in {"waitlist", "offers"}:
        result = queries.customer_collection(
            db,
            WaitlistEntry if section == "waitlist" else Offer,
            account,
            page=page,
            staff=True,
            state=state,
        )
    else:
        model = {"services": Service, "jobs": DurableJob, "audit": AuditEvent}[section]
        query = select(model)
        if state:
            query = query.where(model.state == state)
        result = queries.paginate(db, query.order_by(model.id.desc()), page)
    return render(
        request,
        "staff_list.html",
        {
            "user": account,
            "section": section,
            "result": result,
            "states": states,
            "day": day,
            "service_id": service_id,
            "staff_id": staff_id,
            "state": state,
            **choices(db),
            **lookups(db, result["items"], section),
        },
    )


def local_input(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo:
        return parsed.astimezone(UTC)
    zone = ZoneInfo(settings().business_timezone)
    local_value = parsed.replace(tzinfo=zone)
    # Reject ambiguous/nonexistent wall times instead of silently choosing a DST fold.
    if (
        local_value.utcoffset() != parsed.replace(tzinfo=zone, fold=1).utcoffset()
        or local_value.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != parsed
    ):
        raise DomainError(
            "ambiguous_time",
            "This local time is ambiguous or does not exist. Choose another time.",
            422,
        )
    return local_value.astimezone(UTC)


Action = Literal[
    "book",
    "cancel",
    "join",
    "withdraw",
    "accept",
    "decline",
    "create-service",
    "update-service",
    "create-slot",
    "update-slot",
    "retry-job",
]


@router.post("/actions/{action}")
@router.post("/actions/{action}/{resource_id}")
async def action(
    action: Action,
    request: Request,
    resource_id: int = 0,
    db=Depends(get_db),
    account=Depends(user),
):
    values = await form_payload(request)
    key = values.pop("idempotency_key", None)
    for name in ("starts_at", "earliest_start", "latest_end"):
        if name in values:
            try:
                values[name] = local_input(values[name])
            except ValueError as exc:
                raise DomainError(
                    "invalid_time", "Enter a valid local date and time.", 422
                ) from exc
    if values.get("preferred_staff_id") == "":
        values["preferred_staff_id"] = None

    def dispatch():
        if action == "book":
            return booking.book(db, account, resource_id, key), None
        if action == "cancel":
            return booking.cancel(
                db, account, resource_id, staff=account.role == "STAFF"
            ), "/staff/bookings" if account.role == "STAFF" else "/dashboard"
        if action == "join":
            return waitlist.join(db, account, validate(WaitlistCreate, values)), "/waitlist"
        if action == "withdraw":
            return waitlist.withdraw(db, account, resource_id), "/waitlist"
        if action == "accept":
            return waitlist.accept(db, account, resource_id, key), None
        if action == "decline":
            return waitlist.decline(db, account, resource_id), "/offers"
        if action == "create-service":
            return catalog.create_service(
                db, account, validate(ServiceCreate, values)
            ), "/staff/services"
        if action == "update-service":
            return catalog.update_service(
                db, account, resource_id, validate(ServiceUpdate, values)
            ), "/staff/services"
        if action == "create-slot":
            return catalog.create_slot(db, account, validate(SlotCreate, values)), "/staff/slots"
        if action == "update-slot":
            return catalog.update_slot(
                db, account, resource_id, validate(SlotUpdate, values)
            ), "/staff/slots"
        return jobs.retry(db, account, resource_id), "/staff/jobs"

    result, target = await run_in_threadpool(dispatch)
    if target is None:
        target = f"/bookings/{result['id']}"
    if request.headers.get("HX-Request"):
        response = render(
            request, "feedback.html", {"success": "Saved successfully.", "target": target}
        )
        response.headers["HX-Redirect"] = target
        return response
    return RedirectResponse(target, status_code=303)
