/**
 * Memorystore for Redis.
 *
 * The single most expensive line in this deployment: ~$35/month for the 1GB BASIC tier,
 * which is the smallest SKU that exists. There is no free tier and no smaller instance.
 * On a $300 credit that is roughly a third of the budget spent on a cache.
 *
 * It stays because the cache is load-bearing, not decorative. The evaluation hot path
 * reads the compiled flag set per (tenant, environment) from here and touches Postgres
 * only on a miss — which is what lets a db-f1-micro serve the whole service. Dropping
 * Redis to save $35 would mean upgrading Cloud SQL to absorb the read load, and that
 * costs more than $35.
 */

resource "google_redis_instance" "main" {
  name    = "${var.name_prefix}-redis"
  project = var.project_id
  region  = var.region

  # BASIC = single node, no replica, no failover. STANDARD_HA doubles the price for a
  # replica, and this is a cache: losing it costs a cold read from Postgres, not data.
  # The application already treats every Redis failure as a miss and degrades to the
  # database rather than erroring — see app/cache.py. Paying for HA on a component that
  # is designed to be disposable is the wrong trade.
  tier           = var.tier
  memory_size_gb = var.memory_size_gb
  redis_version  = "REDIS_7_0"

  # PRIVATE_SERVICE_ACCESS reuses the same peering as Cloud SQL rather than allocating a
  # second range. DIRECT_PEERING is the older mode and consumes a separate block.
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  authorized_network      = var.network_id
  reserved_ip_range       = var.psa_range_name
  transit_encryption_mode = "DISABLED" # in-VPC only; TLS would add latency to the hot path
  auth_enabled            = false      # unreachable outside the VPC; AUTH adds a secret to rotate for no boundary gained

  redis_configs = {
    # The cache is a pure derivative of Postgres — every key can be rebuilt from a query.
    # allkeys-lru means memory pressure silently evicts cold tenants instead of returning
    # OOM errors on write, which for this workload is exactly right: an evicted tenant
    # costs one database read, whereas a write error would surface as a failed evaluation.
    maxmemory-policy = "allkeys-lru"
  }

  depends_on = [var.psa_connection]
}
