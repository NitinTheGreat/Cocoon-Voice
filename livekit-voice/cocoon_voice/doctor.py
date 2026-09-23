"""Configuration and provider doctor.

    python -m cocoon_voice.doctor              # config + every reachable provider (small requests)
    python -m cocoon_voice.doctor --offline    # config/local checks only, no network

Never prints secrets or tokens. Vertex AI is checked through the official google-genai SDK,
which performs normal Application Default Credentials discovery; the ADC file is not read here.
Exit code 1 if a check required by the current configuration fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass

import httpx

from .config import VoiceSettings, get_settings


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP | WARN
    detail: str
    required: bool = True


def _short(msg: object, limit: int = 180) -> str:
    text = " ".join(str(msg).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def classify_vertex_error(exc: BaseException) -> tuple[str, str]:
    """(category, advice) for a Vertex failure. Categories are stable strings used in tests."""
    from google.auth import exceptions as gauth

    if isinstance(exc, gauth.DefaultCredentialsError):
        return "missing_adc", "No Application Default Credentials found. Run: gcloud auth application-default login"
    if isinstance(exc, gauth.RefreshError):
        return "adc_refresh_failed", "ADC could not refresh (expired/revoked). Re-run gcloud auth application-default login"
    code = getattr(exc, "code", None)
    text = str(exc).lower()
    if code == 403:
        if "service_disabled" in text or "has not been used" in text or "is disabled" in text:
            return "api_disabled", "Enable the Vertex AI API (aiplatform.googleapis.com) for the project"
        if "billing" in text:
            return "billing", "Billing is not enabled or not usable for the project"
        return "permission_denied", "The ADC identity lacks Vertex AI access (e.g. roles/aiplatform.user)"
    if code == 429:
        return "quota_or_rate_limit", "Quota or rate limit reached; retry later or raise quota"
    if code == 404:
        return "model_unavailable", "Model not available in this project/location; check VERTEX_MODEL"
    if code == 400:
        return "invalid_request", "Request rejected (unsupported model parameter?)"
    if code == 401:
        return "unauthenticated", "Credentials rejected; re-run gcloud auth application-default login"
    return "other", type(exc).__name__


def check_vertex(s: VoiceSettings) -> Check:
    from google import genai
    from google.genai import types

    from .providers import vertex_thinking_config

    if not s.google_genai_use_vertexai or not s.google_cloud_project:
        return Check("vertex", "FAIL", "GOOGLE_GENAI_USE_VERTEXAI=true and GOOGLE_CLOUD_PROJECT are required")
    try:
        client = genai.Client(vertexai=True, project=s.google_cloud_project, location=s.google_cloud_location)
        thinking = vertex_thinking_config(s.vertex_model, s.vertex_thinking)
        cfg = types.GenerateContentConfig(max_output_tokens=16,
                                          thinking_config=types.ThinkingConfig(**thinking) if thinking else None)
        t0 = time.perf_counter()
        resp = client.models.generate_content(model=s.vertex_model, contents="Reply with the word ready.", config=cfg)
        ms = (time.perf_counter() - t0) * 1000
        ok = bool((resp.text or "").strip())
        return Check("vertex", "PASS" if ok else "FAIL",
                     f"model={s.vertex_model} location={s.google_cloud_location} auth=ADC "
                     f"latency={ms:.0f}ms thinking={thinking or 'model default'}"
                     + ("" if ok else " (empty response)"))
    except Exception as exc:  # classified, sanitized
        category, advice = classify_vertex_error(exc)
        return Check("vertex", "FAIL", f"{category}: {advice} [{_short(getattr(exc, 'message', exc))}]")


def check_assemblyai(s: VoiceSettings, client: httpx.Client) -> Check:
    if s.assemblyai_api_key is None:
        return Check("assemblyai", "FAIL", "ASSEMBLYAI_API_KEY is not set")
    try:
        r = client.get("https://streaming.assemblyai.com/v3/token", params={"expires_in_seconds": 60},
                       headers={"Authorization": s.assemblyai_api_key.get_secret_value()})
    except httpx.HTTPError as exc:
        return Check("assemblyai", "FAIL", f"network: {type(exc).__name__}")
    if r.status_code == 200:  # a temporary streaming token was issued (not printed)
        return Check("assemblyai", "PASS", f"streaming key accepted; model={s.assemblyai_model} "
                                           f"keyterms={len(s.keyterms)}")
    return Check("assemblyai", "FAIL", f"HTTP {r.status_code} (401/403 = invalid key or no streaming access)")


def check_cartesia(s: VoiceSettings, client: httpx.Client) -> Check:
    if s.cartesia_api_key is None:
        return Check("cartesia", "FAIL", "CARTESIA_API_KEY is not set")
    try:
        r = client.get(f"https://api.cartesia.ai/voices/{s.cartesia_voice_id}",
                       headers={"X-API-Key": s.cartesia_api_key.get_secret_value(),
                                "Cartesia-Version": "2025-04-16"})
    except httpx.HTTPError as exc:
        return Check("cartesia", "FAIL", f"network: {type(exc).__name__}")
    if r.status_code == 200:
        v = r.json()
        return Check("cartesia", "PASS", f"model={s.cartesia_model} voice={s.cartesia_voice_id} "
                                         f"name={v.get('name')!r} language={v.get('language')!r}")
    if r.status_code == 404:
        return Check("cartesia", "FAIL", f"voice {s.cartesia_voice_id} not found for this key; set CARTESIA_VOICE_ID")
    return Check("cartesia", "FAIL", f"HTTP {r.status_code} (401/403 = invalid key)")


def check_livekit(s: VoiceSettings) -> Check:
    missing = s.missing_livekit()
    if missing:
        return Check("livekit", "FAIL", f"missing {', '.join(missing)}")
    if "<" in (s.livekit_url or ""):
        return Check("livekit", "FAIL", "LIVEKIT_URL still contains the .env.example placeholder")
    from livekit import api

    async def _probe() -> int:
        async with api.LiveKitAPI(s.livekit_url, s.livekit_api_key,
                                  s.livekit_api_secret.get_secret_value()) as lk:  # type: ignore[union-attr]
            rooms = await lk.room.list_rooms(api.ListRoomsRequest())
            return len(rooms.rooms)

    try:
        n = asyncio.run(_probe())
        return Check("livekit", "PASS", f"project reachable at {s.livekit_url}; {n} active room(s); "
                                        f"dispatch name '{s.agent_name}'")
    except Exception as exc:
        return Check("livekit", "FAIL", f"{type(exc).__name__}: {_short(exc)}")


def check_noise(s: VoiceSettings) -> Check:
    from .providers import build_noise_cancellation

    try:
        setup = build_noise_cancellation(s)
    except Exception as exc:
        return Check("noise", "FAIL", _short(exc))
    if setup.degraded:
        return Check("noise", "WARN", f"{setup.effective} (development only; noise handling NOT active)", False)
    return Check("noise", "PASS", f"{setup.effective} constructed on this platform; it authenticates with the "
                                  "LiveKit Cloud job token and only filters audio inside a live cloud session "
                                  "(not validated here)")


def check_porcupine(s: VoiceSettings) -> Check:
    if s.wake_mode != "porcupine":
        return Check("porcupine", "SKIP", "WAKE_MODE=transcript (acoustic keyword spotting not in use)", False)
    problems = [p for p in s.problems("offline") if "PORCUPINE" in p or "PICOVOICE" in p]
    if problems:
        return Check("porcupine", "FAIL", "; ".join(problems))
    from .porcupine_gate import PorcupineEngine

    try:
        engine = PorcupineEngine(s.picovoice_access_key.get_secret_value(), s.porcupine_keyword_path,  # type: ignore
                                 s.porcupine_sensitivity)
        detail = (f"engine created: sample_rate={engine.sample_rate} frame_length={engine.frame_length} "
                  f"version={engine.version} keyword={s.porcupine_keyword_path.name}")  # type: ignore[union-attr]
        engine.delete()
        return Check("porcupine", "PASS", detail)
    except Exception as exc:
        return Check("porcupine", "FAIL", f"{type(exc).__name__}: {_short(exc)}")


def check_vad() -> Check:
    from .providers import load_vad

    t0 = time.perf_counter()
    try:
        load_vad()
    except Exception as exc:
        return Check("silero_vad", "FAIL", f"{type(exc).__name__}: {_short(exc)}")
    return Check("silero_vad", "PASS", f"model loaded locally in {(time.perf_counter() - t0) * 1000:.0f}ms (no key)")


def run(offline: bool) -> list[Check]:
    checks: list[Check] = []
    try:
        s = get_settings()
    except Exception as exc:
        return [Check("settings", "FAIL", _short(exc, 400))]
    config_problems = s.problems("offline")
    checks.append(Check("settings", "FAIL" if config_problems else "PASS",
                        "; ".join(config_problems) or f"profile={s.voice_profile} brain={s.voice_brain}"))
    for obsolete in s.obsolete_settings_present():
        checks.append(Check("obsolete_setting", "WARN", f"{obsolete} is ignored", False))
    checks.append(check_vad())
    checks.append(check_noise(s))
    checks.append(check_porcupine(s))
    if offline:
        return checks
    checks.append(check_vertex(s))
    with httpx.Client(timeout=10) as client:
        checks.append(check_assemblyai(s, client))
        checks.append(check_cartesia(s, client))
    checks.append(check_livekit(s))
    return checks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="skip provider network checks")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    checks = run(args.offline)
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        try:
            print("effective config:", json.dumps(get_settings().safe_summary(), indent=2))
        except Exception:
            pass
        for c in checks:
            print(f"[{c.status:4}] {c.name:16} {c.detail}")
    failed = [c for c in checks if c.status == "FAIL" and c.required]
    if failed:
        print(f"\n{len(failed)} required check(s) failed: {', '.join(c.name for c in failed)}", file=sys.stderr)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
