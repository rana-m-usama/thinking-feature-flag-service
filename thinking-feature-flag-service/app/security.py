"""API key generation, hashing, and the authentication dependency."""

import secrets
import uuid
from hashlib import sha256

from fastapi import Depends, Header, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import ApiKey

KEY_PREFIX = "ffs"


def generate_api_key() -> str:
    """Mint a key. Returned to the caller exactly once, at creation; only the hash is stored."""
    return f"{KEY_PREFIX}_{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    """SHA-256, not bcrypt — see the note on `ApiKey.key_hash` in models.py."""
    return sha256(raw_key.encode()).hexdigest()


# The header is declared Optional and the absence is checked by hand, rather than
# `Header(...)` doing it. A required header makes FastAPI reject a missing key with 422
# Unprocessable Entity, which is the wrong answer: a missing credential is an
# authentication failure, not a malformed request. Clients retry 422s as bugs and 401s
# as "go get a key", and only one of those is right here.
_MISSING = "Missing X-API-Key header"


async def require_admin(x_api_key: str | None = Header(default=None)) -> None:
    """Bootstrap credential for tenant creation.

    POST /tenants cannot authenticate with a tenant key because the tenant — and
    therefore its key — does not exist yet. The endpoint is unimplementable without a
    separate operator credential, so this reads one from the environment. Documented
    in the README as an assumption.
    """
    if x_api_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _MISSING)
    # compare_digest, not ==, so the comparison does not short-circuit on the first
    # wrong byte. Plain equality leaks the key one character at a time to anyone who can
    # measure response time closely enough.
    if not secrets.compare_digest(x_api_key, settings.admin_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin API key")


async def authenticate(
    request: Request,
    x_api_key: str | None = Header(
        default=None, description="Tenant API key issued at tenant registration"
    ),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    """Resolve an API key to its tenant.

    A single indexed lookup on the hash — which is why the hash is deterministic. A
    bcrypt hash would force a scan of every key row in the system per request.
    """
    if x_api_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _MISSING)
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(x_api_key)))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    # Publish the resolved tenant for the middleware, which runs outside the dependency
    # tree and has no other way to learn it. Without this every log line and every
    # metric is labelled "anonymous" — and "evaluations per second by tenant", which
    # the spec asks for by name, silently measures nothing.
    request.state.tenant_id = str(api_key.tenant_id)
    return api_key


async def require_tenant(
    tenant_id: uuid.UUID = Path(..., description="Tenant ID"),
    api_key: ApiKey = Depends(authenticate),
) -> ApiKey:
    """Authenticate, then assert the key actually owns the tenant in the path.

    This is the tenant isolation boundary for every /tenants/{id}/* route. Without the
    ownership check, any valid key could read or mutate any tenant's flags simply by
    changing the URL — authentication without authorisation.

    404 rather than 403 on mismatch: a 403 would confirm that the tenant exists, which
    leaks the tenant list to anyone holding one valid key.
    """
    if api_key.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    return api_key
