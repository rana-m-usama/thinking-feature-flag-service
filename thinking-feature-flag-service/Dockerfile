# syntax=docker/dockerfile:1.7

# ---- Builder ----------------------------------------------------------------
# Dependencies are resolved and compiled here and only the resulting venv is copied
# forward, so the runtime image carries no compilers, no build headers and no package
# index cache. That is most of the difference between a ~1GB image and a ~200MB one,
# and every megabyte is cold-start latency on Cloud Run.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is needed to compile asyncpg if no wheel matches the platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only the dependency manifest first. This layer is cached and rebuilt only when
# dependencies actually change — editing application code does not reinstall the world.
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
    && python - <<'PY' > /tmp/requirements.txt
import tomllib, pathlib
data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
print("\n".join(data["project"]["dependencies"]))
PY
RUN pip install -r /tmp/requirements.txt

# ---- Runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

# curl is here only for HEALTHCHECK. It is a real trade-off: it adds a binary and a
# small attack surface to the runtime image. The alternative is a Python one-liner
# probe, which costs an interpreter start per check. Cloud Run uses its own HTTP probes
# and ignores HEALTHCHECK entirely, so this exists for docker-compose and any plain
# `docker run`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root. A container escape starting from uid 1000 with no shell and no write access
# to the application directory is a materially worse day for an attacker than root.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app/ ./app/
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app alembic.ini pyproject.toml ./

USER app

EXPOSE 8080

# Liveness only — see the docstring on /healthz. Probing the database here would make a
# database blip restart every container, which helps nobody.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/healthz" || exit 1

# Single worker per container on purpose. Cloud Run scales by adding instances, so a
# process manager inside the container just fights the platform's own autoscaler for
# the same CPU and makes per-instance metrics meaningless.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
