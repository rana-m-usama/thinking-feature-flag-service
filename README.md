# Multi-Tenant Configuration & Feature Flag Service

A centralised platform service that lets multiple applications manage feature flags and
runtime configuration — a simplified LaunchDarkly. Teams create flags, roll them out
gradually to a percentage of users, scope them per environment, and get an immutable
audit trail of every change.

Built for the Backend & Platform take-home (Option C). Two deliverables, two folders.

---

## Live on GCP

| | |
|---|---|
| **Deployed application** | **https://flagsvc-production-wqwagzrz3q-el.a.run.app** |
| **Interactive API docs** | **https://flagsvc-production-wqwagzrz3q-el.a.run.app/docs** |
| Readiness — proves the private path to Cloud SQL and Memorystore | [`/readyz`](https://flagsvc-production-wqwagzrz3q-el.a.run.app/readyz) |
| Metrics | [`/metrics`](https://flagsvc-production-wqwagzrz3q-el.a.run.app/metrics) |

```console
$ curl https://flagsvc-production-wqwagzrz3q-el.a.run.app/readyz
{"status":"ready","checks":{"database":"ok","cache":"ok"}}
```

Region `asia-south1`, project `thinking-flagsvc-0e6b`. Deployed entirely by GitHub Actions
with keyless auth — build → migrate → canary at 10% → 100%, rolling back automatically on
failure. Nothing was deployed by hand.

<details>
<summary><b>Candidate URL</b> — for watching a deploy, not for testing against</summary>

**https://candidate---flagsvc-production-wqwagzrz3q-el.a.run.app/docs**

CI deploys every revision with `--tag=candidate` and no traffic, which gives it a URL of its
own before a single user reaches it. That is what the smoke test probes.

**It is a deploy-time artifact, not a stable address.** It points at whatever the most recent
CI run built — which is not necessarily what production is serving. Right now both URLs
happen to resolve to the same revision, so both work. During a canary they do not: the
candidate URL serves the *new* code while production is still 90% on the old one, so a
reviewer hitting it could see a version that isn't live.

Use the stable URL above for anything real.
</details>

---

## What this repository contains

| Folder | What it is | Read it for… |
|---|---|---|
| **[thinking-feature-flag-service/](thinking-feature-flag-service/)** | The service. FastAPI + PostgreSQL + Redis. Data model, evaluation engine, API, tests, Docker. | the database schema, the rollout algorithm, API docs, running it locally |
| **[thinking-terraform/](thinking-terraform/)** | The infrastructure. Terraform for GCP — Cloud Run, Cloud SQL, Memorystore, VPC, IAM, Secret Manager, monitoring. | the GCP architecture, service choices, deployment strategy, cost |

**Each folder has its own README with the full detail.** This one stays at the level of
*what the product is and why it is shaped this way*. Follow the links for anything
concrete — setup steps, schema, endpoints, and Terraform layout all live in the
sub-READMEs, not here.

---

## The problem, in one paragraph

An organisation runs many applications. Each needs to ship features behind flags, turn
them on for 10% of users, verify, then ramp to 100% — and turn them off instantly when
something breaks. Doing that per-application means every team reinvents rollout logic and
nobody can answer "who turned this on?". Centralising it means one service on the critical
path of every request in every app, which sets the engineering constraints: **it must be
fast, it must be multi-tenant-safe, and it must never lie about who changed what.**



## Architecture at a glance

```
   SDKs / applications
          │  X-API-Key
          ▼
   ┌───────────────┐
   │   Cloud Run   │  FastAPI, autoscaled, stateless
   │  (asia-south1)│  evaluation = pure function of (request, cached flag set)
   └──────┬────────┘
          │ private VPC egress — no public IP on either datastore
   ┌──────┴────────────────────┐
   │  Memorystore (Redis)      │  compiled flag set per (tenant, environment)
   │  Cloud SQL (PostgreSQL)   │  source of truth; reached only on a cache miss
   └───────────────────────────┘
```



## Reading order

1. **[thinking-feature-flag-service/README.md](thinking-feature-flag-service/README.md)**
   — the data model and the evaluation algorithm. Start here; the infrastructure only
   makes sense once you know what it runs.
2. **[thinking-terraform/README.md](thinking-terraform/README.md)** — the GCP
   architecture, why each service was chosen, deployment and rollback, and cost.


