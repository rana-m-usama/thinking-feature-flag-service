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

## The  decisions that matter most

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



