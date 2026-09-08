"""Secret and identifier generation.

Instance bearers and the control token carry at least 256 bits of randomness
(secrets.token_urlsafe(32) -> 32 bytes). Display instance IDs are separate,
non-secret values used for targeting and status output.
"""

from __future__ import annotations

import hashlib
import secrets

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_bearer_token() -> str:
    return secrets.token_urlsafe(32)


def new_control_token() -> str:
    return secrets.token_urlsafe(32)


def new_instance_id() -> str:
    return "i-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))
