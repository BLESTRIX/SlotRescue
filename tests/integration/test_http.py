import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Session
from app.services import booking, waitlist

pytestmark = pytest.mark.integration


def login(client, email):
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "TestPassphrase!2026"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": client.cookies["csrf"]}


def test_registration_csrf_role_and_session_invalidation(db, domain):
    with TestClient(app) as client:
        payload = {"email": "new@example.com", "name": "New", "password": "TestPassphrase!2026"}
        assert client.post("/api/v1/auth/register", json=payload).status_code == 403
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        assert (
            client.post(
                "/api/v1/auth/register", json={**payload, "role": "STAFF"}, headers=headers
            ).status_code
            == 422
        )
        response = client.post("/api/v1/auth/register", json=payload, headers=headers)
        assert response.status_code == 201 and response.json()["role"] == "CUSTOMER"
        headers = login(client, "new@example.com")
        assert client.get("/api/v1/auth/me").status_code == 200
        assert db.scalar(select(Session.token_hash)) != client.cookies["session"]
        stolen = client.cookies["session"]
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
        client.cookies.set("session", stolen)
        assert client.get("/api/v1/auth/me").status_code == 401


def test_customer_cannot_read_or_modify_other_resources(db, domain, entry):
    booked = booking.book(db, domain["a"], domain["slot_id"])
    joined = entry()
    booking.cancel(db, domain["a"], booked["id"])
    offered = waitlist.allocate(db, domain["slot_id"])
    with TestClient(app) as client:
        headers = login(client, "c@example.com")
        for path in [
            f"/api/v1/bookings/{booked['id']}",
            f"/api/v1/offers/{offered['id']}",
            f"/bookings/{booked['id']}",
            f"/offers/{offered['id']}",
        ]:
            assert client.get(path).status_code == 404
        for path in [
            f"/api/v1/bookings/{booked['id']}/cancel",
            f"/api/v1/waitlist/{joined['id']}/withdraw",
            f"/api/v1/offers/{offered['id']}/accept",
            f"/api/v1/offers/{offered['id']}/decline",
        ]:
            assert client.post(path, headers=headers).status_code == 404
        assert client.get("/api/v1/staff/bookings").status_code == 403
        assert not client.get("/api/v1/offers/me").json()["items"]


def test_browser_pages_forms_and_htmx_csrf(db, domain, entry):
    entry()
    offered = waitlist.allocate(db, domain["slot_id"])
    with TestClient(app, follow_redirects=False) as client:
        for path in ["/", "/login", "/register", "/slots", "/slots?service_id=1"]:
            response = client.get(path)
            assert response.status_code == 200, response.text
        csrf = client.cookies["csrf"]
        # Hidden form CSRF is accepted; login rotates it.
        response = client.post(
            "/login",
            data={"email": "b@example.com", "password": "TestPassphrase!2026", "csrf_token": csrf},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200 and response.headers["HX-Redirect"] == "/dashboard"
        for path in ["/dashboard", "/waitlist", "/offers", f"/offers/{offered['id']}"]:
            assert client.get(path).status_code == 200
        path = f"/actions/accept/{offered['id']}"
        assert client.post(path, headers={"HX-Request": "true"}).status_code == 403
        result = client.post(
            path,
            data={"csrf_token": client.cookies["csrf"], "idempotency_key": "browser-accept"},
            headers={"HX-Request": "true"},
        )
        assert result.status_code == 200 and result.headers["HX-Redirect"].startswith("/bookings/")
        assert client.get(result.headers["HX-Redirect"]).status_code == 200
        assert (
            client.post("/logout", data={"csrf_token": client.cookies["csrf"]}).status_code == 303
        )
        login(client, "staff@example.com")
        for path in [
            "/staff",
            "/staff/services",
            "/staff/slots",
            "/staff/bookings",
            "/staff/waitlist",
            "/staff/offers",
            "/staff/jobs",
            "/staff/audit",
        ]:
            response = client.get(path)
            assert response.status_code == 200, response.text


def test_login_rate_limit_and_consistent_errors(db, domain):
    with TestClient(app) as client:
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        for _index in range(11):
            response = client.post(
                "/api/v1/auth/login",
                json={"email": "a@example.com", "password": "wrong"},
                headers={"X-CSRF-Token": csrf},
            )
        assert response.status_code == 429
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
        assert response.json()["error"]["code"] == "rate_limited"


def test_csrf_origin_and_session_binding(db, domain):
    with TestClient(app) as client:
        headers = login(client, "a@example.com")
        assert (
            client.post(
                "/api/v1/auth/logout", headers={**headers, "Origin": "https://attacker.example"}
            ).status_code
            == 403
        )
        client.cookies.clear()
        assert client.get("/api/v1/auth/me").status_code == 401
