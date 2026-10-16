"""Management commands. Run `python -m app.cli --help`."""

import argparse
import getpass
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.security import passwords
from app.db.session import session_factory
from app.models import Service, Slot, User
from app.schemas.requests import ServiceCreate, SlotCreate
from app.services import catalog, jobs, waitlist

DEMO_PASSWORD = "DemoPassphrase!2026"


def create_staff(email, name):
    from pydantic import EmailStr, TypeAdapter

    email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    password = os.getenv("STAFF_PASSWORD") or getpass.getpass("Staff password (12+ characters): ")
    if not 12 <= len(password) <= 128:
        raise SystemExit("Password must contain 12–128 characters.")
    with session_factory()() as db:
        if db.scalar(select(User.id).where(User.email == email)):
            raise SystemExit("That email already exists; no changes made.")
        db.add(User(email=email, name=name, role="STAFF", password_hash=passwords.hash(password)))
        db.commit()
    print(f"Created staff account {email}")


def seed():
    if settings().app_env != "development":
        raise SystemExit("Demo seed is restricted to APP_ENV=development.")
    with session_factory()() as db:
        users = []
        for email, name, role in [
            ("staff@slotrescue.test", "Demo · Sara Ahmed", "STAFF"),
            ("omar@slotrescue.test", "Demo · Omar Khan", "STAFF"),
            ("a@slotrescue.test", "Demo · Customer A", "CUSTOMER"),
            ("b@slotrescue.test", "Demo · Customer B", "CUSTOMER"),
            ("c@slotrescue.test", "Demo · Customer C", "CUSTOMER"),
        ]:
            user = db.scalar(select(User).where(User.email == email))
            if not user:
                user = User(
                    email=email, name=name, role=role, password_hash=passwords.hash(DEMO_PASSWORD)
                )
                db.add(user)
                db.commit()
            users.append(user)
        services = []
        for name, description, duration in [
            ("Demo · Consultation", "A focused conversation to plan your next steps.", 30),
            ("Demo · Wellness session", "Make space to reset and recharge.", 60),
            ("Demo · Follow-up", "A quick check-in to keep things moving.", 20),
        ]:
            record = db.scalar(select(Service).where(Service.name == name))
            if not record:
                result = catalog.create_service(
                    db,
                    users[0],
                    ServiceCreate(name=name, description=description, duration_minutes=duration),
                )
                record = db.get(Service, result["id"])
            services.append(record)
        tomorrow = datetime.now(ZoneInfo(settings().business_timezone)).date() + timedelta(days=1)
        for offset in range(3):
            for index, service in enumerate(services):
                for member in users[:2]:
                    starts_at = datetime.combine(
                        tomorrow + timedelta(days=offset),
                        time(10 + index * 2),
                        ZoneInfo(settings().business_timezone),
                    )
                    if not db.scalar(
                        select(Slot.id).where(
                            Slot.staff_id == member.id, Slot.starts_at == starts_at
                        )
                    ):
                        result = catalog.create_slot(
                            db,
                            users[0],
                            SlotCreate(
                                service_id=service.id, staff_id=member.id, starts_at=starts_at
                            ),
                        )
                        waitlist.allocate(db, result["id"])
    print("Demonstration business seeded. Existing users/passwords were preserved.")
    print(
        "Development accounts: staff@slotrescue.test, a@slotrescue.test, b@slotrescue.test, c@slotrescue.test"
    )
    print(f"Initial demo password: {DEMO_PASSWORD}")


def create_test_db():
    url = make_url(
        os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://slotrescue:slotrescue@127.0.0.1:5432/slotrescue_test",
        )
    )
    if (
        not url.database.endswith("_test")
        or url.database == make_url(settings().database_url).database
    ):
        raise SystemExit(
            "Test database must end in _test and differ from the application database."
        )
    admin = url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        admin.render_as_string(hide_password=False), autocommit=True
    ) as connection:
        if not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)
        ).fetchone():
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
    print(f"Test database ready: {url.database}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    staff = commands.add_parser(
        "create-staff", help="Create staff using a prompted password or STAFF_PASSWORD"
    )
    staff.add_argument("--email", required=True)
    staff.add_argument("--name", required=True)
    commands.add_parser("seed", help="Idempotent development demonstration data")
    commands.add_parser("create-test-db", help="Create the dedicated, disposable test database")
    commands.add_parser("drain-jobs", help="Run one durable-job discovery pass without Redis")
    args = parser.parse_args()
    if args.command == "create-staff":
        create_staff(args.email, args.name)
    elif args.command == "seed":
        seed()
    elif args.command == "create-test-db":
        create_test_db()
    else:
        print(f"Processed {jobs.drain()} due jobs")


if __name__ == "__main__":
    main()
