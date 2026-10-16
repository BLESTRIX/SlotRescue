import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.api.routes import router as api
from app.core.config import settings
from app.core.errors import DomainError
from app.core.logging import configure_logging
from app.core.security import valid_csrf
from app.db.session import engine
from app.services.common import request_id

configure_logging()
logger = logging.getLogger("slotrescue.http")


@asynccontextmanager
async def lifespan(app):
    settings()  # Validate configuration before accepting requests; migrations are explicit.
    yield
    engine().dispose()


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    title="SlotRescue",
    version="0.1.0",
    lifespan=lifespan,
    description="Single-business booking and fair cancellation recovery. Cookie authentication: fetch /api/v1/auth/csrf first, then send X-CSRF-Token on every state-changing request. Login rotates the token. All times include a UTC offset.",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api)


def error_response(request, code, message, status, details=None):
    request.state.request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    body = {"error": {"code": code, "message": message, "request_id": request.state.request_id}}
    if details:
        body["error"]["details"] = details
    if request.url.path.startswith(("/api/", "/health/")):
        return JSONResponse(body, status_code=status)
    from app.web.routes import render_error

    return render_error(request, body["error"], status)


@app.exception_handler(DomainError)
async def domain_error(request, exc):
    return error_response(request, exc.code, exc.message, exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    details = [
        {"field": ".".join(map(str, error["loc"])), "message": error["msg"]}
        for error in exc.errors()
    ]
    return error_response(request, "validation_error", "Please check your input.", 422, details)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return error_response(request, "http_error", str(exc.detail), exc.status_code)


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    logger.exception("database.unavailable")
    return error_response(
        request,
        "database_unavailable",
        "The database is temporarily unavailable. Please retry.",
        503,
    )


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    logger.exception("request.failed")
    response = error_response(
        request, "internal_error", "An unexpected error occurred. Please try again.", 500
    )
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.middleware("http")
async def context_and_csrf(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    context_token = request_id.set(request.state.request_id)
    started = time.monotonic()
    try:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            cookie = request.cookies.get("csrf", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not supplied and request.headers.get("content-type", "").split(";")[0] in {
                "application/x-www-form-urlencoded",
                "multipart/form-data",
            }:
                await request.body()
                supplied = str((await request.form()).get("csrf_token", ""))
            origin = request.headers.get("origin")
            if (
                (origin and origin.rstrip("/") != settings().base_url.rstrip("/"))
                or not valid_csrf(cookie)
                or not hmac.compare_digest(cookie, supplied)
            ):
                response = error_response(
                    request,
                    "csrf_failed",
                    "Security check failed. Reload this page and try again.",
                    403,
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        if getattr(request.state, "new_csrf", None):
            response.set_cookie(
                "csrf",
                request.state.new_csrf,
                secure=settings().cookie_secure,
                samesite="lax",
                httponly=False,
                path="/",
                max_age=settings().session_hours * 3600,
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.cookies.get("session"):
            response.headers["Cache-Control"] = "no-store"
        logger.info(
            "request.completed %s %s %s %.3fs",
            request.method,
            request.url.path,
            response.status_code,
            time.monotonic() - started,
        )
        return response
    finally:
        request_id.reset(context_token)


@app.get("/health/live", tags=["Operations"])
def live():
    return {"status": "alive"}


@app.get("/health/ready", tags=["Operations"])
def ready():
    with engine().connect() as connection:
        connection.execute(text("SELECT 1 FROM alembic_version"))
    return {"status": "ready", "database": "ok"}


from app.web.routes import router as web  # noqa: E402

app.include_router(web)
