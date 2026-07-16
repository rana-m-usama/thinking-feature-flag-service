"""Tenant registration."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import ApiKey, Tenant
from app.schemas import TenantCreate, TenantCreated
from app.security import generate_api_key, hash_api_key, require_admin

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


@router.post(
    "",
    response_model=TenantCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Register a new tenant (application)",
    description=(
        "Creates a tenant and mints its first API key.\n\n"
        "**Authenticated with `ADMIN_API_KEY`, not a tenant key.** Every other endpoint "
        "authenticates as a tenant, but at registration the tenant does not exist yet and "
        "neither does its key — the endpoint cannot authenticate as the thing it is about "
        "to create. The operator credential comes from the environment. This is an "
        "assumption; the spec does not describe how tenant creation is authorised.\n\n"
        "The returned `api_key` is shown **once**. Only its SHA-256 hash is stored, so it "
        "cannot be recovered or re-displayed.\n\n"
        "All three environments (`development`, `staging`, `production`) exist implicitly "
        "for every tenant — they are an enum, not rows, so there is nothing to provision."
    ),
    responses={401: {"description": "Missing or invalid admin API key"}},
)
async def create_tenant(payload: TenantCreate, db: AsyncSession = Depends(get_db)) -> TenantCreated:
    tenant = Tenant(name=payload.name)
    db.add(tenant)
    await db.flush()

    raw_key = generate_api_key()
    db.add(ApiKey(tenant_id=tenant.id, key_hash=hash_api_key(raw_key), name="default"))
    await db.commit()
    await db.refresh(tenant)

    return TenantCreated(
        id=tenant.id, name=tenant.name, created_at=tenant.created_at, api_key=raw_key
    )
