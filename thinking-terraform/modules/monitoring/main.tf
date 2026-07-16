/**
 * Observability — log-based metrics, alerts, uptime check, dashboard.
 *
 * These are derived from the structured logs the application already emits, not from a
 * metrics client library. That is a deliberate choice, and the reason is in app/metrics.py:
 * Cloud Run runs N instances and scales to zero, so an in-process counter is per-instance
 * and per-lifetime. It is honest for one container and wrong in aggregate — the p99 you
 * want is across the fleet, not inside one process that may have started 40 seconds ago.
 *
 * The logs already carry `duration_ms`, `tenant_id`, `status_code` and `path` on every
 * request (see app/middleware.py). Cloud Logging can aggregate those across every
 * instance for free, with no extra dependency and no cardinality risk from a library we
 * forgot to configure. The trade is that a log-based metric cannot be finer-grained than
 * what we log — which is why those field names are load-bearing and renaming one silently
 * breaks a dashboard.
 */

# --- Notification --------------------------------------------------------------------
resource "google_monitoring_notification_channel" "email" {
  display_name = "${var.name_prefix} alerts"
  project      = var.project_id
  type         = "email"

  labels = {
    email_address = var.alert_email
  }
}

# --- Log-based metrics ---------------------------------------------------------------

# Evaluation latency distribution. Feeds p50/p95/p99 — the spec asks for all three, and
# a distribution metric gives every percentile from one series rather than three counters.
resource "google_logging_metric" "evaluation_latency" {
  name    = "${var.name_prefix}-evaluation-latency"
  project = var.project_id

  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
    jsonPayload.message=~"^flags_evaluated"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "ms"

    # tenant_id as a label is bounded by customer count, which is fine. The endpoint is
    # deliberately absent — app/middleware.py already normalises paths to route templates
    # for exactly this reason, but a log-based metric label on a raw path would reintroduce
    # unbounded cardinality through the back door.
    labels {
      key         = "tenant_id"
      value_type  = "STRING"
      description = "Tenant UUID"
    }
  }

  value_extractor = "EXTRACT(jsonPayload.duration_ms)"

  label_extractors = {
    "tenant_id" = "EXTRACT(jsonPayload.tenant_id)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 32
      growth_factor      = 1.5
      # 1ms floor: cache-hit evaluations land around 0.5ms, so a coarser scale would put
      # the entire happy path in one bucket and make the p50 meaningless.
      scale = 1
    }
  }
}

# Request errors, by tenant and route template.
resource "google_logging_metric" "request_errors" {
  name    = "${var.name_prefix}-request-errors"
  project = var.project_id

  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
    jsonPayload.message="request_completed"
    jsonPayload.status_code>=500
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"

    labels {
      key        = "tenant_id"
      value_type = "STRING"
    }
    labels {
      key         = "endpoint"
      value_type  = "STRING"
      description = "Route template, never a resolved path"
    }
  }

  label_extractors = {
    "tenant_id" = "EXTRACT(jsonPayload.tenant_id)"
    "endpoint"  = "EXTRACT(jsonPayload.path)"
  }
}

# Total requests — the denominator. An error *rate* needs both, and counting errors alone
# would make a quiet service with two failures look identical to a busy one with two.
resource "google_logging_metric" "request_total" {
  name    = "${var.name_prefix}-request-total"
  project = var.project_id

  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
    jsonPayload.message="request_completed"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"

    labels {
      key        = "tenant_id"
      value_type = "STRING"
    }
    labels {
      key        = "endpoint"
      value_type = "STRING"
    }
  }

  label_extractors = {
    "tenant_id" = "EXTRACT(jsonPayload.tenant_id)"
    "endpoint"  = "EXTRACT(jsonPayload.path)"
  }
}

# --- Alert policies ------------------------------------------------------------------

# "Error rate spikes (>5% over 5-minute window)".
#
# NOTE ON THE DEFINITION: this counts 5xx only, not 4xx. A tenant sending a bad API key
# produces 401s all day and that is the service working correctly — alerting on it would
# page someone about someone else's bug. The spec says "error rate" without saying whose
# errors; this reads it as ours. That distinction belongs in the README, because an
# on-call engineer needs to know which definition the pager uses.
resource "google_monitoring_alert_policy" "error_rate" {
  display_name = "${var.name_prefix} — 5xx error rate above 5%"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "5xx ratio > 5% over 5m"

    condition_monitoring_query_language {
      duration = "300s"
      query    = <<-EOT
        fetch cloud_run_revision
        | { errors:
              metric 'logging.googleapis.com/user/${google_logging_metric.request_errors.name}'
              | align delta(5m)
              | every 1m
              | group_by [], [value_errors: sum(value.request_errors)]
          ; total:
              metric 'logging.googleapis.com/user/${google_logging_metric.request_total.name}'
              | align delta(5m)
              | every 1m
              | group_by [], [value_total: sum(value.request_total)]
          }
        | join
        | value [ratio: val(0) / val(1)]
        | condition ratio > 0.05
      EOT
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = <<-EOT
      More than 5% of requests returned 5xx over 5 minutes.

      This counts server errors only. Client errors (401 bad key, 403 cross-tenant, 422
      validation) are excluded on purpose — those are the service working.

      First checks:
      1. Did a deploy just land? `gcloud run revisions list --service=${var.service_name}`
         If a canary is in progress, roll back first and diagnose after:
         `gcloud run services update-traffic ${var.service_name} --to-revisions=PREVIOUS=100`
      2. Is it one tenant or all of them? The metric is labelled by tenant_id. A single
         tenant suggests their payload; every tenant suggests us.
      3. Is Postgres reachable? /readyz reports it. Redis being down is NOT an outage —
         the app degrades to the database by design.
    EOT
    mime_type = "text/markdown"
  }
}

# "Evaluation latency exceeding threshold".
resource "google_monitoring_alert_policy" "evaluation_latency" {
  display_name = "${var.name_prefix} — evaluation p95 latency high"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "p95 evaluation latency > ${var.latency_threshold_ms}ms for 5m"

    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.evaluation_latency.name}\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = var.latency_threshold_ms

      aggregations {
        alignment_period = "60s"
        # ALIGN_PERCENTILE_95 across the fleet — the number an in-process counter cannot
        # produce, because no single instance sees the whole distribution.
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = <<-EOT
      Evaluation p95 exceeded ${var.latency_threshold_ms}ms for 5 minutes.

      Evaluation on a cache hit is a Redis read plus a SHA-256 — sub-millisecond. Elevated
      p95 almost always means cache misses, so check in this order:

      1. Memorystore reachable? A miss falls back to Postgres, which is ~4x slower and
         still correct — so this alert fires while the service is technically fine.
      2. Cache hit ratio on the dashboard. A sustained drop means invalidation is running
         hot: some client is writing flags in a loop and clearing the key each time.
      3. Cold starts — if min_instances is 0, a scale-from-zero shows here. Expected after
         idle, not a fault.
    EOT
    mime_type = "text/markdown"
  }
}

# "Service health check failures".
resource "google_monitoring_uptime_check_config" "health" {
  display_name = "${var.name_prefix} health"
  project      = var.project_id
  timeout      = "10s"
  period       = "300s"

  http_check {
    path         = "/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.uptime_check_host
    }
  }
}

resource "google_monitoring_alert_policy" "uptime" {
  display_name = "${var.name_prefix} — health check failing"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Uptime check failing from multiple regions"

    condition_threshold {
      filter          = "resource.type = \"uptime_url\" AND metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.labels.check_id = \"${google_monitoring_uptime_check_config.health.uptime_check_id}\""
      duration        = "300s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
      # Google probes from several regions. Requiring more than one to fail avoids paging
      # on a single probe's network blip, which is the most common false positive here.
      trigger {
        count = 2
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    content   = "/healthz is failing from multiple probe regions. This endpoint checks only that the process is alive — if it is down, the container is not running, not merely degraded. Check Cloud Run revision status and container logs for a startup crash."
    mime_type = "text/markdown"
  }
}

# --- Dashboard -----------------------------------------------------------------------
resource "google_monitoring_dashboard" "main" {
  project = var.project_id

  dashboard_json = jsonencode({
    displayName = "${var.name_prefix} — feature flag service"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          width = 6, height = 4, xPos = 0, yPos = 0
          widget = {
            title = "Evaluation latency (p50 / p95 / p99)"
            xyChart = {
              dataSets = [
                for pct in ["50", "95", "99"] : {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"cloud_run_revision\" metric.type=\"logging.googleapis.com/user/${google_logging_metric.evaluation_latency.name}\""
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_PERCENTILE_${pct}"
                        crossSeriesReducer = "REDUCE_MEAN"
                      }
                    }
                  }
                  plotType       = "LINE"
                  legendTemplate = "p${pct}"
                }
              ]
              yAxis = { label = "ms", scale = "LINEAR" }
            }
          }
        },
        {
          width = 6, height = 4, xPos = 6, yPos = 0
          widget = {
            title = "Evaluations per second by tenant"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_revision\" metric.type=\"logging.googleapis.com/user/${google_logging_metric.request_total.name}\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.tenant_id"]
                    }
                  }
                }
                plotType = "STACKED_AREA"
              }]
            }
          }
        },
        {
          width = 6, height = 4, xPos = 0, yPos = 4
          widget = {
            title = "5xx errors by tenant and endpoint"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_revision\" metric.type=\"logging.googleapis.com/user/${google_logging_metric.request_errors.name}\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.tenant_id", "metric.label.endpoint"]
                    }
                  }
                }
                plotType = "LINE"
              }]
            }
          }
        },
        {
          width = 6, height = 4, xPos = 6, yPos = 4
          widget = {
            title = "Cloud Run instance count"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_revision\" resource.label.service_name=\"${var.service_name}\" metric.type=\"run.googleapis.com/container/instance_count\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_MEAN"
                      crossSeriesReducer = "REDUCE_SUM"
                    }
                  }
                }
                plotType = "STACKED_AREA"
              }]
            }
          }
        },
        {
          width = 6, height = 4, xPos = 0, yPos = 8
          widget = {
            title = "Cloud SQL CPU — the shared core is the ceiling"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloudsql_database\" metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\""
                    aggregation = {
                      alignmentPeriod  = "60s"
                      perSeriesAligner = "ALIGN_MEAN"
                    }
                  }
                }
                plotType = "LINE"
              }]
            }
          }
        },
        {
          width = 6, height = 4, xPos = 6, yPos = 8
          widget = {
            title = "Memorystore hit ratio"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"redis_instance\" metric.type=\"redis.googleapis.com/stats/cache_hit_ratio\""
                    aggregation = {
                      alignmentPeriod  = "60s"
                      perSeriesAligner = "ALIGN_MEAN"
                    }
                  }
                }
                plotType = "LINE"
              }]
            }
          }
        },
      ]
    }
  })
}
