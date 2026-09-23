"""Service settings, always loaded from langgraph-agent/.env regardless of the caller's cwd."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = SERVICE_DIR / ".env"

# SHA-256 of the exact bytes of Cocoon_Dataset_v1/data/generated/manifest.json as observed locally on 2026-09-24.
# PROVISIONAL local development snapshot: checksum-validated, NOT reviewed by the data owner. See docs/MIGRATIONS.md.
PINNED_DEV_MANIFEST_SHA256 = "5d7de31c1856daf4179110a653d175891a102a53263356843792dd383f40e42d"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    service_token: SecretStr = Field(alias="COCOON_SERVICE_TOKEN")
    host: str = Field(default="127.0.0.1", alias="COCOON_HOST")
    port: int = Field(default=8000, alias="COCOON_PORT")
    data_dir: Path = Field(default=SERVICE_DIR / "data", alias="COCOON_DATA_DIR")
    log_level: str = Field(default="INFO", alias="COCOON_LOG_LEVEL")

    llm_mode: Literal["mock", "live"] = Field(default="mock", alias="COCOON_LLM_MODE")
    # Live provider. Vertex AI (Gemini) is the selected provider; "anthropic" is kept only as an explicit legacy option.
    llm_provider: Literal["vertex", "anthropic"] = Field(default="vertex", alias="COCOON_LLM_PROVIDER")
    google_cloud_project: str | None = Field(default=None, alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str = Field(default="global", alias="GOOGLE_CLOUD_LOCATION", min_length=1)
    vertex_model: str = Field(default="gemini-3.8-flash", alias="VERTEX_MODEL", min_length=3)
    # gemini-3.8-flash rejects "minimal" (checked 2026-09-24); "low" is the lowest level it accepts.
    vertex_thinking_level: Literal["low", "medium", "high", "model_default"] = Field(
        default="low", alias="VERTEX_THINKING_LEVEL")
    vertex_max_output_tokens: int = Field(default=1024, alias="VERTEX_MAX_OUTPUT_TOKENS", ge=64, le=8192)
    # Optional path to Application Default Credentials. Only passed to the Google SDK through the standard
    # GOOGLE_APPLICATION_CREDENTIALS variable; this service never opens, parses or logs the file.
    google_application_credentials: Path | None = Field(default=None, alias="GOOGLE_APPLICATION_CREDENTIALS")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field(default="claude-opus-5", alias="COCOON_LLM_MODEL")
    llm_effort: Literal["low", "medium", "high"] = Field(default="low", alias="COCOON_LLM_EFFORT")
    llm_fallbacks: Literal["default", "off"] = Field(default="default", alias="COCOON_LLM_FALLBACKS")
    llm_timeout_seconds: float = Field(default=20.0, alias="COCOON_LLM_TIMEOUT_SECONDS")

    turn_timeout_seconds: float = Field(default=45.0, alias="COCOON_TURN_TIMEOUT_SECONDS")
    turn_poll_after_ms: int = Field(default=500, alias="COCOON_TURN_POLL_AFTER_MS")
    announcement_ttl_seconds: int = Field(default=120, alias="COCOON_ANNOUNCEMENT_TTL_SECONDS")
    seatbelt_rule_requires_engine_on: bool = Field(default=True, alias="COCOON_SEATBELT_RULE_REQUIRES_ENGINE_ON")

    dataset_root: Path = Field(default=Path("../Cocoon_Dataset_v1"), alias="DATASET_ROOT")
    dataset_manifest_sha256: str = Field(default=PINNED_DEV_MANIFEST_SHA256, alias="DATASET_MANIFEST_SHA256",
                                         pattern=r"^[0-9a-f]{64}$")
    session_bindings_path: Path | None = Field(default=None, alias="SESSION_BINDINGS_PATH")

    @model_validator(mode="after")
    def _live_mode_needs_key(self) -> "Settings":
        if self.llm_mode == "live" and self.llm_provider == "vertex" and not (self.google_cloud_project or "").strip():
            raise ValueError("COCOON_LLM_MODE=live with Vertex requires GOOGLE_CLOUD_PROJECT; "
                             "use COCOON_LLM_MODE=mock without it")
        if self.llm_mode == "live" and self.llm_provider == "anthropic" and not (
            self.anthropic_api_key and self.anthropic_api_key.get_secret_value()
        ):
            raise ValueError("COCOON_LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY; use COCOON_LLM_MODE=mock "
                             "without it")
        if not self.service_token.get_secret_value().strip():
            raise ValueError("COCOON_SERVICE_TOKEN must not be empty")
        # Relative paths resolve against langgraph-agent/, never the caller's working directory.
        if not self.data_dir.is_absolute():
            self.data_dir = SERVICE_DIR / self.data_dir
        if not self.dataset_root.is_absolute():
            self.dataset_root = (SERVICE_DIR / self.dataset_root).resolve()
        if self.session_bindings_path is not None and not self.session_bindings_path.is_absolute():
            self.session_bindings_path = (SERVICE_DIR / self.session_bindings_path).resolve()
        if self.google_application_credentials is not None and not self.google_application_credentials.is_absolute():
            self.google_application_credentials = (SERVICE_DIR / self.google_application_credentials).resolve()
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
