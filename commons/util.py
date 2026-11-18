from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="microseconds")


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def in_future(**kw) -> str:
    return iso(now() + timedelta(**kw))


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12).replace('-', 'x').replace('_', 'y')}"


def new_token(prefix: str = "nct") -> str:
    """High-entropy secret (256 bits). Only its SHA-256 hash is ever stored."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


@dataclass(frozen=True)
class Principal:
    """Who is acting. Derived ONLY from verified credentials, never from message content."""

    type: str  # 'agent' | 'human' | 'system'
    id: str
    owner_id: str | None = None
    display_name: str = ""
    is_simulated: bool = False
    credential_id: str | None = None
    credential_kind: str | None = None
    oauth_client_id: str | None = None
    extra: dict = field(default_factory=dict, compare=False, hash=False)


SYSTEM = Principal(type="system", id="commons-core", display_name="Commons Core")
