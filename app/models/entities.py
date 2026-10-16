from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Identity:
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Identity, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="CUSTOMER")
    __table_args__ = (CheckConstraint("role IN ('CUSTOMER','STAFF')"),)


class Session(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginLimit(Base):
    __tablename__ = "login_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=1)


class Service(Identity, Base):
    __tablename__ = "services"
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    duration_minutes: Mapped[int]
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (CheckConstraint("duration_minutes BETWEEN 5 AND 480"),)


class Slot(Identity, Base):
    __tablename__ = "slots"
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), default="ALLOCATING", index=True)
    __table_args__ = (
        CheckConstraint("ends_at > starts_at"),
        CheckConstraint("state IN ('AVAILABLE','ALLOCATING','HELD','BOOKED','CLOSED')"),
        ExcludeConstraint(
            ("staff_id", "="),
            (func.tstzrange(starts_at, ends_at, "[)"), "&&"),
            name="staff_slots_no_overlap",
            using="gist",
        ),
    )


class Booking(Identity, Base):
    __tablename__ = "bookings"
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(16), default="CONFIRMED")
    source: Mapped[str] = mapped_column(String(16), default="DIRECT")
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (
        CheckConstraint("state IN ('CONFIRMED','CANCELED','COMPLETED')"),
        CheckConstraint("source IN ('DIRECT','WAITLIST')"),
        CheckConstraint(
            "(state = 'CANCELED') = (canceled_at IS NOT NULL AND canceled_by_id IS NOT NULL)"
        ),
        Index(
            "one_confirmed_booking_per_slot",
            "slot_id",
            unique=True,
            postgresql_where=text("state = 'CONFIRMED'"),
        ),
    )


class WaitlistEntry(Identity, Base):
    __tablename__ = "waitlist_entries"
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    preferred_staff_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    earliest_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latest_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    __table_args__ = (
        CheckConstraint("latest_end > earliest_start"),
        CheckConstraint("state IN ('ACTIVE','FULFILLED','WITHDRAWN')"),
        Index(
            "no_identical_active_entry",
            "customer_id",
            "service_id",
            "earliest_start",
            "latest_end",
            func.coalesce(preferred_staff_id, 0),
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
        Index(
            "waitlist_fifo",
            "service_id",
            "created_at",
            "id",
            postgresql_where=text("state = 'ACTIVE'"),
        ),
    )


class Offer(Identity, Base):
    __tablename__ = "offers"
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), index=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("waitlist_entries.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    explanation: Mapped[str] = mapped_column(Text)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"), unique=True)
    __table_args__ = (
        CheckConstraint("state IN ('ACTIVE','ACCEPTED','DECLINED','EXPIRED','REVOKED')"),
        CheckConstraint("expires_at > created_at"),
        UniqueConstraint("entry_id", "slot_id", name="offer_entry_slot_once"),
        Index(
            "one_active_offer_per_slot",
            "slot_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
        Index(
            "one_active_offer_per_entry",
            "entry_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
    )


class IdempotencyRecord(Identity, Base):
    __tablename__ = "idempotency_records"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    operation: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("user_id", "operation", "key"),)


class DurableJob(Identity, Base):
    __tablename__ = "durable_jobs"
    kind: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    retry_limit: Mapped[int] = mapped_column(default=5)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("state IN ('PENDING','RUNNING','SUCCEEDED','FAILED')"),
        CheckConstraint("attempts >= 0"),
    )


class NotificationAttempt(Identity, Base):
    __tablename__ = "notification_attempts"
    job_id: Mapped[int] = mapped_column(ForeignKey("durable_jobs.id"), index=True)
    attempt: Mapped[int]
    state: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("job_id", "attempt"),
        CheckConstraint("state IN ('SENT','FAILED','SKIPPED')"),
    )


class AuditEvent(Identity, Base):
    __tablename__ = "audit_events"
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[int]
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64))
