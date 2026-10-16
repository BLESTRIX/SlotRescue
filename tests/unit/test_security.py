import pytest
from pydantic import ValidationError

from app.core.errors import DomainError
from app.core.security import csrf_token, digest, passwords, valid_csrf, verify_password
from app.core.states import transition
from app.schemas.requests import Register, WaitlistCreate
from app.web.routes import local_input


def test_argon2id_and_token_protection():
    hashed = passwords.hash("a long test passphrase")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "a long test passphrase")
    assert not verify_password(hashed, "wrong")
    token = csrf_token()
    assert valid_csrf(token) and not valid_csrf(token + "tamper")
    assert digest(token) != token


def test_registration_never_accepts_role():
    with pytest.raises(ValidationError):
        Register(name="Eve", email="eve@example.com", password="long-passphrase", role="STAFF")


def test_naive_api_times_are_rejected():
    with pytest.raises(ValidationError):
        WaitlistCreate(
            service_id=1, earliest_start="2030-01-01T10:00", latest_end="2030-01-01T11:00"
        )


def test_web_converts_local_time():
    assert local_input("2030-01-01T10:00").isoformat() == "2030-01-01T05:00:00+00:00"


def test_terminal_transition_is_rejected():
    from app.models import Offer

    offer = Offer(state="EXPIRED")
    with pytest.raises(DomainError):
        transition(offer, "ACCEPTED")
