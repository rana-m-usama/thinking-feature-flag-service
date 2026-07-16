"""Application configuration.

Every value is sourced from the environment. On GCP these are injected by Cloud Run
from Secret Manager; locally they come from .env. Nothing is read from a file at
runtime and nothing is hardcoded, so the Secret Manager migration is a deployment
concern rather than a code change.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime -------------------------------------------------------------
    app_env: Literal["local", "development", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    port: int = 8080

    # Only used to build the `logging.googleapis.com/trace` field, which Cloud Logging
    # needs fully qualified to join a log line to its trace. Empty locally.
    gcp_project_id: str = ""

    # --- Datastores ----------------------------------------------------------
    # Cloud SQL via the Cloud Run connector uses a unix socket host:
    #   postgresql+asyncpg://user:pass@/dbname?host=/cloudsql/PROJECT:REGION:INSTANCE
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://flagsvc:flagsvc@localhost:5433/flagsvc"
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    redis_url: RedisDsn = Field(default="redis://localhost:6380/0")

    # --- Auth ----------------------------------------------------------------
    # Bootstrap credential for POST /api/v1/tenants. Tenant creation cannot
    # authenticate with a tenant key because the key does not exist yet, so this
    # endpoint needs a separate operator credential. See README "Assumptions".
    admin_api_key: str = Field(default="dev-admin-key-change-me", min_length=8)

    # --- Cache ---------------------------------------------------------------
    # Applies to the compiled per-(tenant, environment) flag set, not to
    # per-user evaluation results. See README "Caching strategy".
    cache_ttl_seconds: int = 300
    cache_enabled: bool = True

    # --- Rate limiting -------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 1000

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
