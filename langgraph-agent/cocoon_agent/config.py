"""Service settings, always loaded from langgraph-agent/.env regardless of the caller's cwd."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = SERVICE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    service_token: SecretStr = Field(alias="COCOON_SERVICE_TOKEN")
    host: str = Field(default="127.0.0.1", alias="COCOON_HOST")
    port: int = Field(default=8000, alias="COCOON_PORT")
    data_dir: Path = Field(default=SERVICE_DIR / "data", alias="COCOON_DATA_DIR")
    log_level: str = Field(default="INFO", alias="COCOON_LOG_LEVEL")

    llm_mode: Literal["mock", "live"] = Field(default="mock", alias="COCOON_LLM_MODE")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field(default="claude-opus-5", alias="COCOON_LLM_MODEL")
    llm_effort: Literal["low", "medium", "high"] = Field(default="low", alias="COCOON_LLM_EFFORT")
    llm_fallbacks: Literal["default", "off"] = Field(default="default", alias="COCOON_LLM_FALLBACKS")
    llm_timeout_seconds: float = Field(default=20.0, alias="COCOON_LLM_TIMEOUT_SECONDS")

    turn_timeout_seconds: float = Field(default=45.0, alias="COCOON_TURN_TIMEOUT_SECONDS")
    turn_poll_after_ms: int = Field(default=500, alias="COCOON_TURN_POLL_AFTER_MS")
    announcement_ttl_seconds: int = Field(default=120, alias="COCOON_ANNOUNCEMENT_TTL_SECONDS")
    seatbelt_rule_requires_engine_on: bool = Field(default=True, alias="COCOON_SEATBELT_RULE_REQUIRES_ENGINE_ON")

    @model_validator(mode="after")
    def _live_mode_needs_key(self) -> "Settings":
        if self.llm_mode == "live" and not (self.anthropic_api_key and self.anthropic_api_key.get_secret_value()):
            raise ValueError("COCOON_LLM_MODE=live requires ANTHROPIC_API_KEY; use COCOON_LLM_MODE=mock without it")
        if not self.service_token.get_secret_value().strip():
            raise ValueError("COCOON_SERVICE_TOKEN must not be empty")
        if not self.data_dir.is_absolute():
            self.data_dir = SERVICE_DIR / self.data_dir
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cocoon.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
