import secrets
from datetime import timedelta

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.errors import DomainError
from app.core.security import DUMMY_HASH, csrf_token, digest, passwords, verify_password
from app.core.transactions import transactional
from app.db.session import db_now
from app.models import LoginLimit, Session, User


def public_user(user):
    return {key: getattr(user, key) for key in ("id", "name", "email", "role")}


@transactional
def register(db, payload):
    user = User(
        email=str(payload.email).lower(),
        name=payload.name,
        password_hash=passwords.hash(payload.password),
        role="CUSTOMER",
    )
    db.add(user)
    db.flush()
    return public_user(user)


def rate_limit(db, email, ip):
    bucket = int(db_now(db).timestamp()) // 900
    for key, limit in ((digest(f"email:{email.lower()}:{ip}"), 10), (digest(f"ip:{ip}"), 100)):
        statement = insert(LoginLimit).values(key=key, bucket=bucket, attempts=1)
        statement = statement.on_conflict_do_update(
            index_elements=[LoginLimit.key],
            set_={
                "bucket": bucket,
                "attempts": case((LoginLimit.bucket == bucket, LoginLimit.attempts + 1), else_=1),
            },
        ).returning(LoginLimit.attempts)
        attempts = db.scalar(statement)
        # Commit failed attempts too. Persistent counters survive app restarts.
        db.commit()
        if attempts > limit:
            raise DomainError(
                "rate_limited",
                "Too many login attempts. Try again after this 15-minute window.",
                429,
            )


def login(db, payload, ip):
    rate_limit(db, str(payload.email), ip)
    user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if (
        not verify_password(user.password_hash if user else DUMMY_HASH, payload.password)
        or not user
    ):
        raise DomainError("invalid_credentials", "Email or password is incorrect.", 401)
    token, csrf = secrets.token_urlsafe(32), csrf_token()
    db.add(
        Session(
            token_hash=digest(token),
            user_id=user.id,
            csrf_hash=digest(csrf),
            expires_at=db_now(db) + timedelta(hours=settings().session_hours),
        )
    )
    db.commit()
    return public_user(user), token, csrf


def current_user(db, token):
    session = db.get(Session, digest(token)) if token else None
    if not session or session.expires_at <= db_now(db):
        raise DomainError("unauthenticated", "Please sign in to continue.", 401)
    return db.get(User, session.user_id), session


@transactional
def logout(db, token):
    db.execute(delete(Session).where(Session.token_hash == digest(token)))
    return {"message": "Signed out."}
