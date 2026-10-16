from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import settings
from app.db.session import Base
from app.models import entities  # noqa: F401

config = context.config
fileConfig(config.config_file_name)

if context.is_offline_mode():
    context.configure(
        url=settings().database_url, target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    connectable = create_engine(settings().database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
