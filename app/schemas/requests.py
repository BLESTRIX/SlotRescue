from datetime import datetime
from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    model_validator,
)

from app.core.config import settings


def email_address(value: str) -> str:
    try:
        # Reserved .test addresses belong only to the explicit local demo.
        return validate_email(
            value, check_deliverability=False, test_environment=settings().app_env == "development"
        ).normalized
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc


EmailAddress = Annotated[
    str, AfterValidator(email_address), WithJsonSchema({"type": "string", "format": "email"})
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Register(Input):
    name: str = Field(min_length=1, max_length=100)
    email: EmailAddress
    password: str = Field(min_length=12, max_length=128)


class Login(Input):
    email: EmailAddress
    password: str = Field(min_length=1, max_length=128)


class ServiceCreate(Input):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    duration_minutes: int = Field(ge=5, le=480)
    active: bool = True


class ServiceUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    active: bool | None = None

    @model_validator(mode="after")
    def no_nulls(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Service fields cannot be null")
        return self


class SlotCreate(Input):
    service_id: int = Field(gt=0)
    staff_id: int = Field(gt=0)
    starts_at: AwareDatetime


class SlotUpdate(SlotCreate):
    pass


class Book(Input):
    slot_id: int = Field(gt=0)


class WaitlistCreate(Input):
    service_id: int = Field(gt=0)
    earliest_start: AwareDatetime
    latest_end: AwareDatetime
    preferred_staff_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def interval(self):
        if self.latest_end <= self.earliest_start:
            raise ValueError("Latest end must be after earliest start")
        return self


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: list | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str


class ServiceOut(BaseModel):
    id: int
    name: str
    description: str
    duration_minutes: int
    active: bool
    created_at: datetime


class SlotRecordOut(BaseModel):
    id: int
    service_id: int
    staff_id: int
    starts_at: datetime
    ends_at: datetime
    state: str
    created_at: datetime


class SlotOut(SlotRecordOut):
    service_name: str
    staff_name: str
    duration_minutes: int


class BookingOut(BaseModel):
    id: int
    slot_id: int
    customer_id: int
    state: str
    source: str
    created_at: datetime
    canceled_at: datetime | None
    canceled_by_id: int | None


class WaitlistOut(BaseModel):
    id: int
    customer_id: int
    service_id: int
    preferred_staff_id: int | None
    earliest_start: datetime
    latest_end: datetime
    state: str
    created_at: datetime


class OfferOut(BaseModel):
    id: int
    slot_id: int
    entry_id: int
    customer_id: int
    state: str
    expires_at: datetime
    responded_at: datetime | None
    explanation: str
    booking_id: int | None
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    page: int
    page_size: int
    total: int


class JobOut(BaseModel):
    id: int
    kind: str
    dedupe_key: str
    payload: dict
    state: str
    available_at: datetime
    attempts: int
    retry_limit: int
    lease_token: str | None
    lease_until: datetime | None
    last_error: str | None
    finished_at: datetime | None
    created_at: datetime


class AuditOut(BaseModel):
    id: int
    actor_id: int | None
    action: str
    resource_type: str
    resource_id: int
    details: dict
    request_id: str | None
    created_at: datetime


class DashboardOut(BaseModel):
    range_start: datetime
    range_end_exclusive: datetime
    timezone: str
    upcoming_confirmed: int
    cancellations: int
    active_waitlist: int
    active_offers: int
    recovered_bookings: int
