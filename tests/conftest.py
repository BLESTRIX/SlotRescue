import os
from datetime import timedelta

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.core.config import settings
from app.core.security import passwords
from app.db.session import db_now, engine
from app.models import Service, User
from app.schemas.requests import SlotCreate, WaitlistCreate
from app.services import catalog, waitlist


@pytest.fixture(scope="session")
def pg():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        if os.getenv("REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("TEST_DATABASE_URL is required")
        pytest.skip("Set TEST_DATABASE_URL to a dedicated PostgreSQL 17 database ending in _test")
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql") or not parsed.database.endswith("_test"):
        pytest.fail(
            "Refusing to erase a database that is not PostgreSQL with a name ending in _test"
        )
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    settings.cache_clear()
    engine.cache_clear()
    test_engine = engine()
    with test_engine.connect() as connection:
        version = int(connection.scalar(text("SHOW server_version_num")))
        assert 170000 <= version < 180000, "Integration tests require PostgreSQL 17"
    command.upgrade(Config("alembic.ini"), "head")
    yield sessionmaker(test_engine, expire_on_commit=False)
    test_engine.dispose()
    engine.cache_clear()
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
    settings.cache_clear()


@pytest.fixture
def db(pg):
    with pg() as session:
        # Fixtures commit independently; a rollback-only fixture cannot test concurrent connections.
        session.execute(
            text(
                "TRUNCATE offers, bookings, slots, waitlist_entries, notification_attempts, durable_jobs, audit_events, idempotency_records, sessions, login_limits, services, users RESTART IDENTITY CASCADE"
            )
        )
        session.commit()
        yield session


@pytest.fixture
def domain(db):
    hashed = passwords.hash("TestPassphrase!2026")
    people = [
        User(name=name, email=f"{name}@example.com", role=role, password_hash=hashed)
        for name, role in [
            ("staff", "STAFF"),
            ("otherstaff", "STAFF"),
            ("a", "CUSTOMER"),
            ("b", "CUSTOMER"),
            ("c", "CUSTOMER"),
        ]
    ]
    db.add_all(people)
    service = Service(name="Consultation", description="Test", duration_minutes=30, active=True)
    db.add(service)
    db.commit()
    start = db_now(db) + timedelta(days=2)
    result = catalog.create_slot(
        db, people[0], SlotCreate(service_id=service.id, staff_id=people[0].id, starts_at=start)
    )
    waitlist.allocate(db, result["id"])
    return {
        "staff": people[0],
        "otherstaff": people[1],
        "a": people[2],
        "b": people[3],
        "c": people[4],
        "service": service,
        "slot_id": result["id"],
        "start": start,
    }


@pytest.fixture
def entry(db, domain):
    def create(user="b", **overrides):
        values = {
            "service_id": domain["service"].id,
            "earliest_start": domain["start"] - timedelta(hours=1),
            "latest_end": domain["start"] + timedelta(hours=1),
            **overrides,
        }
        return waitlist.join(db, domain[user], WaitlistCreate(**values))

    return create
