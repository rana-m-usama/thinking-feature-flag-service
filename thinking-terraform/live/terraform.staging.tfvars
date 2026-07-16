# Staging — WRITTEN BUT NOT APPLIED.
#
# Environment separation is real in this repo: separate state (backend.staging.hcl),
# separate resources (name_prefix), separate config (this file). What is deliberately not
# real is the spend. Applying this costs ~$44/month, and production + staging over the
# 90-day credit window would be ~$320 — over the $300 grant.
#
# So this file exists, is valid, and `make plan ENV=staging` works. It has simply never
# been applied. That is a documented budget decision, not an omission.
#
#   make apply ENV=staging   # if you want it, this is all it takes

project_id  = "thinking-flagsvc-0e6b"
environment = "staging"
region      = "asia-south1"

# Staging must not collide with production's range if they ever peer.
subnet_cidr = "10.20.0.0/24"

db_tier              = "db-f1-micro"
db_availability_type = "ZONAL"
db_disk_size_gb      = 10
# False so a mistake in staging can be cleaned up with `make destroy` rather than a
# console visit. Production has this on for the opposite reason.
db_deletion_protection    = false
db_point_in_time_recovery = false

redis_tier      = "BASIC"
redis_memory_gb = 1

# Scale to zero. Staging is idle most of the day and a cold start bothers nobody here.
min_instances = 0
max_instances = 4
cpu           = "1"
memory        = "512Mi"
concurrency   = 80

db_pool_size    = 2
db_max_overflow = 1

# Short TTL so a stale cache never masks a bug that would then reach production.
cache_ttl_seconds     = 60
rate_limit_per_minute = 1000
log_level             = "DEBUG"

domain             = ""
enable_cloud_armor = true

github_repository = "rana-m-usama/thinking-feature-flag-service"

alert_email          = "usama.dev100@gmail.com"
latency_threshold_ms = 1000
