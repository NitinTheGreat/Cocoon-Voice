"""Voice worker settings, always loaded from livekit-voice/.env regardless of the caller's cwd.

load_dotenv() also exports LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET into
os.environ, which is where the LiveKit SDK (and LiveKit Inference) reads them.
Job processes are spawned and re-import this module, so they see the same values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = SERVICE_DIR / ".env"

load_dotenv(ENV_FILE, override=False)


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # LiveKit Cloud (read by the SDK from the environment; declared here for validation/docs)
    livekit_url: str | None = Field(default=None, alias="LIVEKIT_URL")
    livekit_api_key: str | None = Field(default=None, alias="LIVEKIT_API_KEY")
    livekit_api_secret: SecretStr | None = Field(default=None, alias="LIVEKIT_API_SECRET")

    agent_name: str = Field(default="cocoon-voice", alias="COCOON_AGENT_NAME")
    health_host: str = Field(default="127.0.0.1", alias="COCOON_HEALTH_HOST")
    health_port: int = Field(default=8081, alias="COCOON_HEALTH_PORT")

    # Backend (langgraph-agent or the mock backend)
    backend_url: str = Field(default="http://127.0.0.1:8000", alias="COCOON_BACKEND_URL")
    service_token: SecretStr = Field(alias="COCOON_SERVICE_TOKEN")
    backend_request_timeout: float = Field(default=10.0, alias="COCOON_BACKEND_REQUEST_TIMEOUT")
    backend_connect_timeout: float = Field(default=3.0, alias="COCOON_BACKEND_CONNECT_TIMEOUT")
    backend_max_attempts: int = Field(default=4, alias="COCOON_BACKEND_MAX_ATTEMPTS")
    turn_deadline: float = Field(default=30.0, alias="COCOON_TURN_DEADLINE_SECONDS")

    # Speech (LiveKit Inference model strings; verified against livekit-agents 1.8.2 and the current quickstart)
    stt_model: str = Field(default="assemblyai/universal-3-5-pro", alias="COCOON_STT_MODEL")
    stt_language: str = Field(default="en", alias="COCOON_STT_LANGUAGE")
    tts_model: str = Field(default="fishaudio/s2.1-pro", alias="COCOON_TTS_MODEL")
    tts_voice: str = Field(default="fa4c9eb3dccc4806b382b40d61c6b10a", alias="COCOON_TTS_VOICE")

    greeting: str = Field(
        default="Hi, I'm Cocoon. Ask me for your next task, to log an incident, or about training.",
        alias="COCOON_GREETING",
    )
    default_machine_id: str = Field(default="cat-320-demo", alias="COCOON_DEFAULT_MACHINE_ID")

    # Proactive announcements
    event_poll_interval: float = Field(default=1.0, alias="COCOON_EVENT_POLL_INTERVAL")
    event_poll_max_backoff: float = Field(default=30.0, alias="COCOON_EVENT_POLL_MAX_BACKOFF")
    announcement_quiet_wait: float = Field(default=4.0, alias="COCOON_ANNOUNCEMENT_QUIET_WAIT_SECONDS")
    announcement_playout_timeout: float = Field(default=30.0, alias="COCOON_ANNOUNCEMENT_PLAYOUT_TIMEOUT")

    def missing_livekit(self) -> list[str]:
        return [name for name, value in (
            ("LIVEKIT_URL", self.livekit_url), ("LIVEKIT_API_KEY", self.livekit_api_key),
            ("LIVEKIT_API_SECRET", self.livekit_api_secret),
        ) if not value]


@lru_cache
def get_settings() -> VoiceSettings:
    return VoiceSettings()  # type: ignore[call-arg]
