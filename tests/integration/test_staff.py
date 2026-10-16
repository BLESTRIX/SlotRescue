from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.errors import DomainError
from app.db.session import db_now
from app.models import Service
from app.schemas.requests import ServiceCreate, ServiceUpdate, SlotUpdate
from app.services import booking, catalog, waitlist

pytestmark = pytest.mark.integration


def test_service_management_authorization_and_slot_edit_history(db, domain):
    payload = ServiceCreate(name="A new service", duration_minutes=45)
    with pytest.raises(DomainError) as exc:
        catalog.create_service(db, domain["a"], payload)
    assert exc.value.status == 403
    service = catalog.create_service(db, domain["staff"], payload)
    updated = catalog.update_service(
        db, domain["staff"], service["id"], ServiceUpdate(active=False)
    )
    assert not updated["active"] and db.get(Service, service["id"])
    slot_payload = SlotUpdate(
        service_id=domain["service"].id,
        staff_id=domain["staff"].id,
        starts_at=domain["start"] + timedelta(days=1),
    )
    changed = catalog.update_slot(db, domain["staff"], domain["slot_id"], slot_payload)
    assert changed["state"] == "ALLOCATING"
    waitlist.allocate(db, domain["slot_id"])
    booked = booking.book(db, domain["a"], domain["slot_id"])
    booking.cancel(db, domain["a"], booked["id"])
    waitlist.allocate(db, domain["slot_id"])
    with pytest.raises(DomainError, match="history"):
        catalog.update_slot(db, domain["staff"], domain["slot_id"], slot_payload)


def test_dashboard_recovery_range_and_api_response_schemas(db, domain, entry):
    from tests.integration.test_http import login

    entry()
    offer = waitlist.allocate(db, domain["slot_id"])
    waitlist.accept(db, domain["b"], offer["id"])
    with TestClient(app) as client:
        login(client, "staff@example.com")
        response = client.get("/api/v1/staff/dashboard")
        assert response.status_code == 200 and response.json()["recovered_bookings"] == 1
        assert response.json()["active_offers"] == 0
        today = db_now(db).date()
        old = client.get(
            "/api/v1/staff/dashboard",
            params={
                "start": str(today - timedelta(days=10)),
                "end": str(today - timedelta(days=1)),
            },
        )
        assert old.json()["recovered_bookings"] == 0
        assert (
            client.get(
                "/api/v1/staff/dashboard", params={"start": str(today), "end": str(today)}
            ).status_code
            == 422
        )
        for path in [
            "/api/v1/staff/services",
            "/api/v1/staff/slots",
            "/api/v1/staff/bookings",
            "/api/v1/staff/offers",
            "/api/v1/staff/waitlist",
            "/api/v1/staff/audit-events",
            "/api/v1/staff/jobs",
        ]:
            response = client.get(path, params={"page_size": 1})
            assert response.status_code == 200, response.text
            assert len(response.json()["items"]) <= 1


from app.main import app  # noqa: E402
