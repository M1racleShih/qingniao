"""Stable API error codes shared by the control API, proxy and CLI."""

from __future__ import annotations

INVALID_JSON = "invalid_json"
INVALID_REQUEST = "invalid_request"
INVALID_PAGINATION = "invalid_pagination"
INVALID_CONFIG = "invalid_config"
INVALID_ADMIN_TOKEN = "invalid_admin_token"
INSTANCE_TOKEN_NOT_ADMIN = "instance_token_not_admin"
INVALID_INSTANCE_TOKEN = "invalid_instance_token"
INSTANCE_ENDED = "instance_ended"
INSTANCE_EXPIRED = "instance_expired"
INSTANCE_NOT_FOUND = "instance_not_found"
AMBIGUOUS_LABEL = "ambiguous_label"
UNKNOWN_MODEL = "unknown_model"
NO_DEFAULT_MODEL = "no_default_model"
INVALID_SELECTION = "invalid_selection"
REVISION_CONFLICT = "revision_conflict"
CREDENTIAL_MISSING = "credential_missing"
CREDENTIAL_INVALID = "credential_invalid"
CREDENTIAL_UNREADABLE = "credential_unreadable"
CONFIG_PERSIST_FAILED = "config_persist_failed"
GENERATION_CONFLICT = "generation_conflict"
TRANSACTION_DAMAGED = "transaction_damaged"
UPSTREAM_ERROR = "upstream_error"
GATEWAY_LOCKED = "gateway_locked"
CONFIG_LOAD_FAILED = "config_load_failed"


class ApiError(Exception):
    """Error with a stable code, an HTTP status and optional details."""

    def __init__(self, status_code: int, code: str, message: str, **details):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def to_json(self) -> dict:
        error: dict = {"code": self.code, "message": self.message}
        error.update(self.details)
        return {"error": error}
