"""Secret and identifier generation.

Instance bearers and the control token carry at least 256 bits of randomness
(secrets.token_urlsafe(32) -> 32 bytes) behind a reserved ``qn_`` prefix, so
import tooling can recognize gateway-owned tokens and refuse to store them as
upstream credentials. Display instance IDs are separate, non-secret values
used for targeting and status output. Credential and operation ids use
restricted, length-bounded alphabets that contain no user input.
"""

from __future__ import annotations

import hashlib
import re
import secrets

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

TOKEN_PREFIX = "qn_"
CREDENTIAL_ID_RE = re.compile(r"\Acred_[0-9a-f]{32}\Z")
OPERATION_ID_RE = re.compile(r"\Aop_[0-9a-f]{32}\Z")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_bearer_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_control_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_instance_id() -> str:
    return "i-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))


def new_credential_id() -> str:
    return "cred_" + secrets.token_hex(16)


def new_operation_id() -> str:
    return "op_" + secrets.token_hex(16)
