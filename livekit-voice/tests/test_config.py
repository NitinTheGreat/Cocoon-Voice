"""Typed settings, missing-key diagnostics, profile rules, redaction and Vertex-mode selection.

No test reads the real ADC file: credential discovery is replaced by a fake.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cocoon_voice.config import VoiceSettings

BASE = {
    "LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "lk-key", "LIVEKIT_API_SECRET": "lk-secret-value",
    "ASSEMBLYAI_API_KEY": "aai-secret-value", "CARTESIA_API_KEY": "cartesia-secret-value",
    "GOOGLE_CLOUD_PROJECT": "orbit-507316", "GOOGLE_CLOUD_LOCATION": "global", "GOOGLE_GENAI_USE_VERTEXAI": "true",
}


def make(**overrides) -> VoiceSettings:
    values = {**BASE, **overrides}
    return VoiceSettings(**{k: v for k, v in values.items() if v is not None}, _env_file=None)


@pytest.fixture(autouse=True)
def _no_google_keys(monkeypatch):
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "COCOON_AGENT_NAME", "LIVEKIT_AGENT_NAME"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_the_verified_selection():
    s = make()
    assert s.problems("worker") == []
    assert (s.assemblyai_model, s.cartesia_model, s.vertex_model) == ("universal-3-5-pro", "sonic-3", "gemini-2.5-flash")
    assert s.wake_mode == "transcript" and s.wake_phrase == "Hey Cat" and s.agent_name == "cocoon-voice"
    assert s.preemptive_generation is False and s.noise_cancellation == "krisp"
    assert "Cocoon" in s.keyterms and "CAT" in s.keyterms


def test_missing_keys_are_named_not_valued():
    s = make(ASSEMBLYAI_API_KEY="", CARTESIA_API_KEY="  ", LIVEKIT_API_SECRET="")
    problems = s.problems("worker")
    assert "ASSEMBLYAI_API_KEY is not set" in problems and "CARTESIA_API_KEY is not set" in problems
    assert "LIVEKIT_API_SECRET is not set" in problems
    assert s.problems("offline") == []  # offline checks do not need provider keys


def test_placeholder_livekit_url_is_rejected():
    assert any("LIVEKIT_URL" in p for p in make(LIVEKIT_URL="wss://<your-project>.livekit.cloud").problems())


def test_invalid_values_fail_validation():
    with pytest.raises(ValidationError):
        make(WAKE_MODE="always")
    with pytest.raises(ValidationError):
        make(ASSEMBLYAI_MODEL="invented-model")
    with pytest.raises(ValidationError):
        make(WAKE_ACTIVE_TIMEOUT_SECONDS="1")
    with pytest.raises(ValidationError):
        make(ASSEMBLYAI_MIN_TURN_SILENCE_MS="1500", ASSEMBLYAI_MAX_TURN_SILENCE_MS="1000")
    with pytest.raises(ValidationError):
        make(VOICE_LANGUAGE="hi")  # Hindi not validated for the chosen STT+TTS


def test_vertex_only_no_api_key_fallback(monkeypatch):
    assert any("GOOGLE_GENAI_USE_VERTEXAI" in p for p in make(GOOGLE_GENAI_USE_VERTEXAI="false").problems())
    monkeypatch.setenv("GOOGLE_API_KEY", "should-not-be-used")
    assert any("GOOGLE_API_KEY is set" in p for p in make().problems())
    assert any("GOOGLE_CLOUD_PROJECT" in p for p in make(GOOGLE_CLOUD_PROJECT="").problems())


def test_remote_brain_is_future_only():
    assert any("future-only" in p for p in make(VOICE_BRAIN="remote_langgraph").problems())


def test_porcupine_requires_real_key_and_ppn(tmp_path):
    problems = make(WAKE_MODE="porcupine").problems()
    assert any("PICOVOICE_ACCESS_KEY" in p for p in problems)
    assert any("PORCUPINE_KEYWORD_PATH" in p for p in problems)
    missing = make(WAKE_MODE="porcupine", PICOVOICE_ACCESS_KEY="pv", PORCUPINE_KEYWORD_PATH=str(tmp_path / "x.ppn"))
    assert any("does not point to an existing file" in p for p in missing.problems())
    wrong = tmp_path / "hey_cat.txt"
    wrong.write_text("not a model")
    assert any(".ppn" in p for p in make(WAKE_MODE="porcupine", PICOVOICE_ACCESS_KEY="pv",
                                         PORCUPINE_KEYWORD_PATH=str(wrong)).problems())


def test_production_profile_cannot_bypass_verification():
    problems = make(VOICE_PROFILE="production").problems()
    assert any("rejects WAKE_MODE=transcript" in p for p in problems)
    degraded = make(VOICE_PROFILE="production", NOISE_CANCELLATION="none", ALLOW_DEGRADED_AUDIO="true",
                    PREEMPTIVE_GENERATION="true", LOG_TRANSCRIPTS="true").problems()
    assert any("NOISE_CANCELLATION=krisp" in p for p in degraded)
    assert any("PREEMPTIVE_GENERATION=false" in p for p in degraded)
    assert any("LOG_TRANSCRIPTS" in p for p in degraded)
    assert any("ALLOW_DEGRADED_AUDIO" in p for p in make(NOISE_CANCELLATION="none").problems())


def test_legacy_agent_name_alias(monkeypatch):
    assert make(COCOON_AGENT_NAME="old-name").agent_name == "old-name"
    with pytest.raises(ValidationError):
        make(COCOON_AGENT_NAME="old-name", LIVEKIT_AGENT_NAME="new-name")


def test_safe_summary_redacts_every_secret():
    s = make(PICOVOICE_ACCESS_KEY="pv-secret-value", WAKE_MODE="porcupine")
    dumped = json.dumps(s.safe_summary()) + repr(s) + str(s)
    for secret in ("lk-secret-value", "aai-secret-value", "cartesia-secret-value", "pv-secret-value"):
        assert secret not in dumped
    assert s.safe_summary()["assemblyai"]["api_key"] == "set"
    assert make(CARTESIA_API_KEY="").safe_summary()["cartesia"]["api_key"] == "MISSING"


def test_create_brain_selects_vertex_with_fake_credential_discovery(monkeypatch):
    import google.auth
    from google.auth.credentials import AnonymousCredentials

    calls = []

    def fake_default(*args, **kwargs):
        calls.append(kwargs)
        return AnonymousCredentials(), "orbit-507316"

    monkeypatch.setattr(google.auth, "default", fake_default)
    monkeypatch.setenv("GOOGLE_API_KEY", "must-be-ignored")
    from cocoon_voice.providers import create_brain

    brain = create_brain(make())
    client = brain._client
    assert client.vertexai is True
    assert (client._api_client.project, client._api_client.location) == ("orbit-507316", "global")
    assert brain.model == "gemini-2.5-flash"
    assert brain._opts.thinking_config == {"thinking_budget": 0}


def test_thinking_config_only_for_supported_families():
    from cocoon_voice.providers import vertex_thinking_config

    assert vertex_thinking_config("gemini-2.5-flash", "minimal") == {"thinking_budget": 0}
    assert vertex_thinking_config("gemini-2.5-flash-lite", "minimal") == {"thinking_budget": 0}
    assert vertex_thinking_config("gemini-3.5-flash", "minimal") == {"thinking_level": "minimal"}
    assert vertex_thinking_config("gemini-2.5-pro", "minimal") is None  # pro cannot disable thinking
    assert vertex_thinking_config("gemini-2.5-flash", "model_default") is None


def test_noise_setup_never_silently_disabled(monkeypatch):
    from cocoon_voice.config import ConfigError
    from cocoon_voice.providers import build_noise_cancellation

    assert build_noise_cancellation(make()).effective.startswith("krisp_viva_voice_isolation")
    assert build_noise_cancellation(make(NOISE_PROFILE="noise_suppression")).effective == "krisp_nc_noise_suppression"
    with pytest.raises(ConfigError):
        build_noise_cancellation(make(NOISE_CANCELLATION="none"))
    degraded = build_noise_cancellation(make(NOISE_CANCELLATION="none", ALLOW_DEGRADED_AUDIO="true"))
    assert degraded.degraded and degraded.effective.startswith("DEGRADED")

    import cocoon_voice.providers as providers

    monkeypatch.setattr(providers, "_NOISE_IMPORT_ERROR", ImportError("simulated missing native wheel"))
    with pytest.raises(ConfigError):
        build_noise_cancellation(make())
    assert build_noise_cancellation(make(ALLOW_DEGRADED_AUDIO="true")).degraded


def test_vertex_error_classification():
    from google.auth import exceptions as gauth
    from google.genai import errors

    from cocoon_voice.doctor import classify_vertex_error

    def api_error(code: int, status: str, message: str):
        return errors.APIError(code, {"error": {"code": code, "status": status, "message": message}})

    assert classify_vertex_error(gauth.DefaultCredentialsError("none"))[0] == "missing_adc"
    assert classify_vertex_error(api_error(403, "PERMISSION_DENIED", "Permission denied on resource"))[0] == \
        "permission_denied"
    assert classify_vertex_error(api_error(403, "PERMISSION_DENIED", "SERVICE_DISABLED: API has not been used"))[0] \
        == "api_disabled"
    assert classify_vertex_error(api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"))[0] == "quota_or_rate_limit"
    assert classify_vertex_error(api_error(404, "NOT_FOUND", "Publisher model not found"))[0] == "model_unavailable"
