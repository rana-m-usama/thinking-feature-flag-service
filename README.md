# Multi-Tenant Configuration & Feature Flag Service

A centralised platform service that lets multiple applications manage feature flags and
runtime configuration — a simplified LaunchDarkly. Teams create flags, roll them out
gradually to a percentage of users, scope them per environment, and get an immutable
audit trail of every change.

Built for the Backend & Platform take-home (Option C). Two deliverables, two folders.

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


