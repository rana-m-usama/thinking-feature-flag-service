"""Redis cache for compiled flag sets.

Caching strategy — the load-bearing decision:

We cache the **compiled flag set per (tenant, environment)**, not per-user evaluation
results. The spec says "caching evaluated flag results", and the obvious reading is to
key on the user. That is the wrong layer:

* Hit rate. Keying on user_id means cardinality of users x flags. A tenant with a
  million users generates a million keys per environment, most read once. The working
  set never converges and Memorystore fills with garbage.
* It caches the cheap half. Evaluation is a SHA-256 and a few comparisons —
  microseconds, no I/O. The expensive part is the Postgres round trip for the flag set.
  Caching the output caches the microseconds and keeps the milliseconds.
* Invalidation. Toggling one flag would have to invalidate every user's key for that
  tenant — a SCAN over a million keys, or a version-prefix scheme. Keying on
  (tenant, environment) makes it a single DEL.

So: one Redis key per (tenant, environment) holding every active flag's config, and
the pure engine evaluates against it in-process. One cache entry serves every user of
that tenant, invalidation is O(1), and the hot path makes zero database calls.
"""

import json
import logging
from typing import Any

import redis.asyncio as redis

from app.config import settings
from app.evaluation import FlagSnapshot
from app.models import Environment

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None

# Observability: "Cache hit/miss ratio" is a required metric. Counted here at the only
# place that knows, and exported by the metrics module.
hits = 0
misses = 0


async def get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(str(settings.redis_url), decode_responses=True)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _key(tenant_id: Any, environment: Environment) -> str:
    return f"flags:{tenant_id}:{environment.value}"


async def get_flag_set(tenant_id: Any, environment: Environment) -> list[FlagSnapshot] | None:
    """Return the cached flag set, or None on a miss.

    Every failure mode here returns None rather than raising. Redis is a latency
    optimisation, not a source of truth — if Memorystore is unreachable the service
    must degrade to Postgres and stay up, not start failing evaluations.
    """
    global hits, misses
    if not settings.cache_enabled:
        return None
    try:
        client = await get_client()
        raw = await client.get(_key(tenant_id, environment))
    except Exception:
        logger.warning("cache_read_failed", exc_info=True, extra={"tenant_id": str(tenant_id)})
        return None

    if raw is None:
        misses += 1
        return None

    hits += 1
    try:
        return [FlagSnapshot(**item) for item in json.loads(raw)]
    except (json.JSONDecodeError, TypeError) as exc:
        # A poisoned entry (shape change across a deploy) must not be fatal.
        logger.warning("cache_decode_failed", extra={"error": str(exc)})
        misses += 1
        return None


async def set_flag_set(
    tenant_id: Any, environment: Environment, snapshots: list[FlagSnapshot]
) -> None:
    if not settings.cache_enabled:
        return
    payload = json.dumps([s.__dict__ for s in snapshots])
    try:
        client = await get_client()
        await client.set(_key(tenant_id, environment), payload, ex=settings.cache_ttl_seconds)
    except Exception:
        logger.warning("cache_write_failed", exc_info=True, extra={"tenant_id": str(tenant_id)})


async def invalidate(tenant_id: Any, environment: Environment | None = None) -> None:
    """Drop cached sets after a write.

    A flag-level change (rename, archive, default_value) affects every environment, so
    `environment=None` clears all three. An environment-scoped change clears one.

    Invalidation is a DEL rather than a rewrite: a rewrite would race with concurrent
    readers repopulating from a snapshot they read before the write committed.
    """
    if not settings.cache_enabled:
        return
    environments = [environment] if environment else list(Environment)
    try:
        client = await get_client()
        await client.delete(*[_key(tenant_id, e) for e in environments])
    except Exception:
        # Worst case the stale entry survives until TTL. Log loudly — this is the
        # window where a killed flag stays live, and cache_ttl_seconds bounds it.
        logger.error("cache_invalidate_failed", exc_info=True, extra={"tenant_id": str(tenant_id)})


def cache_stats() -> dict[str, int | float]:
    total = hits + misses
    return {"hits": hits, "misses": misses, "ratio": round(hits / total, 4) if total else 0.0}
