"""Actor identity for the prototype: persisted principals and opaque bearer tokens (I02b).

- A token is ``cct_`` + ``secrets.token_urlsafe(32)``: 32 random bytes (256 bits), about 47 characters.
  Only ``sha256(token)`` is stored. A plain hash is appropriate because the secret is high-entropy random data,
  not a user-chosen password; the digest is only a lookup key.
- The service credential (COCOON_SERVICE_TOKEN) is compared separately in constant time and is never stored.
- Expiry and revocation are checked on every protected request against the wall-clock UTC time, never data or
  replay time and never a client timestamp.

This is a local prototype mechanism, not an identity provider: no passwords, OAuth, SSO or JWTs.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

TOKEN_PREFIX = "cct_"
TOKEN_RANDOM_BYTES = 32
MAX_CREDENTIAL_LENGTH = 256
MIN_TTL = timedelta(minutes=5)
MAX_TTL = timedelta(days=30)
DEFAULT_TTL = timedelta(hours=12)

ROLE_SCOPES: dict[str, tuple[str, ...]] = {
    "operator": ("me:read", "sessions:own"),
    "supervisor": ("me:read",),  # no site grants exist yet (DG-01): a supervisor can only describe itself
}

PrincipalKind = Literal["service", "operator", "supervisor"]


@dataclass(frozen=True)
class Principal:
    """The immutable, server-resolved caller of one request."""

    kind: PrincipalKind
    subject_id: str
    operator_id: str | None = None
    display_name: str | None = None
    token_id: str | None = None
    scopes: frozenset[str] = frozenset()
    expires_at: datetime | None = None

    def has_scope(self, scope: str) -> bool:
        return self.kind == "service" or scope in self.scopes


SERVICE_PRINCIPAL = Principal(kind="service", subject_id="service")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_time(value: datetime) -> str:
    """Fixed-width UTC timestamp so stored values also compare correctly as text."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_RANDOM_BYTES)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_ttl(ttl: timedelta) -> timedelta:
    if not MIN_TTL <= ttl <= MAX_TTL:
        raise ValueError(f"token lifetime must be between {MIN_TTL} and {MAX_TTL}")
    return ttl


def write_token_file(path: Path, token: str) -> None:
    """Create `path` exclusively and write the token once. Refuses to overwrite an existing file.

    On POSIX the file is created with mode 0600. On Windows `os.open` mode bits only control the read-only flag,
    so access depends on the folder's inherited ACL: write it inside your own user profile, never a shared folder.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write((token + "\n").encode("ascii"))
