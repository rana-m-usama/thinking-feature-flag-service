/**
 * Global External Application Load Balancer in front of Cloud Run.
 *
 * The counterpart to your ALB + target group, with one structural difference: there is
 * no target group and no health check. A Serverless NEG points at the Cloud Run service
 * by name, and Cloud Run manages instance health itself — adding a backend health check
 * here is an error, not an omission.
 *
 * WHY IT EXISTS AT ALL. Cloud Run already gives you a *.run.app URL with valid managed
 * TLS, free. This earns its ~$18/month by:
 *
 *   - Locking the run.app URL down. The service module sets ingress to
 *     INTERNAL_LOAD_BALANCER, making this the only way in.
 *   - Cloud Armor at the edge: rejects floods before they reach a billable container.
 *     Our per-tenant limiter is app-level and only runs after Cloud Run has spun up an
 *     instance and authenticated the caller.
 *   - A custom domain.
 *
 * ALL of which needs a domain you own — managed certs validate by DNS against a real
 * zone. With no domain an LB can serve only plain HTTP, which is strictly worse than the
 * free TLS on run.app. Hence enable_load_balancer defaults to false.
 */

resource "google_compute_region_network_endpoint_group" "cloudrun" {
  name                  = "${var.name_prefix}-neg"
  project               = var.project_id
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = var.cloud_run_service_name
  }
}

resource "google_compute_backend_service" "main" {
  name    = "${var.name_prefix}-backend"
  project = var.project_id

  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"

  backend {
    group = google_compute_region_network_endpoint_group.cloudrun.id
  }

  security_policy = var.enable_cloud_armor ? google_compute_security_policy.main[0].id : null

  log_config {
    enable = true
    # 100%: this is the only record of requests rejected at the edge. Those never reach
    # the app, so they never appear in our structured logs.
    sample_rate = 1.0
  }
}

# --- Cloud Armor -------------------------------------------------------------------
#
# Complements the app's per-tenant limiter rather than duplicating it. The app limits by
# API key AFTER a container has started and authenticated. This limits by IP BEFORE any
# of that — which is what protects against traffic carrying no valid key at all, a flood
# the app-level limiter would cheerfully autoscale to absorb and bill you for.
resource "google_compute_security_policy" "main" {
  count   = var.enable_cloud_armor ? 1 : 0
  name    = "${var.name_prefix}-armor"
  project = var.project_id

  rule {
    action      = "rate_based_ban"
    priority    = 1000
    description = "Edge flood guard per source IP. Deliberately far looser than the per-tenant app quota."
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = var.armor_rate_limit_per_minute
        interval_sec = 60
      }
      ban_duration_sec = 300
    }
  }

  rule {
    action      = "allow"
    priority    = 2147483647
    description = "Required terminal default-allow rule."
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

# --- TLS ---------------------------------------------------------------------------
resource "google_compute_managed_ssl_certificate" "main" {
  name    = "${var.name_prefix}-cert"
  project = var.project_id

  managed {
    domains = [var.domain]
  }

  # Google validates by resolving the domain to the IP below. The cert sits in
  # PROVISIONING until that A record exists and propagates — routinely 15-60 minutes, and
  # it stays FAILED_NOT_VISIBLE indefinitely if the record never appears. A DNS problem
  # wearing a Terraform costume.
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_url_map" "main" {
  name            = "${var.name_prefix}-urlmap"
  project         = var.project_id
  default_service = google_compute_backend_service.main.id
}

resource "google_compute_target_https_proxy" "main" {
  name             = "${var.name_prefix}-https-proxy"
  project          = var.project_id
  url_map          = google_compute_url_map.main.id
  ssl_certificates = [google_compute_managed_ssl_certificate.main.id]
}

resource "google_compute_global_address" "main" {
  name    = "${var.name_prefix}-lb-ip"
  project = var.project_id
}

resource "google_compute_global_forwarding_rule" "https" {
  name                  = "${var.name_prefix}-https"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.main.id
  port_range            = "443"
  target                = google_compute_target_https_proxy.main.id
}

# Port 80 exists only to redirect. Closed, http:// requests time out rather than upgrade,
# which users read as "the site is down".
resource "google_compute_url_map" "redirect" {
  name    = "${var.name_prefix}-redirect"
  project = var.project_id

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  name    = "${var.name_prefix}-http-proxy"
  project = var.project_id
  url_map = google_compute_url_map.redirect.id
}

resource "google_compute_global_forwarding_rule" "http" {
  name                  = "${var.name_prefix}-http"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.main.id
  port_range            = "80"
  target                = google_compute_target_http_proxy.redirect.id
}
