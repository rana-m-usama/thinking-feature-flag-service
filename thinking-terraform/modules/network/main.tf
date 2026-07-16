/**
 * Network — VPC, subnet, and the Private Service Access peering that Cloud SQL and
 * Memorystore need for private IPs.
 *
 * Two things that differ from the AWS mental model and cost people days:
 *
 * 1. There are no public/private subnets. GCP has no per-subnet route table split — a
 *    resource is public only if it has an external IP. "Private" here means every
 *    datastore below is created with no external IP at all, so there is nothing to
 *    firewall off from the internet in the first place.
 *
 * 2. GCP VPCs are GLOBAL and subnets are REGIONAL. One subnet covers every zone in the
 *    region, so the per-AZ subnet fan-out from AWS has no counterpart here.
 */

resource "google_compute_network" "main" {
  name    = "${var.name_prefix}-vpc"
  project = var.project_id

  # Auto mode would create a subnet in every region with fixed ranges we do not control.
  auto_create_subnetworks = false

  # Regional routing keeps the blast radius of a route change inside one region.
  routing_mode = "REGIONAL"
}

resource "google_compute_subnetwork" "main" {
  name    = "${var.name_prefix}-subnet"
  project = var.project_id
  region  = var.region
  network = google_compute_network.main.id

  ip_cidr_range = var.subnet_cidr

  # Required for Cloud Run direct VPC egress: the connection is proxied via Google's
  # infrastructure and Private Google Access is what lets it reach the subnet at all.
  private_ip_google_access = true

  # Your AWS setup has flow-logs.tf, so keeping parity. Sampled at 50% with a 10-minute
  # aggregation because flow logs bill by volume and full-fidelity logs on a service this
  # size would cost more than the service.
  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# --- Private Service Access --------------------------------------------------------
#
# Cloud SQL and Memorystore private IPs do not live in the subnet above. Google runs
# them in their own VPC and peers it into ours; we only hand over a CIDR to allocate
# from. This is the single biggest departure from RDS-in-your-own-subnet.
resource "google_compute_global_address" "psa_range" {
  name          = "${var.name_prefix}-psa-range"
  project       = var.project_id
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.psa_prefix_length
  network       = google_compute_network.main.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa_range.name]

  # THE destroy footgun. Without this, `terraform destroy` tears the peering down while
  # Cloud SQL still holds an IP inside it, the delete hangs for ~20 minutes and then
  # fails, and you are left hand-unpicking a half-destroyed network. This tells the
  # provider to remove Google's side of the peering first.
  #
  # Even with it, ordering matters: destroy the database and cache before the network.
  # The Makefile's `destroy` target does exactly that.
  deletion_policy = "ABANDON"
}

# --- Firewall ----------------------------------------------------------------------
#
# Deliberately minimal, and worth explaining rather than leaving as a suspicious absence.
#
# GCP firewall rules attach to the NETWORK and target by tag or service account — there
# is no security-group-referencing-a-security-group idiom to port over. More to the
# point: Cloud SQL and Memorystore here have no external IP and sit behind the PSA
# peering, so the only thing that can reach them is something already inside this VPC.
# Cloud Run's egress is the only thing inside this VPC. There is no lateral surface to
# segment.
#
# The one rule worth having is a deny-all for anything that ever gets an external IP by
# accident. GCP's implied rules already deny ingress by default, so this is belt and
# braces with logging attached.
resource "google_compute_firewall" "deny_all_ingress" {
  name        = "${var.name_prefix}-deny-all-ingress"
  project     = var.project_id
  network     = google_compute_network.main.id
  description = "Explicit deny + logging. GCP implicitly denies ingress; this makes it visible."

  direction = "INGRESS"
  priority  = 65534

  deny {
    protocol = "all"
  }

  source_ranges = ["0.0.0.0/0"]

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}
