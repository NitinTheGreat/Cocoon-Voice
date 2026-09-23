"""Insights settings, read from the same langgraph-agent/.env as the core backend."""

from __future__ import annotations

from functools import lru_cache

from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import ENV_FILE


class InsightsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    enabled: bool = Field(default=True, alias="INSIGHTS_ENABLED")
    # Either one URL (postgresql://user:password@host:port/db) or the discrete POSTGRES_* values below.
    database_url: SecretStr | None = Field(default=None, alias="INSIGHTS_DATABASE_URL")
    host: str = Field(default="127.0.0.1", alias="POSTGRES_HOST")
    port: int = Field(default=5432, alias="POSTGRES_PORT", ge=1, le=65535)
    database: str = Field(default="cocoon_insights", alias="POSTGRES_DB", min_length=1)
    user: str = Field(default="postgres", alias="POSTGRES_USER", min_length=1)
    password: SecretStr | None = Field(default=None, alias="POSTGRES_PASSWORD")
    sslmode: str = Field(default="prefer", alias="POSTGRES_SSLMODE")
    pool_max_size: int = Field(default=5, alias="INSIGHTS_POOL_MAX_SIZE", ge=1, le=50)
    # true: the employee ID must be a catalog operator ID (OP_DEMO_1_1 ... OP_DEMO_5_3), which is what voice
    # sessions carry, so history links automatically. false: any ID matching [A-Za-z0-9_.-]{2,64} is accepted,
    # but voice sessions only link to it if the app sends that same ID as the participant's operator_id.
    require_catalog_employee: bool = Field(default=True, alias="INSIGHTS_REQUIRE_CATALOG_EMPLOYEE")

    def conninfo(self, database: str | None = None) -> str:
        """libpq connection string. Never log it: it can contain the password."""
        if self.database_url is not None and database is None:
            return self.database_url.get_secret_value()
        return make_conninfo(
            host=self.host, port=self.port, dbname=database or self.database, user=self.user,
            password=self.password.get_secret_value() if self.password else None, sslmode=self.sslmode,
            application_name="cocoon-insights", connect_timeout=5,
        )

    def describe(self) -> str:
        """Non-secret target for logs."""
        if self.database_url is not None:
            return "INSIGHTS_DATABASE_URL"
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


@lru_cache
def get_insights_settings() -> InsightsSettings:
    return InsightsSettings()
