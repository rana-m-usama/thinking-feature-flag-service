"""Integration test fixtures.

These run against a real Postgres and a real Redis, not fakes. The things most worth
testing here — tenant isolation, the append-only trigger, cache invalidation, cascade
behaviour — are all *database* behaviours. A mocked session would happily let a test
pass while the real query returned another tenant's rows, which is precisely the bug
class this suite exists to catch.

Environment variables are set before any `app.*` import because `app.config` builds its
Settings at module import and `app.db` builds the engine from it. Importing first and
patching after would give you an engine pointed at the development database, and the
first test to run would truncate someone's local data.
"""

import os

# Must precede every app import. See the module docstring.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://flagsvc:flagsvc@localhost:5433/flagsvc_test"
)
# A dedicated Redis database index. Tests flush it, and flushing db 0 would wipe the
# cache of whatever the developer happened to be running locally.
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/15")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("APP_ENV", "local")
# Off by default: the limiter is stateful across tests in Redis, so leaving it on makes
# test outcomes depend on execution order. The tests that care turn it back on.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import asyncio  # noqa: E402
from collections.abc import AsyncGenerator  # noqa: E402

import asyncpg  # noqa: E402
import pytest_asyncio  # noqa: E402
import redis.asyncio as redis  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402

TEST_DB = "flagsvc_test"


async def _create_test_database() -> None:
    """Create the test database if absent, connecting to `postgres` to do it.

    CREATE DATABASE cannot run inside a transaction and cannot run from a connection to
    the database being created, hence the separate admin connection.

    Host and port are parsed out of DATABASE_URL rather than hardcoded. They were briefly
    pinned to localhost:5433 — the port docker-compose.dev.yml publishes to dodge a native
    Postgres on this machine — which is a fact about one laptop, not about the service.
    A CI runner publishes 5432 and the hardcoded value fails there and only there.
    """
    url = settings.database_url
    admin = await asyncpg.connect(
        user=url.hosts()[0]["username"],
        password=url.hosts()[0]["password"],
        database="postgres",  # the one database guaranteed to exist
        host=url.hosts()[0]["host"],
        port=url.hosts()[0]["port"],
    )
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await admin.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_database():
    """Create the database and migrate it once per session.

    Migrations run via Alembic rather than `Base.metadata.create_all` on purpose: the
    append-only trigger and the CHECK constraints exist only in the migration. Using
    create_all would test a schema that never ships, and the immutability test would
    pass against a table that has no trigger on it.
    """
    await _create_test_database()

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", str(settings.database_url))
    await asyncio.to_thread(command.upgrade, config, "head")

    yield

    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_state() -> AsyncGenerator[None, None]:
    """Reset between tests.

    Truncating is not enough on its own: the flag set is cached in Redis keyed by
    (tenant, environment), so a test that leaves a warm entry behind would make the next
    one read flags that no longer exist in the database.

    TRUNCATE rather than DELETE because the audit trigger rejects row-level DELETE — the
    same guarantee the production schema relies on. TRUNCATE fires no row trigger, which
    is exactly why the migration's comment insists the grant is not redundant.
    """
    yield

    async with engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(
            text(
                "TRUNCATE flag_audit_log, flag_environment_configs, flags, api_keys, "
                "tenants RESTART IDENTITY CASCADE"
            )
        )

    client = redis.from_url(str(settings.redis_url))
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """An httpx client speaking directly to the ASGI app — no network, no port."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def tenant_factory(client: AsyncClient):
    """Register a tenant and return (tenant_id, api_key).

    Goes through the real endpoint rather than inserting rows, so the tests exercise key
    generation and hashing the same way a caller would.
    """

    async def _make(name: str = "test-tenant") -> tuple[str, str]:
        response = await client.post(
            "/api/v1/tenants",
            json={"name": name},
            headers={"X-API-Key": settings.admin_api_key},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return body["id"], body["api_key"]

    return _make


@pytest_asyncio.fixture
async def two_tenants(tenant_factory):
    """Two independent tenants — the setup for every isolation test."""
    alpha_id, alpha_key = await tenant_factory("alpha-corp")
    beta_id, beta_key = await tenant_factory("beta-corp")
    return {
        "alpha": {"id": alpha_id, "key": alpha_key},
        "beta": {"id": beta_id, "key": beta_key},
    }
