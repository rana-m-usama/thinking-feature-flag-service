/**
 * Cloud Run service, the migration job, and their service accounts.
 *
 * Two decisions dominate this file:
 *
 * 1. Terraform owns the SHAPE of the service; CI owns WHAT RUNS IN IT. See the lifecycle
 *    block on the service — without it, `terraform apply` silently reverts whatever CI
 *    last deployed, and your IaC and your CD pipeline fight over production.
 *
 * 2. Migrations run as a Cloud Run JOB, never at container start. Cloud Run scales past
 *    one instance, and N containers racing `alembic upgrade head` against one database
 *    is how you corrupt a schema. CI runs the job and requires it to succeed before any
 *    traffic shifts.
 */

# --- Service accounts --------------------------------------------------------------
#
# Two identities, not one, because they need different powers. The serving app reads
# rows; the migration job runs DDL. Sharing one account would mean the request-handling
# container carries permission to drop tables for the lifetime of every request it
# serves — and that container is the one exposed to the internet.

resource "google_service_account" "run" {
  account_id   = "${var.name_prefix}-run"
  project      = var.project_id
  display_name = "Cloud Run runtime identity for ${var.name_prefix}"
  description  = "Serves traffic. Reads secrets, writes logs and metrics. No DDL."
}

resource "google_service_account" "migrate" {
  account_id   = "${var.name_prefix}-migrate"
  project      = var.project_id
  display_name = "Migration job identity for ${var.name_prefix}"
  description  = "Runs alembic. Separate from the runtime SA so DDL is not reachable from the serving path."
}

# Secret access is granted PER SECRET, not project-wide. roles/secretmanager.secretAccessor
# at the project level would let the service read every secret the project will ever hold,
# including ones added later by people who never considered this service.
resource "google_secret_manager_secret_iam_member" "run_database_url" {
  project   = var.project_id
  secret_id = var.database_url_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

resource "google_secret_manager_secret_iam_member" "run_admin_key" {
  project   = var.project_id
  secret_id = var.admin_api_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

resource "google_secret_manager_secret_iam_member" "migrate_database_url" {
  project   = var.project_id
  secret_id = var.database_url_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrate.email}"
}

# The runtime SA gets exactly three project-level roles and no more. Notably absent:
# roles/cloudsql.client — we connect over the VPC by private IP with a password, not
# through the Cloud SQL connector, so the IAM role buys nothing.
resource "google_project_iam_member" "run" {
  for_each = toset([
    "roles/logging.logWriter",       # structured logs to Cloud Logging
    "roles/monitoring.metricWriter", # custom metrics
    "roles/cloudtrace.agent",        # request traces
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_project_iam_member" "migrate" {
  for_each = toset([
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.migrate.email}"
}

# --- Cloud Run service -------------------------------------------------------------

resource "google_cloud_run_v2_service" "main" {
  name     = var.name_prefix
  project  = var.project_id
  location = var.region

  # THE most important line here.
  #
  # With a load balancer, this must be INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER or the
  # *.run.app URL stays on the public internet and the load balancer — with its Cloud
  # Armor rules, its WAF, its TLS — becomes decoration that attackers route around by
  # calling the run.app URL directly.
  #
  # Without a load balancer, INGRESS_TRAFFIC_ALL is correct and the run.app URL is the
  # front door, protected by Google-managed TLS.
  ingress = var.enable_load_balancer ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL"

  deletion_protection = false

  template {
    service_account = google_service_account.run.email

    # min=0 lets the service scale to zero and cost nothing at rest, at the price of a
    # cold start (~2s here: interpreter boot, then the first evaluation is a cache miss).
    # min=1 keeps one warm. For a flag service on the critical path of every request in
    # every downstream app, a cold start is a latency spike for someone's checkout page.
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    # Reach into the VPC for Cloud SQL and Memorystore private IPs.
    #
    # This is DIRECT VPC EGRESS, not a Serverless VPC Access connector. The connector is
    # what most guides still describe: a set of e2-micro instances you pay for (~$30/mo)
    # and patch. Direct egress has been GA since 2024, needs no instances, and costs
    # nothing beyond the traffic.
    vpc_access {
      network_interfaces {
        network    = var.network_name
        subnetwork = var.subnet_name
      }
      # PRIVATE_RANGES_ONLY: RFC1918 traffic goes through the VPC, everything else takes
      # Cloud Run's default egress. This is what avoids needing Cloud NAT — ALL_TRAFFIC
      # would route internet-bound requests through the VPC, which then needs a NAT
      # gateway at ~$32/month plus data processing, for no benefit.
      egress = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        # CPU is throttled to zero between requests. The app does no background work —
        # no schedulers, no queue consumers — so paying for always-on CPU would buy
        # nothing. Revisit if the SSE endpoint ever ships, since an idle SSE connection
        # with throttled CPU cannot push.
        cpu_idle          = true
        startup_cpu_boost = true
      }

      # --- Configuration: every value from the environment ---------------------
      # Non-secret settings are plain env vars; secrets are Secret Manager references
      # that Cloud Run resolves at start. The application cannot tell the difference —
      # it reads os.environ either way — which is exactly why the local .env and this
      # block can diverge in source without diverging in code.

      env {
        name  = "APP_ENV"
        value = var.app_env
      }
      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "PORT"
        value = "8080"
      }
      env {
        name  = "REDIS_URL"
        value = var.redis_url
      }
      env {
        name  = "CACHE_TTL_SECONDS"
        value = tostring(var.cache_ttl_seconds)
      }
      env {
        name  = "CACHE_ENABLED"
        value = "true"
      }
      env {
        name  = "RATE_LIMIT_ENABLED"
        value = "true"
      }
      env {
        name  = "RATE_LIMIT_PER_MINUTE"
        value = tostring(var.rate_limit_per_minute)
      }
      env {
        name  = "DB_POOL_SIZE"
        value = tostring(var.db_pool_size)
      }
      env {
        name  = "DB_MAX_OVERFLOW"
        value = tostring(var.db_max_overflow)
      }

      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret = var.database_url_secret_id
            # "latest" means a secret rotation takes effect on the next revision, not
            # instantly — a pinned version would need a Terraform apply to roll forward.
            version = "latest"
          }
        }
      }
      env {
        name = "ADMIN_API_KEY"
        value_source {
          secret_key_ref {
            secret  = var.admin_api_key_secret_id
            version = "latest"
          }
        }
      }

      # Liveness. Deliberately /healthz, which checks nothing but the process — see the
      # docstring in app/main.py. Probing the database here would make a Cloud SQL blip
      # restart every container, converting a degraded database into a total outage.
      liveness_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 10
        period_seconds        = 30
        timeout_seconds       = 3
        failure_threshold     = 3
      }

      # Readiness. /readyz probes Postgres, so an instance that cannot serve a cache miss
      # leaves the rotation. Redis being down is reported but not fatal there — failing
      # readiness on a cache outage would pull every instance and turn slow into dead.
      startup_probe {
        http_get {
          path = "/readyz"
        }
        initial_delay_seconds = 5
        period_seconds        = 3
        timeout_seconds       = 3
        # 30 x 3s = 90s. Generous because the first boot may wait on a cold Cloud SQL
        # connection; a tight budget here turns a slow start into a crash loop.
        failure_threshold = 30
      }
    }

    max_instance_request_concurrency = var.concurrency
    timeout                          = "30s"
  }

  # Traffic goes to the latest revision by default. CI overrides this during a canary by
  # calling `gcloud run services update-traffic` directly — which is precisely why
  # `traffic` is in ignore_changes below.
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    ignore_changes = [
      # Terraform must not own the image tag. CI deploys :$GIT_SHA on every merge; if
      # Terraform tracked `image`, the next `terraform apply` would revert production to
      # whatever tag is in tfvars — an unplanned rollback triggered by an unrelated infra
      # change. Terraform owns the shape of the service, CI owns its contents.
      template[0].containers[0].image,

      # Same reasoning. During a canary, CI sets 90/10 across two revisions. Terraform's
      # view says "100% to latest". Without this, an apply mid-canary would slam 100% of
      # traffic onto the candidate — completing a rollout nobody approved.
      client,
      client_version,
      traffic,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.run_database_url,
    google_secret_manager_secret_iam_member.run_admin_key,
  ]
}

# --- Migration job -----------------------------------------------------------------
#
# Same image, different entrypoint. Runs inside the VPC so it can reach the private IP —
# which is also why CI cannot just run alembic from the GitHub runner: there is no public
# path to the database, by design.
resource "google_cloud_run_v2_job" "migrate" {
  name     = "${var.name_prefix}-migrate"
  project  = var.project_id
  location = var.region

  deletion_protection = false

  template {
    template {
      service_account = google_service_account.migrate.email
      # A failed migration should fail the deploy, loudly and once. Retrying a partially
      # applied migration is how a bad schema change becomes an unrecoverable one.
      max_retries = 0
      timeout     = "600s"

      vpc_access {
        network_interfaces {
          network    = var.network_name
          subnetwork = var.subnet_name
        }
        egress = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = var.image
        command = ["alembic"]
        args    = ["upgrade", "head"]

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = var.database_url_secret_id
              version = "latest"
            }
          }
        }
        env {
          name  = "APP_ENV"
          value = var.app_env
        }
        # Settings validation reads these at import even though migrations never use
        # them; a missing required var fails the job before alembic runs.
        env {
          name  = "REDIS_URL"
          value = var.redis_url
        }
        env {
          name  = "ADMIN_API_KEY"
          value = "unused-by-migrations-but-required-by-settings"
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # As above: CI sets the image, Terraform sets the shape.
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.migrate_database_url]
}

# --- Public access -----------------------------------------------------------------
#
# allUsers invoker. The service authenticates its own callers with API keys, so IAM-level
# auth would mean every SDK needed a Google identity — which is not what an internal
# platform service handing out API keys to other teams looks like.
#
# When a load balancer is in front, ingress is already restricted to it, so allUsers here
# means "the load balancer may invoke", not "the internet may".
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.main.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
