"""Cartesia authentication for the TTS plugin.

Evidence (2026-09-24, docs/work-log.md M9): for this project's Cartesia keys, every synthesis
endpoint (/tts/bytes, /tts/websocket; API versions 2025-04-16 and 2026-08-14; X-API-Key or Bearer)
rejects the raw API key with 401 "Invalid API key", while the same key authenticates GET /voices and
POST /access-token, and a TTS-granted access token minted from it synthesizes over HTTP and streams
over the websocket, including when sent in the X-API-Key header the LiveKit plugin uses.

CARTESIA_AUTH=access_token (default) therefore mints a short-lived TTS token server-side and gives the
plugin the token; CARTESIA_AUTH=api_key passes the raw key unchanged. Tokens last at most 1 hour, so a
per-session refresher replaces the token before expiry and recycles pooled websocket connections.
The raw key only ever goes to POST /access-token and is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("cocoon_voice.cartesia_auth")

API_BASE = "https://api.cartesia.ai"
TOKEN_API_VERSION = "2026-08-14"  # current documented Cartesia-Version
MAX_TOKEN_TTL_S = 3600  # Cartesia: "'expires_in' cannot be greater than 3600 seconds"

_KEY_RE = re.compile(r"sk_car_[A-Za-z0-9_\-]+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-.]+")


def sanitize(text: str, limit: int = 240) -> str:
    """Provider error text with any key or token removed."""
    text = _JWT_RE.sub("<token>", _KEY_RE.sub("sk_car_<redacted>", " ".join(str(text).split())))
    return text[:limit]


class CartesiaAuthError(Exception):
    def __init__(self, status: int | None, message: str, request_id: str | None):
        self.status, self.provider_message, self.request_id = status, message, request_id
        super().__init__(f"Cartesia HTTP {status}: {message} (request_id={request_id})")

    @property
    def category(self) -> str:
        return describe_status(self.status)


def describe_status(status: int | None) -> str:
    return {401: "unauthenticated", 402: "payment_required", 403: "forbidden", 404: "not_found",
            429: "rate_limited"}.get(status or 0, "provider_error" if status else "network")


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_at: float  # time.monotonic()

    def __repr__(self) -> str:  # never show the token
        return f"AccessToken(expires_in={self.expires_at - time.monotonic():.0f}s)"


def error_details(response: httpx.Response) -> tuple[str, str | None]:
    request_id = response.headers.get("x-request-id")
    try:
        data = response.json()
        message = data.get("message") or data.get("error") or response.text
        request_id = request_id or data.get("request_id")
    except ValueError:
        message = response.text
    return sanitize(message), request_id


async def mint_access_token(api_key: str, *, ttl_s: int = MAX_TOKEN_TTL_S,
                            client: httpx.AsyncClient | None = None) -> AccessToken:
    """POST /access-token with a TTS grant. Raises CartesiaAuthError with sanitized details."""
    ttl_s = max(60, min(ttl_s, MAX_TOKEN_TTL_S))
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        started = time.monotonic()
        r = await client.post(f"{API_BASE}/access-token", json={"grants": {"tts": True}, "expires_in": ttl_s},
                              headers={"Authorization": f"Bearer {api_key}", "Cartesia-Version": TOKEN_API_VERSION})
    except httpx.HTTPError as exc:
        raise CartesiaAuthError(None, f"network error: {type(exc).__name__}", None) from exc
    finally:
        if owns:
            await client.aclose()
    if r.status_code != 200:
        message, request_id = error_details(r)
        raise CartesiaAuthError(r.status_code, message, request_id)
    data: dict[str, Any] = r.json()
    token = data.get("token") or data.get("access_token")
    if not token:
        raise CartesiaAuthError(r.status_code, "response contained no token", r.headers.get("x-request-id"))
    return AccessToken(token, started + ttl_s)


class CartesiaTokenRefresher:
    """Keeps a cartesia.TTS instance supplied with a valid TTS access token for one session."""

    def __init__(self, tts: Any, api_key: str, *, ttl_s: int = MAX_TOKEN_TTL_S, refresh_margin_s: int = 600,
                 client: httpx.AsyncClient | None = None):
        self._tts = tts
        self._api_key = api_key
        self._ttl_s = ttl_s
        self._margin_s = max(0, min(refresh_margin_s, ttl_s - 1))  # refresh strictly before expiry
        self._client = client
        self._task: asyncio.Task | None = None
        self.refreshes = 0
        self.current: AccessToken | None = None

    def apply(self, token: AccessToken) -> None:
        self._tts._opts.api_key = token.token  # plugin has no public setter for credentials
        pool = getattr(self._tts, "_pool", None)
        if pool is not None:
            pool.invalidate()  # new websocket connections authenticate with the new token
        self.current = token

    async def start(self) -> None:
        self.apply(await mint_access_token(self._api_key, ttl_s=self._ttl_s, client=self._client))
        log.info("cartesia access token issued (tts grant, ttl=%ss)", self._ttl_s)
        self._task = asyncio.create_task(self._run(), name="cartesia-token-refresh")

    async def _run(self) -> None:
        backoff = 5.0
        while True:
            assert self.current is not None
            wait = max(1.0, self.current.expires_at - time.monotonic() - self._margin_s)
            await asyncio.sleep(wait)
            try:
                self.apply(await mint_access_token(self._api_key, ttl_s=self._ttl_s, client=self._client))
                self.refreshes += 1
                backoff = 5.0
                log.info("cartesia access token refreshed")
            except CartesiaAuthError as exc:
                log.error("cartesia token refresh failed (%s, status=%s, request_id=%s); retrying in %.0fs",
                          exc.category, exc.status, exc.request_id, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
