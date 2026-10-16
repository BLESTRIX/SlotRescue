import functools
import time

from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import DomainError


def transactional(function):
    """Service owns commit/rollback. Retry only PostgreSQL deadlock/serialization failures."""

    @functools.wraps(function)
    def wrapped(db, *args, **kwargs):
        for attempt in range(3):
            try:
                result = function(db, *args, **kwargs)
                db.commit()
                return result
            except IntegrityError as exc:
                db.rollback()
                raise DomainError(
                    "constraint_conflict",
                    "This change conflicts with an existing record or appointment. Refresh and try again.",
                ) from exc
            except DBAPIError as exc:
                db.rollback()
                if getattr(exc.orig, "sqlstate", None) not in {"40001", "40P01"}:
                    raise
                if attempt == 2:
                    raise DomainError(
                        "concurrent_change", "Another request changed this resource. Please retry."
                    ) from exc
                time.sleep(0.02 * (2**attempt))
            except Exception:
                db.rollback()
                raise

    return wrapped
