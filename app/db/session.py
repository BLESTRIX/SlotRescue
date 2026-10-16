from functools import lru_cache

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    return create_engine(
        settings().database_url,
        pool_pre_ping=True,
        pool_size=25,
        max_overflow=10,
        connect_args={"connect_timeout": 5, "options": "-c timezone=UTC"},
    )


def session_factory():
    return sessionmaker(engine(), expire_on_commit=False)


def get_db():
    with session_factory()() as db:
        yield db


def db_now(db: Session):
    # clock_timestamp is wall time, unlike now(), which is transaction start time.
    return db.scalar(select(func.clock_timestamp()))
