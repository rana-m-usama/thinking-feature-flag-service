"""Custom metrics.

The spec names four: evaluation latency (p50/p95/p99), evaluations per second by
tenant, error rates by tenant and endpoint, and cache hit/miss ratio. All four are
collected here and exposed on `GET /metrics`.

Deliberately in-process rather than a Cloud Monitoring client library, for one reason:
Cloud Run scales to zero and runs many instances, so an in-process counter is per
instance and per lifetime. It is honest for a single instance and wrong in aggregate.
The two production paths, documented in the README:

1. Log-based metrics — the structured logs already carry `duration_ms`, `tenant_id`
   and `status_code`, so Cloud Monitoring can derive every one of these with no code
   and no extra dependency. This is what the Terraform provisions.
2. An OpenTelemetry sidecar exporting to Cloud Monitoring, which is where this goes if
   the metric cardinality ever justifies the operational cost.

This endpoint stays useful regardless: it is what the load test reads, and it makes
the metrics inspectable locally without a GCP project.
"""

import time
from collections import defaultdict, deque
from threading import Lock

from app import cache

# Bounded so memory cannot grow without limit on a long-lived instance. 10k samples is
# ~2 minutes at 100 rps, which is the window the p99 is meaningful over anyway.
_MAX_SAMPLES = 10_000

_lock = Lock()
_latencies: deque[float] = deque(maxlen=_MAX_SAMPLES)
_evaluations: deque[tuple[float, str]] = deque(maxlen=_MAX_SAMPLES)  # (timestamp, tenant_id)
_flag_evaluations = 0
_errors: dict[tuple[str, str], int] = defaultdict(int)  # (tenant_id, endpoint) -> count
_requests: dict[tuple[str, str], int] = defaultdict(int)

_started_at = time.time()


def record_evaluation(tenant_id: str, endpoint: str, duration_ms: float, flag_count: int) -> None:
    global _flag_evaluations
    with _lock:
        _latencies.append(duration_ms)
        _evaluations.append((time.time(), tenant_id))
        _flag_evaluations += flag_count


def record_request(tenant_id: str, endpoint: str, status_code: int) -> None:
    with _lock:
        _requests[(tenant_id, endpoint)] += 1
        if status_code >= 500:
            _errors[(tenant_id, endpoint)] += 1


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Exact for the sample, no interpolation — with 10k
    samples the difference from a linear-interpolation percentile is noise."""
    if not sorted_values:
        return 0.0
    index = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return round(sorted_values[index], 3)


def snapshot() -> dict:
    with _lock:
        latencies = sorted(_latencies)
        evaluations = list(_evaluations)
        errors = dict(_errors)
        requests = dict(_requests)
        flag_evaluations = _flag_evaluations

    now = time.time()
    # Rate over a trailing 60s window rather than since-boot: a since-boot average
    # hides the spike you are trying to see.
    recent = [(ts, tenant) for ts, tenant in evaluations if now - ts <= 60]
    per_tenant: dict[str, int] = defaultdict(int)
    for _, tenant in recent:
        per_tenant[tenant] += 1

    error_rates = {}
    for key, total in requests.items():
        tenant_id, endpoint = key
        failed = errors.get(key, 0)
        error_rates[f"{tenant_id}|{endpoint}"] = {
            "requests": total,
            "errors": failed,
            "rate": round(failed / total, 4) if total else 0.0,
        }

    return {
        "uptime_seconds": round(now - _started_at, 1),
        "evaluation_latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "samples": len(latencies),
        },
        "evaluations_per_second": {
            "total": round(len(recent) / 60, 2),
            "by_tenant": {t: round(c / 60, 2) for t, c in per_tenant.items()},
        },
        "flag_evaluations_total": flag_evaluations,
        "error_rates": error_rates,
        "cache": cache.cache_stats(),
    }
