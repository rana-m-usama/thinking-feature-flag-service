"""Tenant isolation and environment scoping.

"Integration tests for tenant isolation (verify one tenant cannot access another's
flags) and environment scoping."

The threat model is not an anonymous attacker — that is the boring case, and a missing
key is a 401 from the first line of the dependency. The interesting case is a *customer*
holding a perfectly valid key for their own tenant, who changes a UUID in a URL or a
field in a JSON body. Authentication passes. Only authorisation stands between them and
another customer's data, and authorisation is the thing that gets refactored.

Every route that takes a tenant is covered, because isolation is only as strong as the
weakest endpoint and the weakest endpoint is invariably the one nobody tested.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

BOOL_FLAG = {
    "key": "checkout.new_flow",
    "name": "New checkout",
    "type": "boolean",
    "default_value": False,
}


async def _create_flag(client: AsyncClient, tenant_id: str, key: str, payload=None) -> None:
    body = {**BOOL_FLAG, **(payload or {})}
    response = await client.post(
        f"/api/v1/tenants/{tenant_id}/flags",
        json=body,
        headers={"X-API-Key": key},
    )
    assert response.status_code == 201, response.text


class TestTenantIsolation:
    async def test_cannot_list_another_tenants_flags(self, client, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await _create_flag(client, alpha["id"], alpha["key"])

        response = await client.get(
            f"/api/v1/tenants/{alpha['id']}/flags", headers={"X-API-Key": beta["key"]}
        )
        assert response.status_code == 404

    async def test_cannot_create_flag_in_another_tenant(self, client, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        response = await client.post(
            f"/api/v1/tenants/{alpha['id']}/flags",
            json=BOOL_FLAG,
            headers={"X-API-Key": beta["key"]},
        )
        assert response.status_code == 404

    async def test_cannot_update_another_tenants_flag(self, client, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await _create_flag(client, alpha["id"], alpha["key"])

        response = await client.put(
            f"/api/v1/tenants/{alpha['id']}/flags/checkout.new_flow",
            json={"environment": "production", "enabled": True},
            headers={"X-API-Key": beta["key"]},
        )
        assert response.status_code == 404

        # The write must not have landed. A 404 that still mutated would be worse than
        # a 200 — it would be a silent cross-tenant write.
        check = await client.get(
            f"/api/v1/tenants/{alpha['id']}/flags", headers={"X-API-Key": alpha["key"]}
        )
        production = next(
            c for c in check.json()[0]["configs"] if c["environment"] == "production"
        )
        assert production["enabled"] is False

    async def test_cannot_archive_another_tenants_flag(self, client, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await _create_flag(client, alpha["id"], alpha["key"])

        response = await client.delete(
            f"/api/v1/tenants/{alpha['id']}/flags/checkout.new_flow",
            headers={"X-API-Key": beta["key"]},
        )
        assert response.status_code == 404

        check = await client.get(
            f"/api/v1/tenants/{alpha['id']}/flags", headers={"X-API-Key": alpha["key"]}
        )
        assert check.json()[0]["archived_at"] is None

    async def test_cannot_read_another_tenants_audit_history(self, client, two_tenants):
        """Audit history leaks change patterns even when flag values are protected."""
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await _create_flag(client, alpha["id"], alpha["key"])

        response = await client.get(
            f"/api/v1/tenants/{alpha['id']}/flags/checkout.new_flow/history",
            headers={"X-API-Key": beta["key"]},
        )
        assert response.status_code == 404

    async def test_cannot_evaluate_with_spoofed_tenant_id(self, client, two_tenants):
        """The body is caller-controlled input; the key is the authority on tenancy.

        This is the one endpoint where tenant_id arrives in the payload rather than the
        URL, which makes it the easiest place to forget the check.
        """
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await _create_flag(client, alpha["id"], alpha["key"])

        response = await client.post(
            "/api/v1/evaluate",
            json={
                "tenant_id": alpha["id"],
                "environment": "production",
                "user_id": "user_1",
                "context": {},
            },
            headers={"X-API-Key": beta["key"]},
        )
        assert response.status_code == 403

    async def test_cannot_bulk_evaluate_with_spoofed_tenant_id(self, client, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        response = await client.post(
            "/api/v1/evaluate/bulk",
            json={
                "tenant_id": alpha["id"],
                "environment": "production",
                "user_id": "user_1",
                "context": {},
            },
            headers={"X-API-Key": beta["key"]},
        )
        assert response.status_code == 403

    async def test_identical_flag_keys_stay_separate(self, client, two_tenants):
        """The same flag key in two tenants must be two independent flags.

        `key` is unique per *tenant*, not globally. If the uniqueness constraint or a
        query lost its tenant scope, this is where it shows: beta would see alpha's
        enabled flag, or the second create would 409.
        """
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await _create_flag(client, alpha["id"], alpha["key"])
        await _create_flag(client, beta["id"], beta["key"])

        await client.put(
            f"/api/v1/tenants/{alpha['id']}/flags/checkout.new_flow",
            json={"environment": "production", "enabled": True, "rollout_percentage": 100},
            headers={"X-API-Key": alpha["key"]},
        )

        for tenant, expected in ((alpha, True), (beta, False)):
            response = await client.post(
                "/api/v1/evaluate",
                json={
                    "tenant_id": tenant["id"],
                    "environment": "production",
                    "user_id": "user_1",
                    "context": {},
                },
                headers={"X-API-Key": tenant["key"]},
            )
            assert response.json()["flags"]["checkout.new_flow"] is expected

    async def test_cache_is_not_shared_between_tenants(self, client, two_tenants):
        """The cache key includes the tenant.

        Caching is where isolation quietly dies: the auth layer can be flawless while a
        cache keyed only on environment serves alpha's flag set to beta. Alpha is
        evaluated first here specifically to warm the cache before beta reads.
        """
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await _create_flag(client, alpha["id"], alpha["key"], {"key": "alpha.only"})

        body = {"environment": "production", "user_id": "user_1", "context": {}}
        warm = await client.post(
            "/api/v1/evaluate/bulk",
            json={**body, "tenant_id": alpha["id"]},
            headers={"X-API-Key": alpha["key"]},
        )
        assert "alpha.only" in warm.json()["flags"]

        response = await client.post(
            "/api/v1/evaluate/bulk",
            json={**body, "tenant_id": beta["id"]},
            headers={"X-API-Key": beta["key"]},
        )
        assert response.json()["flags"] == {}


class TestAuthentication:
    async def test_missing_key_is_401_not_422(self, client, two_tenants):
        """A missing credential is an auth failure, not a malformed request.

        FastAPI's `Header(...)` would make this a 422, which tells a client to fix its
        payload when it should be telling them to get a key.
        """
        alpha = two_tenants["alpha"]
        response = await client.get(f"/api/v1/tenants/{alpha['id']}/flags")
        assert response.status_code == 401

    async def test_forged_key_is_401(self, client, two_tenants):
        alpha = two_tenants["alpha"]
        response = await client.get(
            f"/api/v1/tenants/{alpha['id']}/flags", headers={"X-API-Key": "ffs_forged"}
        )
        assert response.status_code == 401

    async def test_tenant_key_cannot_create_tenants(self, client, two_tenants):
        """Tenant registration takes the operator credential, not a tenant key."""
        alpha = two_tenants["alpha"]
        response = await client.post(
            "/api/v1/tenants", json={"name": "escalated"}, headers={"X-API-Key": alpha["key"]}
        )
        assert response.status_code == 401

    async def test_admin_key_is_not_a_tenant_key(self, client, two_tenants):
        """The operator credential must not double as a skeleton key for tenant data."""
        from app.config import settings

        alpha = two_tenants["alpha"]
        response = await client.get(
            f"/api/v1/tenants/{alpha['id']}/flags",
            headers={"X-API-Key": settings.admin_api_key},
        )
        assert response.status_code == 401

    async def test_api_key_is_never_stored_in_plaintext(self, client, tenant_factory):
        """"keys stored hashed, not in plain text"."""
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import ApiKey
        from app.security import hash_api_key

        _, raw_key = await tenant_factory("hash-check")

        async with SessionLocal() as session:
            rows = (await session.execute(select(ApiKey))).scalars().all()
            stored = [row.key_hash for row in rows]

        assert raw_key not in stored
        assert hash_api_key(raw_key) in stored
        assert all(len(h) == 64 for h in stored)


class TestEnvironmentScoping:
    async def test_enabling_production_leaves_other_environments_dark(self, client, two_tenants):
        alpha = two_tenants["alpha"]
        await _create_flag(client, alpha["id"], alpha["key"])
        await client.put(
            f"/api/v1/tenants/{alpha['id']}/flags/checkout.new_flow",
            json={"environment": "production", "enabled": True, "rollout_percentage": 100},
            headers={"X-API-Key": alpha["key"]},
        )

        for environment, expected in (
            ("production", True),
            ("staging", False),
            ("development", False),
        ):
            response = await client.post(
                "/api/v1/evaluate",
                json={
                    "tenant_id": alpha["id"],
                    "environment": environment,
                    "user_id": "user_1",
                    "context": {},
                },
                headers={"X-API-Key": alpha["key"]},
            )
            assert response.json()["flags"]["checkout.new_flow"] is expected, environment

    async def test_new_flag_is_dark_in_every_environment(self, client, two_tenants):
        """A new flag must never be live in production the moment it is created."""
        alpha = two_tenants["alpha"]
        await _create_flag(client, alpha["id"], alpha["key"])

        response = await client.get(
            f"/api/v1/tenants/{alpha['id']}/flags", headers={"X-API-Key": alpha["key"]}
        )
        configs = response.json()[0]["configs"]
        assert len(configs) == 3
        assert all(c["enabled"] is False for c in configs)

    async def test_environment_filter_narrows_configs(self, client, two_tenants):
        alpha = two_tenants["alpha"]
        await _create_flag(client, alpha["id"], alpha["key"])

        response = await client.get(
            f"/api/v1/tenants/{alpha['id']}/flags?environment=production",
            headers={"X-API-Key": alpha["key"]},
        )
        configs = response.json()[0]["configs"]
        assert [c["environment"] for c in configs] == ["production"]

    async def test_cache_invalidation_is_environment_scoped(self, client, two_tenants):
        """Changing production must not serve staging a stale set — or a wrong one.

        Both environments are warmed first so that a broken invalidation shows up as a
        stale read rather than an incidental cache miss.
        """
        alpha = two_tenants["alpha"]
        await _create_flag(client, alpha["id"], alpha["key"])

        async def evaluate(environment: str):
            response = await client.post(
                "/api/v1/evaluate",
                json={
                    "tenant_id": alpha["id"],
                    "environment": environment,
                    "user_id": "user_1",
                    "context": {},
                },
                headers={"X-API-Key": alpha["key"]},
            )
            return response.json()["flags"]["checkout.new_flow"]

        assert await evaluate("production") is False
        assert await evaluate("staging") is False

        await client.put(
            f"/api/v1/tenants/{alpha['id']}/flags/checkout.new_flow",
            json={"environment": "production", "enabled": True, "rollout_percentage": 100},
            headers={"X-API-Key": alpha["key"]},
        )

        assert await evaluate("production") is True  # invalidated
        assert await evaluate("staging") is False  # untouched
