# Feature Flag Service

FastAPI service implementing multi-tenant feature flags: tenant registration, flag CRUD,
a deterministic evaluation engine, and an append-only audit trail.

**Infrastructure lives in [../thinking-terraform/](../thinking-terraform/).** Product
overview in the [root README](../README.md).

---

## Quick start

Requires Docker and Python 3.12 (via [uv](https://docs.astral.sh/uv/)).

```bash
cd thinking-feature-flag-service
cp .env.example .env

# Postgres + Redis only. The app runs on the host for a tighter reload loop.
docker compose -f docker-compose.dev.yml up -d

uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Interactive API docs: **http://localhost:8080/docs**

```bash
.venv/bin/python -m pytest -q     # 58 tests
.venv/bin/ruff check app/ tests/
```

To run the real container image instead of host uvicorn:
`docker compose -f docker-compose.dev.yml --profile full up`


---

## Database schema

Five tables. Every column traces to a requirement in the brief; anything speculative was
cut.

```
┌────────────────────────────┐
│         tenants            │
│  id, name, timestamps      │
└──┬──────────────────┬──────┘
   │ 1:N              │ 1:N
   ▼                  ▼
┌────────────────────┐   ┌──────────────────────────────┐
│      api_keys      │   │           flags              │
│ id, tenant_id      │   │ id, tenant_id                │
│ key_hash (sha256)  │   │ key, name, description       │
│ name               │   │ type (boolean|string|number) │
└─────────┬──────────┘   │ default_value  (jsonb)       │
          │              │ archived_at                  │
          │              │ UNIQUE (tenant_id, key)      │
          │              └──┬────────────────────┬──────┘
          │                 │ 1:N                │ 1:N
          │                 ▼                    ▼
          │   ┌──────────────────────────────┐  ┌───────────────────────────┐
          │   │  flag_environment_configs    │  │      flag_audit_log       │
          │   │ flag_id + environment  (PK)  │  │ id, tenant_id, flag_id    │
          │   │ tenant_id                    │  │ environment  (NULL = flag-│
          │   │ enabled          bool        │  │              level change)│
          │   │ value            jsonb       │  │ action                    │
          │   │ rollout_percentage  0..100   │  │ old_value, new_value      │
          │   │ targeting_rules  jsonb       │  │ created_at                │
          │   └──────────────────────────────┘  │ actor_key_id ─────────────┼──┐
          │                                     └───────────────────────────┘  │
          └──────────────────────────────────────────────────────────────────────┘
                                    actor  ("who changed it")
```


### Audit immutability is enforced by the database

```sql
CREATE TRIGGER trg_flag_audit_log_immutable
BEFORE UPDATE OR DELETE ON flag_audit_log
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
```

An ORM that never issues an UPDATE is a convention. A trigger that refuses one is a
guarantee — it also holds against `psql`, a migration, and a future maintainer who hasn't
read the brief. Verified by attempting both from `psql`:

```
ERROR:  flag_audit_log is append-only: UPDATE is not permitted
ERROR:  flag_audit_log is append-only: DELETE is not permitted
```


## API

Full interactive docs at `/docs`. Every endpoint takes `X-API-Key`.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/tenants` | **Uses `ADMIN_API_KEY`**, not a tenant key — see Assumptions |
| POST | `/api/v1/tenants/{id}/flags` | Seeds all three environment configs, dark |
| GET | `/api/v1/tenants/{id}/flags` | `?environment=` and `?status=active\|archived\|all` |
| PUT | `/api/v1/tenants/{id}/flags/{flag_key}` | `environment` required in body for env-scoped fields |
| DELETE | `/api/v1/tenants/{id}/flags/{flag_key}` | Soft-delete (archive) |
| GET | `/api/v1/tenants/{id}/flags/{flag_key}/history` | Chronological, newest first |
| POST | `/api/v1/evaluate` | Optional `flag_keys` to scope |
| POST | `/api/v1/evaluate/bulk` | All active flags, one round trip |
| GET | `/healthz` `/readyz` `/metrics` | Operations |

### Examples

```bash
# Register a tenant — returns the API key ONCE, never recoverable
curl -X POST localhost:8080/api/v1/tenants \
  -H 'X-API-Key: dev-admin-key-change-me' -H 'Content-Type: application/json' \
  -d '{"name":"bizscout-web"}'
# {"id":"83fc...","name":"bizscout-web","api_key":"ffs_Qo0R4ad8J95c..."}

# Create a flag
curl -X POST localhost:8080/api/v1/tenants/$TID/flags \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"key":"checkout.new_flow","name":"New checkout","type":"boolean","default_value":false}'

# Enable in production at 25%
curl -X PUT localhost:8080/api/v1/tenants/$TID/flags/checkout.new_flow \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"environment":"production","enabled":true,"rollout_percentage":25}'

# Evaluate
curl -X POST localhost:8080/api/v1/evaluate \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"tenant_id":"'$TID'","environment":"production","user_id":"qa-bot","context":{}}'
# {"user_id":"qa-bot","environment":"production","flags":{"checkout.new_flow":true}}
```

### Targeting rules

```json
{"attribute": "plan", "operator": "in", "values": ["enterprise"], "value": true}
```

Operators: `in`, `not_in`, `eq`, `neq`, `contains`, `starts_with`, `ends_with`. The
attribute `user_id` resolves to the request's `user_id` rather than a context key. Rules
evaluate in array order, first match wins.

A malformed rule (unknown operator, missing attribute) matches nothing rather than
raising — it falls through to the rollout, which can only ever serve `default_value`. A
bad rule can fail to turn a feature **on**; it can never fail it **on**.





## Testing strategy

**58 tests: 40 unit, 18 integration.** Weighted toward the evaluation engine on purpose —
a bug there serves the wrong feature to real users *silently*, whereas a bug in the CRUD
layer returns a 500 someone notices.

The engine is pure (no DB, no cache, no clock), which is what makes it exhaustively
testable without fixtures.



## Layout

```
app/
├── main.py            FastAPI app, health probes, OpenAPI description
├── config.py          every knob, all from env (pydantic-settings)
├── models.py          SQLAlchemy — the five tables
├── schemas.py         Pydantic request/response = the OpenAPI contract
├── security.py        API key hashing, auth, the tenant-ownership guard
├── evaluation.py      THE ENGINE — pure, no I/O
├── cache.py           Redis; compiled flag sets, invalidation
├── metrics.py         in-process metrics for /metrics
├── middleware.py      correlation IDs, access logs, rate limiting
├── logging_config.py  Cloud Logging JSON
└── routers/           tenants, flags, evaluate
migrations/            Alembic — includes the audit trigger
tests/                 test_evaluation.py (unit), test_isolation.py (integration)
```

## Configuration

See [.env.example](.env.example) for every variable and what it does.