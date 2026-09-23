"""Typed voice-worker settings, always loaded from livekit-voice/.env regardless of the caller's cwd.

load_dotenv() also exports the values into os.environ, which is where the LiveKit SDK
(LIVEKIT_*) and provider plugins (ASSEMBLYAI_API_KEY, CARTESIA_API_KEY) look for them.
Job processes re-import this module, so they see the same values.

Vertex AI uses Google Application Default Credentials discovered by the official Google SDK.
This module never reads, parses or prints the ADC file and never sets GOOGLE_APPLICATION_CREDENTIALS.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = SERVICE_DIR / ".env"

load_dotenv(ENV_FILE, override=False)

log = logging.getLogger("cocoon_voice.config")

AssemblyAIModel = Literal["universal-3-5-pro", "universal-streaming-english", "universal-streaming-multilingual"]
CartesiaModel = Literal["sonic-3", "sonic-2", "sonic-turbo"]

# Phase-0 names that no longer configure anything (LiveKit Inference strings were replaced by direct plugins).
OBSOLETE_SETTINGS = {
    "COCOON_STT_MODEL": "ASSEMBLYAI_MODEL",
    "COCOON_STT_LANGUAGE": "VOICE_LANGUAGE",
    "COCOON_TTS_MODEL": "CARTESIA_MODEL",
    "COCOON_TTS_VOICE": "CARTESIA_VOICE_ID",
    "COCOON_GREETING": "VOICE_GREETING",
    "PORCUPINE_PREROLL_MS": "WAKE_PREROLL_MS",
    "PORCUPINE_WAKE_ONLY_WAIT_MS": "WAKE_ONLY_ACK_WAIT_MS",
}
FORBIDDEN_GOOGLE_KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")


class ConfigError(ValueError):
    """Settings are unusable for the requested purpose. Message lists variable NAMES only."""


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore",
                                      populate_by_name=False)

    # ---------------------------------------------------------------- LiveKit Cloud
    livekit_url: str | None = Field(default=None, alias="LIVEKIT_URL")
    livekit_api_key: str | None = Field(default=None, alias="LIVEKIT_API_KEY")
    livekit_api_secret: SecretStr | None = Field(default=None, alias="LIVEKIT_API_SECRET")
    agent_name: str = Field(default="cocoon-voice", alias="LIVEKIT_AGENT_NAME")
    legacy_agent_name: str | None = Field(default=None, alias="COCOON_AGENT_NAME")  # deprecated alias
    health_host: str = Field(default="127.0.0.1", alias="COCOON_HEALTH_HOST")
    health_port: int = Field(default=8081, alias="COCOON_HEALTH_PORT", ge=1, le=65535)

    # ---------------------------------------------------------------- direct providers
    assemblyai_api_key: SecretStr | None = Field(default=None, alias="ASSEMBLYAI_API_KEY")
    cartesia_api_key: SecretStr | None = Field(default=None, alias="CARTESIA_API_KEY")
    assemblyai_model: AssemblyAIModel = Field(default="universal-3-5-pro", alias="ASSEMBLYAI_MODEL")
    assemblyai_min_turn_silence_ms: int = Field(default=160, alias="ASSEMBLYAI_MIN_TURN_SILENCE_MS", ge=0, le=2000)
    assemblyai_max_turn_silence_ms: int = Field(default=1400, alias="ASSEMBLYAI_MAX_TURN_SILENCE_MS", ge=200,
                                                le=5000)
    assemblyai_keyterms: str = Field(
        default="Cocoon,Caterpillar,CAT,Hey Cat,excavator,wheel loader,dozer,backhoe,hydraulic,"
                "seatbelt,walkaround,bucket,boom,track tension,operator,idle",
        alias="ASSEMBLYAI_KEYTERMS",
    )
    cartesia_model: CartesiaModel = Field(default="sonic-3", alias="CARTESIA_MODEL")
    cartesia_voice_id: str = Field(default="f786b574-daa5-4673-aa0c-cbe3e8534c02", alias="CARTESIA_VOICE_ID",
                                   min_length=8)
    cartesia_speed: float | None = Field(default=None, alias="CARTESIA_SPEED", ge=0.6, le=1.5)
    # access_token: mint a short-lived TTS token from the key (raw key rejected by Cartesia TTS; see cartesia_auth.py)
    cartesia_auth: Literal["access_token", "api_key"] = Field(default="access_token", alias="CARTESIA_AUTH")
    voice_language: Literal["en"] = Field(default="en", alias="VOICE_LANGUAGE")

    # ---------------------------------------------------------------- Vertex AI (ADC)
    google_cloud_project: str | None = Field(default=None, alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str = Field(default="global", alias="GOOGLE_CLOUD_LOCATION")
    google_genai_use_vertexai: bool = Field(default=True, alias="GOOGLE_GENAI_USE_VERTEXAI")
    vertex_model: str = Field(default="gemini-2.5-flash", alias="VERTEX_MODEL", min_length=3)
    vertex_thinking: Literal["minimal", "model_default"] = Field(default="minimal", alias="VERTEX_THINKING")
    vertex_temperature: float = Field(default=0.6, alias="VERTEX_TEMPERATURE", ge=0.0, le=2.0)
    vertex_max_output_tokens: int = Field(default=220, alias="VERTEX_MAX_OUTPUT_TOKENS", ge=32, le=2048)
    llm_first_chunk_timeout_s: float = Field(default=3.5, alias="LLM_FIRST_CHUNK_TIMEOUT_SECONDS", gt=0, le=60)
    llm_stall_timeout_s: float = Field(default=5.0, alias="LLM_STALL_TIMEOUT_SECONDS", gt=0, le=60)
    llm_max_attempts: int = Field(default=3, alias="LLM_MAX_ATTEMPTS", ge=1, le=4)
    max_context_turns: int = Field(default=8, alias="MAX_CONTEXT_TURNS", ge=1, le=40)

    # ---------------------------------------------------------------- brain / profile
    voice_brain: Literal["standalone_vertex", "remote_langgraph"] = Field(default="standalone_vertex",
                                                                         alias="VOICE_BRAIN")
    voice_profile: Literal["development", "production"] = Field(default="development", alias="VOICE_PROFILE")
    voice_greeting: str = Field(default="Hi, I'm Cat, your Cocoon assistant.", alias="VOICE_GREETING",
                                min_length=1, max_length=200)

    # ---------------------------------------------------------------- wake gate
    # off = no wake gating (local Playground/diagnostic mode): starts ACTIVE, never re-arms, every final
    # transcript goes to the agent. Rejected in VOICE_PROFILE=production.
    wake_mode: Literal["transcript", "livekit_wakeword", "porcupine", "off"] = Field(default="transcript",
                                                                                     alias="WAKE_MODE")
    wake_phrase: str = Field(default="Hey Cat", alias="WAKE_PHRASE")
    wake_active_timeout_s: float = Field(default=45.0, alias="WAKE_ACTIVE_TIMEOUT_SECONDS", ge=5, le=600)
    wake_debounce_s: float = Field(default=2.0, alias="WAKE_DEBOUNCE_SECONDS", ge=0, le=30)
    wake_echo_guard_ms: int = Field(default=600, alias="WAKE_ECHO_GUARD_MS", ge=0, le=5000)
    picovoice_access_key: SecretStr | None = Field(default=None, alias="PICOVOICE_ACCESS_KEY")
    porcupine_keyword_path: Path | None = Field(default=None, alias="PORCUPINE_KEYWORD_PATH")
    porcupine_sensitivity: float = Field(default=0.5, alias="PORCUPINE_SENSITIVITY", ge=0.0, le=1.0)
    wakeword_model_path: Path | None = Field(default=None, alias="LIVEKIT_WAKEWORD_MODEL_PATH")
    wakeword_threshold: float = Field(default=0.7, alias="LIVEKIT_WAKEWORD_THRESHOLD", gt=0.0, lt=1.0)
    wakeword_hop_ms: int = Field(default=160, alias="LIVEKIT_WAKEWORD_HOP_MS", ge=80, le=320)
    # apply to every acoustic mode (livekit_wakeword, porcupine)
    wake_preroll_ms: int = Field(default=600, alias="WAKE_PREROLL_MS", ge=0, le=2000)
    wake_only_ack_wait_ms: int = Field(default=900, alias="WAKE_ONLY_ACK_WAIT_MS", ge=100, le=5000)

    # ---------------------------------------------------------------- audio / turns
    noise_cancellation: Literal["krisp", "none"] = Field(default="krisp", alias="NOISE_CANCELLATION")
    noise_profile: Literal["voice_isolation", "noise_suppression"] = Field(default="voice_isolation",
                                                                            alias="NOISE_PROFILE")
    krisp_suppression_level: int = Field(default=75, alias="KRISP_SUPPRESSION_LEVEL", ge=0, le=100)
    allow_degraded_audio: bool = Field(default=False, alias="ALLOW_DEGRADED_AUDIO")
    preemptive_generation: bool = Field(default=False, alias="PREEMPTIVE_GENERATION")
    # adaptive = LiveKit's barge-in model (rejects backchannels/noise; needs LiveKit Cloud or dev mode and an STT
    # with aligned transcripts); vad = voice activity only
    interruption_mode: Literal["adaptive", "vad"] = Field(default="adaptive", alias="INTERRUPTION_MODE")
    interruption_min_duration_s: float = Field(default=0.5, alias="INTERRUPTION_MIN_DURATION_SECONDS", ge=0.1,
                                               le=2.0)
    interruption_min_words: int = Field(default=0, alias="INTERRUPTION_MIN_WORDS", ge=0, le=3)
    # seconds of detected speech without any transcript before Cat asks the user to repeat (0 = off)
    transcription_timeout_s: float = Field(default=3.0, alias="TRANSCRIPTION_TIMEOUT_SECONDS", ge=0, le=15)
    # only speech at least this long (VAD) earns a "could you say that again?" (shorter blips are treated as noise)
    clarify_min_speech_s: float = Field(default=0.8, alias="CLARIFY_MIN_SPEECH_SECONDS", ge=0.2, le=5)
    false_interruption_timeout_s: float = Field(default=2.0, alias="FALSE_INTERRUPTION_TIMEOUT_SECONDS", ge=0.3,
                                                le=5.0)
    thinking_cue_enabled: bool = Field(default=True, alias="THINKING_CUE_ENABLED")
    thinking_cue_delay_ms: int = Field(default=1200, alias="THINKING_CUE_DELAY_MS", ge=300, le=10000)
    tts_cache_dir: Path = Field(default=SERVICE_DIR / ".cache" / "tts", alias="TTS_CACHE_DIR")

    # ---------------------------------------------------------------- privacy / diagnostics
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", alias="LOG_LEVEL")
    log_transcripts: bool = Field(default=False, alias="LOG_TRANSCRIPTS")
    record_audio: bool = Field(default=False, alias="RECORD_AUDIO")
    record_audio_dir: Path = Field(default=SERVICE_DIR / "recordings", alias="RECORD_AUDIO_DIR")
    record_retention_hours: float = Field(default=24.0, alias="RECORD_RETENTION_HOURS", gt=0, le=720)
    metrics_dir: Path = Field(default=SERVICE_DIR / "metrics", alias="METRICS_DIR")

    # ---------------------------------------------------------------- remote brain (VOICE_BRAIN=remote_langgraph)
    backend_url: str = Field(default="http://127.0.0.1:8000", alias="COCOON_BACKEND_URL")
    service_token: SecretStr | None = Field(default=None, alias="COCOON_SERVICE_TOKEN")
    # one HTTP request; a live backend turn makes two model calls of about 2-15 s each
    backend_request_timeout: float = Field(default=20.0, alias="COCOON_BACKEND_REQUEST_TIMEOUT", gt=0, le=120)
    backend_connect_timeout: float = Field(default=3.0, alias="COCOON_BACKEND_CONNECT_TIMEOUT", gt=0, le=30)
    backend_max_attempts: int = Field(default=4, alias="COCOON_BACKEND_MAX_ATTEMPTS", ge=1, le=10)
    # whole turn including 202 polling and retries (backend COCOON_TURN_TIMEOUT_SECONDS defaults to 45)
    turn_deadline: float = Field(default=55.0, alias="COCOON_TURN_DEADLINE_SECONDS", gt=0, le=300)
    # catalog IDs only (free-text IDs get 422 from the backend for new sessions)
    default_machine_id: str = Field(default="EXC_DEMO_001", alias="COCOON_DEFAULT_MACHINE_ID", min_length=1)
    # used only when neither job metadata nor the participant attribute "operator_id" names one;
    # the LiveKit participant identity is never used as a catalog operator ID
    default_operator_id: str | None = Field(default=None, alias="COCOON_DEFAULT_OPERATOR_ID")

    @field_validator("wake_phrase")
    @classmethod
    def _wake_phrase_words(cls, value: str) -> str:
        words = value.split()
        if not 1 <= len(words) <= 4:
            raise ValueError("WAKE_PHRASE must be 1-4 words")
        return value

    @field_validator("livekit_url", "livekit_api_key", "google_cloud_project", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("assemblyai_api_key", "cartesia_api_key", "livekit_api_secret", "picovoice_access_key",
                     "service_token", "porcupine_keyword_path", "wakeword_model_path", "default_operator_id",
                     mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _resolve(self) -> "VoiceSettings":
        if self.legacy_agent_name:
            if "agent_name" in self.model_fields_set and self.agent_name != self.legacy_agent_name:
                raise ValueError("LIVEKIT_AGENT_NAME and deprecated COCOON_AGENT_NAME disagree; remove COCOON_AGENT_NAME")
            if "agent_name" not in self.model_fields_set:
                log.warning("COCOON_AGENT_NAME is deprecated; rename it to LIVEKIT_AGENT_NAME")
                self.agent_name = self.legacy_agent_name
        if self.assemblyai_min_turn_silence_ms >= self.assemblyai_max_turn_silence_ms:
            raise ValueError("ASSEMBLYAI_MIN_TURN_SILENCE_MS must be below ASSEMBLYAI_MAX_TURN_SILENCE_MS")
        for attr in ("tts_cache_dir", "record_audio_dir", "metrics_dir"):
            path = getattr(self, attr)
            if not path.is_absolute():
                setattr(self, attr, SERVICE_DIR / path)
        if self.porcupine_keyword_path is not None and not self.porcupine_keyword_path.is_absolute():
            self.porcupine_keyword_path = SERVICE_DIR / self.porcupine_keyword_path
        if self.wakeword_model_path is not None and not self.wakeword_model_path.is_absolute():
            self.wakeword_model_path = SERVICE_DIR / self.wakeword_model_path
        return self

    # ---------------------------------------------------------------- derived values

    @property
    def keyterms(self) -> list[str]:
        return [t.strip() for t in self.assemblyai_keyterms.split(",") if t.strip()]

    @property
    def acoustic_wake(self) -> bool:
        return self.wake_mode in ("livekit_wakeword", "porcupine")

    def wake_mode_description(self) -> str:
        return {
            "off": "off (no wake gating: starts ACTIVE, never re-arms, every final transcript reaches the agent)",
            "transcript": f"transcript (say '{self.wake_phrase}' first; follow-ups accepted for "
                          f"{self.wake_active_timeout_s:.0f}s of inactivity, then re-armed)",
            "livekit_wakeword": "livekit_wakeword (acoustic keyword spotting; STT opens after the keyword)",
            "porcupine": "porcupine (acoustic keyword spotting; STT opens after the keyword)",
        }[self.wake_mode]

    def wake_model_mismatch(self) -> str | None:
        """Warn when the keyword model's name does not match WAKE_PHRASE (e.g. testing with hey_livekit)."""
        from .wake import normalize

        if self.wake_mode != "livekit_wakeword" or self.wakeword_model_path is None:
            return None
        model_words = normalize(self.wakeword_model_path.stem.replace("_", " "))
        if model_words != normalize(self.wake_phrase):
            return (f"wake model '{self.wakeword_model_path.stem}' does not match WAKE_PHRASE '{self.wake_phrase}' "
                    "(fine for a pipeline test; set WAKE_PHRASE to the model's phrase)")
        return None

    def missing_livekit(self) -> list[str]:
        return [name for name, value in (
            ("LIVEKIT_URL", self.livekit_url), ("LIVEKIT_API_KEY", self.livekit_api_key),
            ("LIVEKIT_API_SECRET", self.livekit_api_secret),
        ) if not value]

    def problems(self, purpose: Literal["worker", "doctor", "offline"] = "worker") -> list[str]:
        """Human-readable blocking problems (variable names only, never values)."""
        issues: list[str] = []
        if purpose in ("worker", "doctor"):
            issues += [f"{name} is not set" for name in self.missing_livekit()]
            if self.livekit_url and not self.livekit_url.startswith(("wss://", "ws://")) or (
                    self.livekit_url and "<" in self.livekit_url):
                issues.append("LIVEKIT_URL must be a real wss:// URL for your LiveKit Cloud project")
            for name, value in (("ASSEMBLYAI_API_KEY", self.assemblyai_api_key),
                                ("CARTESIA_API_KEY", self.cartesia_api_key)):
                if value is None:
                    issues.append(f"{name} is not set")
        if self.voice_brain == "remote_langgraph":
            # the backend owns the model; the worker needs no Vertex settings in this mode
            if self.service_token is None:
                issues.append("VOICE_BRAIN=remote_langgraph requires COCOON_SERVICE_TOKEN (same value as the backend)")
            if not self.backend_url.startswith(("http://", "https://")):
                issues.append("COCOON_BACKEND_URL must be an http(s) URL")
            if self.preemptive_generation:
                issues.append("VOICE_BRAIN=remote_langgraph requires PREEMPTIVE_GENERATION=false "
                              "(a backend turn can save records)")
        else:
            if not self.google_genai_use_vertexai:
                issues.append("GOOGLE_GENAI_USE_VERTEXAI must be true (Vertex AI via ADC; the Gemini Developer API "
                              "is not used)")
            if not self.google_cloud_project:
                issues.append("GOOGLE_CLOUD_PROJECT is not set")
        for name in FORBIDDEN_GOOGLE_KEYS:
            if os.environ.get(name):
                issues.append(f"{name} is set; remove it. Vertex AI uses ADC and must not fall back to an API key")
        if self.wake_mode == "livekit_wakeword":
            if self.wakeword_model_path is None:
                issues.append("WAKE_MODE=livekit_wakeword requires LIVEKIT_WAKEWORD_MODEL_PATH (a trained .onnx)")
            elif not self.wakeword_model_path.is_file():
                issues.append("LIVEKIT_WAKEWORD_MODEL_PATH does not point to an existing file")
            elif self.wakeword_model_path.suffix.lower() != ".onnx":
                issues.append("LIVEKIT_WAKEWORD_MODEL_PATH must be a livekit-wakeword .onnx classifier")
        if self.wake_mode == "porcupine":
            if self.picovoice_access_key is None:
                issues.append("WAKE_MODE=porcupine requires PICOVOICE_ACCESS_KEY")
            if self.porcupine_keyword_path is None:
                issues.append("WAKE_MODE=porcupine requires PORCUPINE_KEYWORD_PATH (a real custom 'Hey Cat' .ppn)")
            elif not self.porcupine_keyword_path.is_file():
                issues.append("PORCUPINE_KEYWORD_PATH does not point to an existing file")
            elif self.porcupine_keyword_path.suffix.lower() != ".ppn":
                issues.append("PORCUPINE_KEYWORD_PATH must be a Porcupine .ppn keyword file")
        if self.voice_profile == "production":
            if self.wake_mode in ("transcript", "off"):
                issues.append(f"VOICE_PROFILE=production rejects WAKE_MODE={self.wake_mode} "
                              "(idle speech is streamed to STT)")
            if self.noise_cancellation != "krisp" or self.allow_degraded_audio:
                issues.append("VOICE_PROFILE=production requires NOISE_CANCELLATION=krisp and ALLOW_DEGRADED_AUDIO=false")
            if self.preemptive_generation:
                issues.append("VOICE_PROFILE=production requires PREEMPTIVE_GENERATION=false")
            if self.log_transcripts or self.record_audio:
                issues.append("VOICE_PROFILE=production requires LOG_TRANSCRIPTS=false and RECORD_AUDIO=false")
        if self.noise_cancellation == "none" and not self.allow_degraded_audio:
            issues.append("NOISE_CANCELLATION=none needs ALLOW_DEGRADED_AUDIO=true (development only)")
        return issues

    def obsolete_settings_present(self) -> list[str]:
        from dotenv import dotenv_values

        present = {k for k, v in dotenv_values(ENV_FILE).items() if v} if ENV_FILE.exists() else set()
        present |= {k for k in OBSOLETE_SETTINGS if os.environ.get(k)}
        return sorted(f"{k} (use {OBSOLETE_SETTINGS[k]})" for k in present if k in OBSOLETE_SETTINGS)

    def safe_summary(self) -> dict[str, object]:
        """Non-secret effective configuration, suitable for logs and doctor output."""
        def presence(value: object) -> str:
            return "set" if value else "MISSING"

        return {
            "profile": self.voice_profile,
            "brain": self.voice_brain,
            "backend": ({"url": self.backend_url, "service_token": presence(self.service_token),
                         "default_machine_id": self.default_machine_id,
                         "default_operator_id": self.default_operator_id or "UNSET",
                         "turn_deadline_s": self.turn_deadline}
                        if self.voice_brain == "remote_langgraph" else "unused"),
            "agent_name": self.agent_name,
            "livekit_url": self.livekit_url or "MISSING",
            "livekit_api_key": presence(self.livekit_api_key),
            "livekit_api_secret": presence(self.livekit_api_secret),
            "assemblyai": {"model": self.assemblyai_model, "api_key": presence(self.assemblyai_api_key),
                           "min_turn_silence_ms": self.assemblyai_min_turn_silence_ms,
                           "max_turn_silence_ms": self.assemblyai_max_turn_silence_ms,
                           "keyterms": len(self.keyterms)},
            "cartesia": {"model": self.cartesia_model, "voice_id": self.cartesia_voice_id,
                         "api_key": presence(self.cartesia_api_key), "auth": self.cartesia_auth},
            "vertex": {"project": self.google_cloud_project, "location": self.google_cloud_location,
                       "use_vertexai": self.google_genai_use_vertexai, "model": self.vertex_model,
                       "thinking": self.vertex_thinking, "auth": "application-default-credentials"},
            "wake": {"mode": self.wake_mode, "phrase": self.wake_phrase,
                     "active_timeout_s": self.wake_active_timeout_s,
                     "picovoice_access_key": presence(self.picovoice_access_key) if self.wake_mode == "porcupine"
                     else "not needed",
                     "keyword_path": str(self.porcupine_keyword_path) if self.porcupine_keyword_path else None,
                     "wakeword_model": self.wakeword_model_path.name if self.wakeword_model_path else None,
                     "wakeword_threshold": self.wakeword_threshold},
            "noise": {"mode": self.noise_cancellation, "profile": self.noise_profile,
                      "allow_degraded": self.allow_degraded_audio},
            "preemptive_generation": self.preemptive_generation,
            "log_transcripts": self.log_transcripts,
            "record_audio": self.record_audio,
        }


def load_settings(**overrides) -> VoiceSettings:
    return VoiceSettings(**overrides)  # type: ignore[call-arg]


@lru_cache
def get_settings() -> VoiceSettings:
    return load_settings()
