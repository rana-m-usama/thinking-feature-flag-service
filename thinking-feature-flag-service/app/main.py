"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import cache, metrics
from app.config import settings
from app.db import SessionLocal, engine
from app.logging_config import configure_logging
from app.middleware import CorrelationMiddleware, RateLimitMiddleware
from app.routers import evaluate, flags, tenants

logger = logging.getLogger(__name__)

DESCRIPTION = """
Multi-tenant configuration and feature flag service.

### Authentication

Every endpoint takes an `X-API-Key` header.

* **Tenant endpoints** use the key returned when the tenant was registered. The key
  determines the tenant — a key can only ever reach its own tenant's data, and a
  `tenant_id` in a URL or body that disagrees with the key is rejected.
* **`POST /api/v1/tenants`** uses the operator key from `ADMIN_API_KEY` instead. A tenant
  cannot authenticate its own creation: at that moment neither it nor its key exists yet.

Keys are stored as SHA-256 hashes and shown exactly once, at registration.

### Evaluation and determinism

`user_id` is opaque and is never stored. Rollout membership is
`sha256(flag_key + ":" + user_id)[:4] % 100 < rollout_percentage` — a pure function, so
the same user always resolves to the same value for a given flag, on every instance,
forever. Raising a rollout percentage only ever adds users to the cohort; nobody loses a
feature mid-session.

`flag_key` is mixed into the hash so that each flag partitions the user base
independently. Without it the same unlucky cohort would land in the first 10% of every
rollout and every experiment would confound every other.

Precedence: **archived → disabled → targeting rules → rollout → on value.** Targeting
beats the rollout, so a pinned QA user is not left testing a coin flip. The kill switch
beats targeting, so turning a flag off during an incident is never second-guessed by a
rule someone wrote last month.
"""

TAGS_METADATA = [
    {"name": "tenants", "description": "Register applications and mint their API keys."},
    {
        "name": "flags",
        "description": "Flag CRUD, per-environment configuration, and audit history.",
    },
    {"name": "evaluation", "description": "The hot path. Resolve flag values for a user context."},
    {"name": "operations", "description": "Health probes and metrics."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("service_starting", extra={"app_env": settings.app_env})
    yield
    await cache.close_client()
    await engine.dispose()
    logger.info("service_stopped")


app = FastAPI(
    title="Feature Flag Service",
    description=DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=TAGS_METADATA,
    # Interactive docs stay on in production on purpose: this is an internal platform
    # service, and its consumers are the engineers who need them.
    docs_url="/docs",
    redoc_url="/redoc",
)

# Order matters: rate limiting runs inside the correlation context, so a 429 still logs
# with a request_id and still counts toward the metrics.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(CorrelationMiddleware)

app.include_router(tenants.router)
app.include_router(flags.router)
app.include_router(evaluate.router)


# NOT /healthz. That path is intercepted by Google's frontend on Cloud Run and never
# reaches the container — a request to /healthz returns Google's own HTML 404, while a
# request to /healthzz (which this app does not define) reaches FastAPI and returns its
# JSON 404. The route is fine; the path is unusable on this platform.
#
# Cost: a canary that probed /healthz could never pass, so every deploy rolled back to a
# placeholder while the candidate was provably healthy on /readyz. Locally /healthz works
# perfectly, which is exactly what made it hard to see.
@app.get("/livez", tags=["operations"], summary="Liveness probe")
async def livez() -> dict[str, str]:
    """Is the process alive?

    Checks nothing else, deliberately. If this probed Postgres, a database blip would
    fail liveness, Cloud Run would kill every container, and the restarts would achieve
    nothing except turning a degraded database into a total outage. Liveness answers
    "would restarting me help?" — dependency health is `/readyz`'s job.
    """
    return {"status": "ok"}


@app.get("/readyz", tags=["operations"], summary="Readiness probe")
async def readyz() -> JSONResponse:
    """Can this instance serve traffic?

    Probes Postgres, because an instance that cannot reach it cannot serve a cache miss
    and should leave the load balancer rotation until it can.

    Redis is checked but not fatal: the cache is a latency optimisation and the service
    degrades to Postgres without it. Failing readiness on a Redis outage would pull every
    instance out of rotation and turn a slow service into a dead one.
    """
    checks: dict[str, str] = {}
    healthy = True

    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        healthy = False

    try:
        client = await cache.get_client()
        await client.ping()
        checks["cache"] = "ok"
    except Exception as exc:
        checks["cache"] = f"degraded: {exc}"

    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if healthy else "not_ready", "checks": checks},
    )


@app.get("/metrics", tags=["operations"], summary="Service metrics")
async def get_metrics() -> dict:
    """Evaluation latency percentiles, evaluation rate by tenant, error rates, cache ratio.

    In-process and therefore per-instance and per-lifetime — see the note in metrics.py.
    Production aggregation is via log-based metrics derived from the structured logs.
    """
    return metrics.snapshot()
