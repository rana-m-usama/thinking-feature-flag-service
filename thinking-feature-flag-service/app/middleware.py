"""Request middleware: correlation IDs, access logging, per-tenant rate limiting."""

import logging
import time
import uuid

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import cache, metrics
from app.config import settings
from app.logging_config import request_id_var, trace_id_var
from app.security import hash_api_key

logger = logging.getLogger(__name__)


def _route_template(request: Request) -> str:
    """Return the matched route pattern, not the resolved URL.

    `/api/v1/tenants/{tenant_id}/flags`, never `/api/v1/tenants/83fc5c19-.../flags`.

    This is the difference between one metric series per endpoint and one per tenant.
    Resolved paths embed UUIDs, so every new tenant mints a new time series forever:
    the Cloud Monitoring bill grows with the customer list, dashboards fragment into
    thousands of near-empty lines, and the "error rate by endpoint" alert never fires
    because no single series ever has enough volume to breach a threshold. Unbounded
    label cardinality is the standard way to make an observability stack useless, and
    it is invisible until you have enough tenants to hurt.

    Falls back to the raw path only for unrouted requests (404s), which are bounded in
    practice by the fact that nothing matched.
    """
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Assign every request an ID and log its completion.

    On Cloud Run, `X-Cloud-Trace-Context` already carries a trace ID that the load
    balancer generated — reusing it means our logs join up with the platform's own
    request logs instead of sitting in a parallel universe. We only mint an ID when
    nothing upstream provided one.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request_id_var.set(request_id)

        # Format: TRACE_ID/SPAN_ID;o=1
        cloud_trace = request.headers.get("x-cloud-trace-context")
        if cloud_trace:
            trace_id_var.set(cloud_trace.split("/")[0])

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request_failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration_ms, 3),
                },
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id

        tenant_id = getattr(request.state, "tenant_id", "anonymous")
        endpoint = _route_template(request)
        metrics.record_request(tenant_id, endpoint, response.status_code)

        # These fields are what the Cloud Monitoring log-based metrics are built from —
        # error rate filters on status_code, latency distributions on duration_ms, both
        # grouped by tenant_id. Renaming any of them silently breaks a dashboard.
        logger.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 3),
                "tenant_id": tenant_id,
            },
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-tenant rate limiting. "to prevent noisy-neighbor problems".

    Fixed-window counter in Redis: INCR a key bucketed to the current minute, EXPIRE it,
    reject above the limit. Two properties matter more than elegance here:

    * It is keyed on the tenant, not the IP. The noisy neighbour we care about is a
      tenant looping evaluations, and their traffic arrives from many IPs.
    * The counter lives in Redis, not in the process. Cloud Run runs N instances, so an
      in-process counter would let a tenant burn N x the limit and the effective limit
      would change every time the service autoscaled.

    Fixed windows allow a 2x burst across a window boundary. A sliding-log window fixes
    that and costs a sorted set per tenant; the trade is documented in the README and
    the burst is harmless at these limits.

    Failure is open, not closed. If Redis is down, rate limiting stops but the service
    keeps serving — an availability incident should not be amplified into an outage by
    the component that exists to protect availability.
    """

    _EXEMPT = frozenset({"/livez", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"})

    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.rate_limit_enabled or request.url.path in self._EXEMPT:
            return await call_next(request)

        api_key = request.headers.get("x-api-key")
        if not api_key:
            return await call_next(request)  # Let the auth dependency produce the 401.

        # Hash rather than the raw key: this value ends up in Redis, and a raw key in a
        # datastore we do not treat as secret is a credential leak waiting for a memory
        # dump or a MONITOR command.
        identity = hash_api_key(api_key)[:16]
        window = int(time.time() // 60)
        key = f"ratelimit:{identity}:{window}"

        try:
            client = await cache.get_client()
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, 120)
        except Exception:
            logger.warning("rate_limit_unavailable", exc_info=True)
            return await call_next(request)

        if count > settings.rate_limit_per_minute:
            logger.warning("rate_limit_exceeded", extra={"count": count})
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded"},
                headers={
                    "retry-after": str(60 - int(time.time() % 60)),
                    "x-ratelimit-limit": str(settings.rate_limit_per_minute),
                },
            )

        response = await call_next(request)
        response.headers["x-ratelimit-limit"] = str(settings.rate_limit_per_minute)
        response.headers["x-ratelimit-remaining"] = str(
            max(0, settings.rate_limit_per_minute - count)
        )
        return response
