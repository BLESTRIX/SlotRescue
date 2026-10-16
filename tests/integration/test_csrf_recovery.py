import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.integration.test_http import login

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("path", ["/slots", "/dashboard", "/api/v1/auth/csrf", "/api/v1/auth/me"])
def test_lost_csrf_cookie_recovers_on_authenticated_safe_get(db, domain, path):
    with TestClient(app) as client:
        login(client, "a@example.com")
        for cookie in list(client.cookies.jar):
            if cookie.name == "csrf":
                client.cookies.delete(cookie.name, domain=cookie.domain, path=cookie.path)
        assert client.post("/api/v1/auth/logout").status_code == 403
        # A safe request can restore a cookie bound to the existing opaque session.
        response = client.get(path)
        assert response.status_code == 200
        assert "Max-Age=604800" in response.headers.get("set-cookie", "")
        assert (
            client.post(
                "/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies["csrf"]}
            ).status_code
            == 200
        )
