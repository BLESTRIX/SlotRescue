import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import settings

passwords = PasswordHasher()  # Argon2id, library's maintained memory/time defaults.
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(24))


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token() -> str:
    value = secrets.token_urlsafe(32)
    signature = hmac.new(settings().secret_key.encode(), value.encode(), "sha256").hexdigest()
    return f"{value}.{signature}"


def valid_csrf(token: str) -> bool:
    try:
        value, signature = token.rsplit(".", 1)
        expected = hmac.new(settings().secret_key.encode(), value.encode(), "sha256").hexdigest()
        return hmac.compare_digest(signature, expected)
    except ValueError:
        return False


def verify_password(hashed: str, password: str) -> bool:
    try:
        return passwords.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False
