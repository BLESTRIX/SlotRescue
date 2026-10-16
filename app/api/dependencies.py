import hmac

from fastapi import Depends, Request

from app.core.errors import DomainError
from app.core.security import csrf_token, digest, valid_csrf
from app.db.session import get_db
from app.repositories.queries import staff_only
from app.services.auth import current_user


def user(request: Request, db=Depends(get_db)):
    account, session = current_user(db, request.cookies.get("session"))
    supplied = request.cookies.get("csrf", "")
    matches = hmac.compare_digest(session.csrf_hash, digest(supplied))
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not matches:
            raise DomainError(
                "csrf_failed", "Your security token changed. Reload this page and try again.", 403
            )
    elif not matches or not valid_csrf(supplied):
        # Restore a lost CSRF cookie only on a safe, authenticated request. The
        # response middleware writes the replacement; unsafe requests still fail.
        replacement = csrf_token()
        session.csrf_hash = digest(replacement)
        db.commit()
        request.state.new_csrf = replacement
    return account


def staff(account=Depends(user)):
    staff_only(account)
    return account
