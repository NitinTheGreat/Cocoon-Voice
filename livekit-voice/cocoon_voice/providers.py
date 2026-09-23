"""Provider factories: AssemblyAI STT, Cartesia TTS, Silero VAD, Krisp input filter, and the brain.

create_brain() is the single replacement point for the later remote LangGraph adapter:
it must return a LiveKit-compatible *streaming* llm.LLM. Nothing else in the worker knows
which brain is in use, and the LiveKit session's chat context is the only conversation history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from livekit import rtc
from livekit.agents import llm

# LiveKit plugins must be imported (registered) on the main thread at module import time,
# never lazily inside a job, so they are imported here.
from livekit.plugins import assemblyai, cartesia, google, silero

from .config import ConfigError, VoiceSettings

try:  # native wheels; a failure is reported explicitly by build_noise_cancellation()
    from livekit.plugins import krisp, noise_cancellation

    _NOISE_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # pragma: no cover - platform dependent
    krisp = noise_cancellation = None  # type: ignore[assignment]
    _NOISE_IMPORT_ERROR = _exc

log = logging.getLogger("cocoon_voice.providers")


def build_stt(settings: VoiceSettings, http_session=None):
    kwargs: dict[str, Any] = {
        "api_key": settings.assemblyai_api_key.get_secret_value() if settings.assemblyai_api_key else None,
        "model": settings.assemblyai_model,
        # One authoritative end-of-turn policy: AssemblyAI endpointing (turn_detection="stt").
        "min_turn_silence": settings.assemblyai_min_turn_silence_ms,
        "max_turn_silence": settings.assemblyai_max_turn_silence_ms,
        "keyterms_prompt": settings.keyterms,
    }
    if kwargs["api_key"] is None:
        kwargs.pop("api_key")  # plugin raises a clear error naming ASSEMBLYAI_API_KEY
    if http_session is not None:  # only needed outside a LiveKit job (smoke/benchmark tools)
        kwargs["http_session"] = http_session
    return assemblyai.STT(**kwargs)


def build_tts(settings: VoiceSettings, http_session=None, credential: str | None = None):
    """credential: a minted access token (CARTESIA_AUTH=access_token) or None to use the raw key."""
    raw_key = settings.cartesia_api_key.get_secret_value() if settings.cartesia_api_key else None
    return cartesia.TTS(
        api_key=credential or raw_key,
        model=settings.cartesia_model,
        voice=settings.cartesia_voice_id,
        language=settings.voice_language,
        speed=settings.cartesia_speed,
        http_session=http_session,  # None inside a job: the plugin uses the job's shared session
        # Streaming synthesis: the plugin's sentence tokenizer coalesces LLM tokens into
        # speakable segments, so audio starts after the first sentence, not the whole reply.
    )


def load_vad():
    """Load Silero VAD (bundled ONNX model; no provider key). Call from prewarm, not per turn."""
    return silero.VAD.load(min_silence_duration=0.45, activation_threshold=0.5)


def vertex_thinking_config(model: str, level: str) -> dict[str, Any] | None:
    """Lowest supported thinking setting per Gemini family; None = leave the model default.

    gemini-2.5-flash / flash-lite accept thinking_budget=0 (thinking off).
    gemini-3.x uses thinking_level ("minimal"). Other models: do not send unsupported fields.
    Thoughts are never requested (include_thoughts is left false), so none reach speech/logs.
    """
    if level != "minimal":
        return None
    name = model.lower()
    if name.startswith(("gemini-2.5-flash",)):
        return {"thinking_budget": 0}
    if name.startswith("gemini-3"):
        return {"thinking_level": "minimal"}
    return None


def create_brain(settings: VoiceSettings) -> llm.LLM:
    """Return the conversation brain as a streaming LiveKit LLM.

    Phase 1 (this build): Gemini on Vertex AI through ADC.
    Future: VOICE_BRAIN=remote_langgraph returns a streaming adapter that forwards the
    completed user turn to langgraph-agent; it plugs in here and nowhere else.
    """
    if settings.voice_brain != "standalone_vertex":
        raise ConfigError("VOICE_BRAIN=remote_langgraph is future-only and not implemented in this phase")
    if not settings.google_genai_use_vertexai:
        raise ConfigError("GOOGLE_GENAI_USE_VERTEXAI must be true")
    if not settings.google_cloud_project:
        raise ConfigError("GOOGLE_CLOUD_PROJECT is not set")
    kwargs: dict[str, Any] = dict(
        model=settings.vertex_model,
        vertexai=True,  # explicit: never let GOOGLE_API_KEY select the Gemini Developer API
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
        temperature=settings.vertex_temperature,
        max_output_tokens=settings.vertex_max_output_tokens,
    )
    thinking = vertex_thinking_config(settings.vertex_model, settings.vertex_thinking)
    if thinking is not None:
        kwargs["thinking_config"] = thinking
    return google.LLM(**kwargs)


@dataclass(frozen=True)
class NoiseSetup:
    """What input processing is actually configured (logged, non-secret)."""

    processor: rtc.NoiseCancellationOptions | rtc.FrameProcessor[rtc.AudioFrame] | None
    effective: str  # e.g. "krisp_viva_voice_isolation(level=75)", "krisp_nc", "DEGRADED:none"
    degraded: bool


def krisp_filter_active(processor) -> bool | None:
    """True once Krisp VIVA has credentials and a live filter (it passes audio through unfiltered before that)."""
    inner = getattr(processor, "_inner", None)
    if inner is None:
        return None
    return getattr(inner, "_credentials", None) is not None and getattr(inner, "_filter", None) is not None


def check_noise_cancellation(settings: VoiceSettings) -> None:
    """Startup validation without constructing a native filter (one filter is built per session)."""
    if settings.noise_cancellation == "none":
        if not settings.allow_degraded_audio or settings.voice_profile == "production":
            raise ConfigError("NOISE_CANCELLATION=none requires ALLOW_DEGRADED_AUDIO=true in development")
        return
    if _NOISE_IMPORT_ERROR is not None and not (settings.allow_degraded_audio
                                                and settings.voice_profile != "production"):
        raise ConfigError(f"NOISE_CANCELLATION=krisp could not be initialised ({type(_NOISE_IMPORT_ERROR).__name__})")


def build_noise_cancellation(settings: VoiceSettings) -> NoiseSetup:
    """Krisp through LiveKit Cloud, built once per session and passed to the SDK's room input.

    NOISE_CANCELLATION=none (with ALLOW_DEGRADED_AUDIO=true) disables worker-side enhancement completely:
    no processor is built or passed, and nothing re-enables it. Krisp frame processing and credential
    updates run where the SDK calls them (per frame on the event loop); this module does not move them.
    """
    if settings.noise_cancellation == "none":
        check_noise_cancellation(settings)
        return NoiseSetup(None, "DEGRADED:none (worker-side enhancement disabled by NOISE_CANCELLATION=none)", True)
    try:
        if _NOISE_IMPORT_ERROR is not None:
            raise _NOISE_IMPORT_ERROR
        if settings.noise_profile == "voice_isolation":
            processor = krisp.voice_isolation(noise_suppression_level=settings.krisp_suppression_level)
            return NoiseSetup(processor, f"krisp_viva_voice_isolation(level={settings.krisp_suppression_level})",
                              False)
        return NoiseSetup(noise_cancellation.NC(), "krisp_nc_noise_suppression", False)
    except Exception as exc:  # import/native-library failure on this platform
        if settings.allow_degraded_audio and settings.voice_profile != "production":
            log.error("Krisp unavailable (%s); running DEGRADED without noise cancellation", type(exc).__name__)
            return NoiseSetup(None, f"DEGRADED:krisp_unavailable({type(exc).__name__})", True)
        raise ConfigError(f"NOISE_CANCELLATION=krisp could not be initialised ({type(exc).__name__})") from exc
