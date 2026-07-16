# Infrastructure — GCP / Terraform

Terraform for the feature flag service: Cloud Run, Cloud SQL, Memorystore, VPC, IAM,
Secret Manager, Workload Identity Federation, and monitoring.

**The service lives in [../thinking-feature-flag-service/](../thinking-feature-flag-service/).**
Product overview in the [root README](../README.md).

---

## Architecture

```
                        Internet
                           │
                           │  (optional: Global External ALB + Cloud Armor —
                           │   only with a domain, see "Load balancer")
                    ┌──────▼────────┐
                    │   Cloud Run   │  flagsvc-production
                    │───────────────│  min=1, max=10, asia-south1
                    │ rev-abc   90% │◄── traffic split = the deploy strategy
                    │ rev-def   10% │◄── canary
                    └──────┬────────┘
                           │ Direct VPC egress, PRIVATE_RANGES_ONLY
                    ┌──────▼──────────────────────────────┐
                    │  VPC (global) / subnet (regional)   │
                    │                                     │
                    │   ┌──────────────┐  ┌────────────┐  │
                    │   │  Cloud SQL   │  │Memorystore │  │  private IP only —
                    │   │ PostgreSQL 16│  │  Redis 7   │  │  no public IP anywhere
                    │   └──────────────┘  └────────────┘  │
                    │        ▲ Private Service Access     │
                    │        │ 10.18.0.0/16 peering       │
                    └─────────────────────────────────────┘
                           ▲
                    ┌──────┴────────┐
                    │ Cloud Run Job │  alembic upgrade head
                    │   (migrate)   │  must succeed before traffic shifts
                    └───────────────┘

  Secret Manager    → DATABASE_URL, ADMIN_API_KEY  (mounted as env vars)
  Artifact Registry → images tagged by git SHA
  GitHub Actions    → Workload Identity Federation (no service account keys)
```

**Project** `thinking-flagsvc-0e6b` · **region** `asia-south1` · **state**
`gs://thinking-flagsvc-0e6b-tfstate`

---

## Service choices

| Choice | Instead of | Why |
|---|---|---|
| **Cloud Run** | GKE | GKE Autopilot is ~$75/mo for a control plane before running anything. Cloud Run's revision traffic splitting *is* the canary requirement, built in |
| **Direct VPC egress** | Serverless VPC connector | The connector is e2-micro instances you pay for (~$30/mo) and patch. Direct egress is GA, needs no instances, costs nothing |
| **`PRIVATE_RANGES_ONLY`** | `ALL_TRAFFIC` | Avoids Cloud NAT entirely — ~$32/mo saved, for no benefit lost |
| **Cloud SQL** | AlloyDB | AlloyDB starts ~$250/mo with no shared-core tier. The hot path is served from Redis; Postgres handles flag writes and audit inserts |
| **Private IP only** | Public IP + firewall | Datastores aren't *firewalled off* from the internet — they're **absent from it**. Which is why the firewall module is nearly empty |
| **Canary at the revision level** | LB traffic shifting | Native, and rollback is one command against a revision that never stopped existing |

**Cloud Run splits traffic per request, not sticky** — the same user can hit old then new
revision. That's fine here for a specific reason: evaluation is a pure function of
`(flag_key, user_id)`, so both revisions compute bucket 67 for `user_2c91` and return the
same answer. The determinism that makes rollouts work makes non-sticky canary safe.

---



## Layout

```
thinking-terraform/
├── Makefile                    the safety rail — always use this
├── bootstrap/                  state bucket + APIs + budget alert (LOCAL state, run once)
├── live/                       the single root module
│   ├── backend.production.hcl  ┐
│   ├── backend.staging.hcl     │ per-environment config
│   ├── terraform.production.tfvars
│   ├── terraform.staging.tfvars┘
│   └── main.tf providers.tf variables.tf outputs.tf versions.tf
└── modules/
    ├── network/       VPC, subnet, flow logs, PSA range + peering
    ├── database/      Cloud SQL, database, user, password → Secret Manager
    ├── cache/         Memorystore
    ├── service/       Cloud Run service + migrate job + least-privilege SAs
    ├── loadbalancer/  Serverless NEG, backend, managed cert, Cloud Armor (conditional)
    ├── secrets/       admin API key
    ├── monitoring/    log-based metrics, alerts, uptime check, dashboard
    └── github-oidc/   WIF pool, deployer SA, Artifact Registry
```

**Why `live/` and not a directory per environment:** one root, N `backend.<env>.hcl` +
`terraform.<env>.tfvars`. DRY, with one sharp edge — nothing in Terraform stops you
initialising with staging's backend and applying with production's tfvars. You'd plan
production resources into staging's state, and the first sign would be a plan proposing to
destroy everything. The **Makefile** derives both filenames from one `ENV`, so they cannot
disagree. Don't run `terraform` in `live/` by hand.

---

## Usage

```bash
make bootstrap                 # once, by hand — creates the state bucket (local state)

make init  ENV=production
make plan  ENV=production      # review this
make apply ENV=production

make url       ENV=production  # deployed URL
make admin-key ENV=production  # reads the bootstrap key from Secret Manager
make lint                      # fmt -check + validate; no credentials needed
```

`init` passes `-reconfigure` deliberately: without it, switching environments makes
Terraform **migrate** state from the old backend to the new one — copying staging's state
into production's bucket, behind a prompt that looks routine.

`destroy` takes datastores out first, explicitly. The PSA peering can't be torn down while
Cloud SQL or Memorystore hold IPs from it; Terraform gets that ordering wrong often enough
that the delete hangs ~20 minutes then fails, leaving a half-destroyed network.

---

## The two decisions that matter most

### 1. Terraform owns the *shape*; CI owns the *contents*

```hcl
lifecycle {
  ignore_changes = [template[0].containers[0].image, traffic]
}
```

Without this, **every `terraform apply` silently reverts whatever CI last deployed** — an
unplanned rollback triggered by an unrelated infra change. Mid-canary, an apply would slam
100% of traffic onto the candidate, completing a rollout nobody approved.

It's also why `image` defaults to `gcr.io/cloudrun/hello`: Cloud Run can't be created
without an image, and Artifact Registry is empty until CI runs once.

### 2. Migrations are a Cloud Run Job, never a container entrypoint

`alembic upgrade head` at container start is what compose does locally, and it's a race the
moment Cloud Run scales past one instance. The job uses the same image with a different
entrypoint and runs **inside the VPC** — which is also why CI can't run alembic from the
GitHub runner: there's no public path to the database, by design.

**Consequence:** during a canary, old and new revisions run against the **same schema**, so
**migrations must be backward-compatible**. Expand/contract — add a column, deploy,
backfill, drop later. Never rename in one step.

---

## Deployment & rollback

```
build → push :$GIT_SHA → run migrate job → deploy revision --no-traffic --tag=candidate
  → smoke test https://candidate---flagsvc-production-xxx.run.app/readyz
  → shift 10% → watch error rate 2 min → shift 100%
```

```bash
# Rollback — seconds, no rebuild, because the old revision never stopped existing
gcloud run services update-traffic flagsvc-production --to-revisions=PREVIOUS=100
```

> **CI/CD is not written yet.** The infrastructure it needs is live — WIF pool, deployer
> SA, Artifact Registry — but the GitHub Actions workflow is outstanding.

---

## Security

**Workload Identity Federation, no service account keys.** A JSON key is the most commonly
leaked GCP credential — it doesn't expire, works from anywhere, and is one `echo $KEY` in a
debug step from a public build log.

The attribute condition is the **entire trust boundary**:

```hcl
attribute_condition = "assertion.repository == 'rana-m-usama/thinking-feature-flag-service'"
```

Pinned to the full repo, not just the owner — `repository_owner == 'x'` still trusts every
repo in the org, including one a compromised account creates.

| Control | Detail |
|---|---|
| **Two service accounts** | Serving app reads rows; migrate job runs DDL. Sharing one means the internet-facing container can drop tables |
| **Secret access per secret** | Project-level `secretAccessor` would grant every secret the project ever holds |
| **`actAs` scoped** to two SAs | Project-level `serviceAccountUser` grants actAs on *every* account including the deployer — a privilege-escalation path |
| **State bucket = Secret Manager** | `random_password` writes plaintext to state. Hence versioning, UBLA, `public_access_prevention`, `prevent_destroy` |

---

## Observability

Metrics derive from the **structured logs**, not a metrics client library. Cloud Run runs N
instances and scales to zero, so an in-process counter is per-instance and per-lifetime —
honest for one container, wrong in aggregate. The logs already carry `duration_ms`,
`tenant_id`, `status_code` and `path` on every request, so Cloud Logging aggregates them
across the fleet for free.

**Those field names are load-bearing — renaming one silently breaks a dashboard.**

| Alert | Condition |
|---|---|
| Error rate | 5xx ratio > 5% over 5 min (MQL ratio of two log-based metrics) |
| Evaluation latency | p95 > 500ms for 5 min |
| Health check | uptime check failing from ≥2 probe regions |

Every alert carries a `documentation` block with the first three debugging steps — an alert
that fires without telling you what to check is a pager, not a signal.

**Error rate counts 5xx only.** A tenant sending a bad key produces 401s all day and that's
the service working correctly. `latency_threshold_ms = 500` is deliberately loose (~1000×
a cache-hit evaluation) — an alert that fires on normal cold starts gets muted within a
week, and a muted alert is worse than none.

---

## Cost

| Resource | Config | ~$/month |
|---|---|---|
| Cloud SQL | `db-f1-micro`, **ENTERPRISE**, ZONAL, 10GB | ~9 |
| Memorystore | BASIC 1GB | **~35** |
| Cloud Run | min=1, max=10 | ~5 |
| Registry / secrets / monitoring | | ~3 |
| Load balancer | *not deployed* (no domain) | 0 |
| **Total** | | **~$52–63** |

**Memorystore is the floor** — 1GB BASIC is the smallest SKU that exists. No free tier, no
smaller instance. A third of the budget on a cache. It stays because dropping it means
upgrading Cloud SQL to absorb the read load, which costs more than $35.

**Staging is written but not applied.** `terraform.staging.tfvars`, `backend.staging.hcl`
and its own state prefix all exist, and `make plan ENV=staging` works. Applying costs
~$44/mo; both environments over the 90-day credit window exceeds $300. Environment
separation is real in the code; the spend is a documented decision.

A **budget alert** is live at 50/90/100% of $300 plus a forecast rule (which lands days
earlier — the only one with time to act on). It does **not** cap spend; GCP has no hard
stop. Smoke detector, not sprinkler.

**`min_instances = 1` costs ~$5** and buys away a ~2s cold start. A flag service is on the
critical path of every request in every app that consumes it — a cold start here is a
latency spike on someone else's checkout page, and they can't tell it was us.

### The connection math

```
db-f1-micro allows ~25 connections.
Pool is PER CONTAINER:
  max_instances × (db_pool_size + db_max_overflow) = 10 × (2 + 1) = 30
```

30 > 25, deliberately. Reaching it needs all 10 instances saturated *and* each holding full
overflow — for an async app whose hot path never touches the database, that's a genuine
incident, not normal traffic. The fix is `db-g1-small` (~$25/mo, 50 connections), **not**
lowering the pool, which just moves the failure to connection starvation inside each
container.

---

## Load balancer — and why it's off

`domain = ""`, so the module compiles out entirely.

**Google-managed SSL certificates validate by DNS against a domain you own.** With no
domain, a load balancer serves only plain HTTP — *strictly worse* than the free, valid
managed TLS already on the `*.run.app` URL. You'd pay ~$18/mo to make security worse.

Set `domain` and you get an LB, Cloud Armor edge rate limiting (rejecting floods *before*
they reach a billable container, complementing the app's per-tenant limiter which runs
*after* authentication), a managed cert, and ingress locked to
`INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`.

That last line is the most important in the module. Without it the `run.app` URL stays on
the public internet **regardless of the load balancer**, and the LB — with its WAF and TLS
— becomes decoration attackers route around.

---

## Gotchas hit during the real apply

| Symptom | Cause | Fix |
|---|---|---|
| `Error code 16: Request had invalid authentication credentials` on `servicenetworking` | ADC user credentials carry no **quota project**; the API has nothing to bill the call against. Reads as a broken login — it isn't | `user_project_override = true` + `billing_project` in the provider. Without it, PSA and every private IP are uncreatable |
| `reserved env names were provided: PORT` | Cloud Run **injects `PORT`**; setting it is a hard 400. It also fails *fast*, and Terraform stops scheduling after an error — which is why Cloud SQL never started on that apply | Don't set it. The container reads it |
| `Invalid Tier (db-f1-micro) for (ENTERPRISE_PLUS) Edition` | Cloud SQL **defaults to ENTERPRISE_PLUS**, which demands `db-perf-optimized-*` — ~**$300+/mo, the whole credit in a month**. Only caught because it failed loudly; a compatible tier would have silently created a 30× instance | **Never leave `edition` implicit.** Pin `ENTERPRISE` |
| `Couldn't find free blocks in allocated IP ranges` | A `/24` PSA range cannot hold two services. Memorystore takes a `/29`, but **Cloud SQL carves out a whole `/24`**. ~248 addresses free and still no room — the constraint is *subnet blocks*, not addresses | `/16`, Google's recommendation. Not a round-up — a requirement |
| `Secret .../versions/latest was not found` | The migrate job referenced the secret's *id*, so Terraform thought the dependency was met once the empty secret existed. Cloud Run resolves `versions/latest` **at create time**, and that version can't be written until Cloud SQL has a private IP — a minutes-wide race | `depends_on = [module.database, module.secrets]` |

A Makefile bug of my own also hid a real error: `apply` wrapped Terraform in
`|| (echo "No saved plan?"; exit 1)`, so any mid-apply API failure was reported as a
missing plan file. A fallback message must never describe a failure it didn't diagnose.

---

## Outstanding

- **CI/CD workflow.** Infrastructure is live; the GitHub Actions YAML isn't written.
- **Cloud SQL finish + first deploy.** Provisioning at time of writing; the app image has
  never been pushed, so there is no deployed URL yet.
- **Load test.** Not run.
- **`prevent_destroy` on the production database.** `deletion_protection = true` is on; the
  Terraform-side guard stays off until the URL is handed out.
