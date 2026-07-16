/**
 * live/ — the single root module, configured per environment.
 *
 * Your pattern from the AWS repo: one root, N backend.<env>.hcl + terraform.<env>.tfvars.
 *
 *   terraform init -backend-config=backend.production.hcl -reconfigure
 *   terraform apply -var-file=terraform.production.tfvars
 *
 * The hazard is real: init with staging's backend and apply with production's tfvars and
 * you will plan production resources into staging's state. Nothing in Terraform stops
 * you. That is what the Makefile at the repo root is for — `make apply ENV=production`
 * can only ever pair matching files. Do not run terraform here by hand.
 */

locals {
  # Every resource carries this. Because staging and production may share a project
  # (see the README's cost note), the prefix is the only thing keeping their resources
  # from colliding on name.
  name_prefix = "flagsvc-${var.environment}"

  labels = {
    application = "flagsvc"
    environment = var.environment
    managed_by  = "terraform"
  }
}

module "network" {
  source = "../modules/network"

  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix
  subnet_cidr = var.subnet_cidr
}

module "secrets" {
  source = "../modules/secrets"

  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix
}

module "database" {
  source = "../modules/database"

  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix

  network_id     = module.network.network_id
  psa_connection = module.network.psa_connection

  tier                   = var.db_tier
  availability_type      = var.db_availability_type
  disk_size_gb           = var.db_disk_size_gb
  deletion_protection    = var.db_deletion_protection
  point_in_time_recovery = var.db_point_in_time_recovery
}

module "cache" {
  source = "../modules/cache"

  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix

  network_id     = module.network.network_id
  psa_range_name = module.network.psa_range_name
  psa_connection = module.network.psa_connection

  tier           = var.redis_tier
  memory_size_gb = var.redis_memory_gb
}

module "service" {
  source = "../modules/service"

  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix

  # Set once, then ignored forever — CI owns the tag from the first deploy onward.
  # See the lifecycle block in modules/service/main.tf.
  image     = var.image
  app_env   = var.environment
  log_level = var.log_level

  network_name = module.network.network_name
  subnet_name  = module.network.subnet_name

  redis_url               = module.cache.redis_url
  database_url_secret_id  = module.database.database_url_secret_id
  admin_api_key_secret_id = module.secrets.admin_api_key_secret_id

  min_instances         = var.min_instances
  max_instances         = var.max_instances
  cpu                   = var.cpu
  memory                = var.memory
  concurrency           = var.concurrency
  cache_ttl_seconds     = var.cache_ttl_seconds
  rate_limit_per_minute = var.rate_limit_per_minute
  db_pool_size          = var.db_pool_size
  db_max_overflow       = var.db_max_overflow

  enable_load_balancer = local.enable_load_balancer

  # Depend on the whole database and secrets modules, not just the secret IDs.
  #
  # Cloud Run resolves `secret_key_ref { version = "latest" }` AT CREATE TIME and fails
  # if no version exists yet. The service only references the secret's *id*, so Terraform
  # considered the dependency met as soon as the empty secret existed — and raced ahead
  # to create the job before `google_secret_manager_secret_version.database_url` was
  # written. That version cannot be written until Cloud SQL has a private IP, so the gap
  # is minutes wide, not milliseconds.
  #
  # "Secret ... versions/latest was not found" is what that race looks like.
  depends_on = [module.database, module.secrets]
}

# --- Load balancer (conditional) ----------------------------------------------------
#
# Only meaningful with a domain. Google-managed certs validate by DNS against a zone you
# control; with no domain an LB can serve nothing but plain HTTP, which is strictly worse
# than the free managed TLS already on the run.app URL. So no domain means no LB, and
# ~$18/month saved rather than spent making things worse.
locals {
  enable_load_balancer = var.domain != ""
}

module "loadbalancer" {
  count  = local.enable_load_balancer ? 1 : 0
  source = "../modules/loadbalancer"

  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix

  cloud_run_service_name      = module.service.service_name
  domain                      = var.domain
  enable_cloud_armor          = var.enable_cloud_armor
  armor_rate_limit_per_minute = var.armor_rate_limit_per_minute
}

module "github_oidc" {
  source = "../modules/github-oidc"

  project_id  = var.project_id
  region      = var.region
  name_prefix = local.name_prefix

  github_repository = var.github_repository
  runtime_service_accounts = [
    module.service.runtime_service_account,
    module.service.migrate_service_account,
  ]
}

module "monitoring" {
  source = "../modules/monitoring"

  project_id  = var.project_id
  name_prefix = local.name_prefix

  service_name = module.service.service_name
  alert_email  = var.alert_email

  # The uptime check needs a bare hostname, no scheme. With an LB that is the custom
  # domain; without one it is the run.app host — and the run.app URL arrives as a full
  # https:// URL, so the scheme has to come off.
  uptime_check_host = local.enable_load_balancer ? var.domain : replace(module.service.service_uri, "https://", "")

  latency_threshold_ms = var.latency_threshold_ms
}
