"""Alembic environment.

The database URL comes from application settings rather than alembic.ini so that
migrations use the same credential source as the service — on Cloud Run that means
Secret Manager, with no second place for a connection string to drift.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.models import Base

config = context.config

# Deliberately NOT config.set_main_option("sqlalchemy.url", ...).
#
# Alembic's config is a ConfigParser, where '%' is interpolation syntax. A URL-encoded
# password — which is what any generated password becomes once urlencode() has touched it
# — is full of %2B, %23, %25. ConfigParser sees those and raises
# "invalid interpolation syntax", so a perfectly valid URL is rejected by the config
# layer rather than the database.
#
# The usual workaround is .replace('%', '%%'). This skips the layer instead: the engine is
# built directly from settings below, so there is no parser between the URL and the
# driver, and no escaping rule for a future reader to know about.

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(settings.database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Straight from settings — no ConfigParser in the path. See the note above.
    connectable = create_async_engine(str(settings.database_url), poolclass=NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
