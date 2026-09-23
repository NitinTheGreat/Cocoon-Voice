"""Regression tests for Cartesia authentication (see cartesia_auth.py for the evidence).

Offline: the Cartesia HTTP API is replaced by httpx.MockTransport. No real key is used.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from cocoon_voice.cartesia_auth import (AccessToken, CartesiaAuthError, CartesiaTokenRefresher, describe_status,
                                        mint_access_token, sanitize)
from cocoon_voice.providers import build_tts

from .fakes import settings

RAW_KEY = "sk_car_TESTKEYtestkey0123456789"


def api(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_mint_sends_raw_key_only_to_access_token_with_tts_grant_and_clamped_ttl():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), auth=request.headers["Authorization"],
                    version=request.headers["Cartesia-Version"], body=json.loads(request.content))
        return httpx.Response(200, json={"token": "eyJminted.token.value"})

    token = await mint_access_token(RAW_KEY, ttl_s=86400, client=api(handler))
    assert seen["url"] == "https://api.cartesia.ai/access-token"
    assert seen["auth"] == f"Bearer {RAW_KEY}"  # raw key, correct scheme, no masking
    assert seen["version"] == "2026-08-14"
    assert seen["body"] == {"grants": {"tts": True}, "expires_in": 3600}  # provider maximum
    assert token.token == "eyJminted.token.value" and "eyJ" not in repr(token)


async def test_mint_error_keeps_status_message_request_id_without_secrets():
    def handler(request):
        return httpx.Response(401, headers={"x-request-id": "req-123"},
                              json={"message": f"Invalid API key {RAW_KEY}", "title": "Unauthorized"})

    with pytest.raises(CartesiaAuthError) as exc:
        await mint_access_token(RAW_KEY, client=api(handler))
    err = exc.value
    assert (err.status, err.request_id, err.category) == (401, "req-123", "unauthenticated")
    assert "Invalid API key" in err.provider_message and RAW_KEY not in str(err)


def test_status_categories_are_not_all_invalid_key():
    assert [describe_status(s) for s in (401, 402, 403, 429, 500, None)] == [
        "unauthenticated", "payment_required", "forbidden", "rate_limited", "provider_error", "network"]
    assert sanitize(f"bad {RAW_KEY} and eyJabc.def.ghi") == "bad sk_car_<redacted> and <token>"


def test_tts_client_gets_the_minted_credential_not_the_raw_key():
    s = settings(CARTESIA_API_KEY=RAW_KEY)
    assert s.cartesia_auth == "access_token"
    assert build_tts(s, credential="eyJminted")._opts.api_key == "eyJminted"
    assert build_tts(settings(CARTESIA_API_KEY=RAW_KEY, CARTESIA_AUTH="api_key"))._opts.api_key == RAW_KEY
    assert build_tts(s)._opts.api_key != "**********"  # SecretStr unwrapped at the provider boundary


class FakePool:
    def __init__(self):
        self.invalidated = 0

    def invalidate(self):
        self.invalidated += 1


class FakeTTS:
    def __init__(self):
        self._opts = type("Opts", (), {"api_key": RAW_KEY})()
        self._pool = FakePool()


async def test_refresher_installs_and_rotates_tokens_before_expiry():
    minted = []

    def handler(request):
        minted.append(json.loads(request.content)["expires_in"])
        return httpx.Response(200, json={"token": f"eyJtoken{len(minted)}"})

    tts = FakeTTS()
    r = CartesiaTokenRefresher(tts, RAW_KEY, ttl_s=60, refresh_margin_s=59, client=api(handler))
    await r.start()
    assert tts._opts.api_key == "eyJtoken1" and tts._pool.invalidated == 1
    for _ in range(40):  # margin leaves ~1 s before refresh
        await asyncio.sleep(0.1)
        if r.refreshes:
            break
    await r.aclose()
    assert r.refreshes >= 1 and tts._opts.api_key.startswith("eyJtoken") and tts._opts.api_key != "eyJtoken1"
    assert tts._pool.invalidated >= 2 and minted[0] == 60


def test_access_token_repr_hides_value():
    assert "secret" not in repr(AccessToken("eyJsecret", 0.0))
