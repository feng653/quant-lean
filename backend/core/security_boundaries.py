"""Reusable fail-closed boundaries for durable and public payloads.

Research jobs are long lived and their database rows are user-visible.  Raw
exceptions, local paths, credentials and unbounded JSON therefore must not be
allowed to cross from a worker into durable state or an API response.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "passphrase",
    "private_key",
    "secret",
    "session",
    "cookie",
    "token",
)
PATH_KEY_PARTS = (
    "path",
    "directory",
    "checkpoint",
    "review_queue",
    "coverage_report",
)

_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|api_key|apikey|"
    r"authorization|password|secret)=)([^&\s\"']*)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|passphrase|secret|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|authorization|cookie)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
)
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:Users|home|private|tmp|var|opt|etc|Volumes|srv)"
    r"(?:/[^\s\"'<>:;,\]\[(){}]+)+"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/](?:[^\s\"'<>|:;,\]\[(){}]+[\\/]?)+"
)


class UnsafePayloadError(ValueError):
    """A value is unsafe or too large for durable job state."""

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}: {field}")


def sanitize_diagnostic(value: Any, *, max_length: int = 8192) -> str:
    """Return a bounded diagnostic without credentials or host-local paths."""

    text = str(value or "")
    text = _SENSITIVE_QUERY_VALUE.sub(r"\1<redacted>", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _SENSITIVE_ASSIGNMENT.sub(r"\1=<redacted>", text)
    text = _JWT.sub("<redacted-jwt>", text)
    text = _WINDOWS_ABSOLUTE_PATH.sub("<local-path>", text)
    text = _POSIX_ABSOLUTE_PATH.sub("<local-path>", text)
    if len(text) > max_length:
        return text[: max(0, max_length - 14)] + "…<truncated>"
    return text


def sanitize_public_payload(value: Any, *, _key: str = "") -> Any:
    """Recursively remove secrets and internal paths from API payloads."""

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                cleaned[key] = "***"
            elif any(part in lowered for part in PATH_KEY_PARTS):
                cleaned[key] = (
                    None if item is None else "<internal-path>"
                )
            else:
                cleaned[key] = sanitize_public_payload(item, _key=key)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize_public_payload(item, _key=_key) for item in value]
    if isinstance(value, str):
        return sanitize_diagnostic(value, max_length=8192)
    return value


def canonical_json_for_storage(
    value: Any,
    *,
    field: str,
    max_bytes: int,
    max_depth: int = 12,
    max_nodes: int = 100_000,
    forbid_sensitive_keys: bool = False,
) -> str:
    """Validate and serialize bounded finite JSON for a durable DB column."""

    nodes = 0

    def inspect(item: Any, path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise UnsafePayloadError("payload_node_limit_exceeded", field)
        if depth > max_depth:
            raise UnsafePayloadError("payload_depth_limit_exceeded", field)
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise UnsafePayloadError("payload_non_finite_number", path)
            return
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                if not isinstance(raw_key, str):
                    raise UnsafePayloadError("payload_key_must_be_string", path)
                if len(raw_key) > 128:
                    raise UnsafePayloadError("payload_key_too_long", path)
                if forbid_sensitive_keys and any(
                    part in raw_key.lower() for part in SENSITIVE_KEY_PARTS
                ):
                    raise UnsafePayloadError("payload_secret_key_forbidden", path)
                inspect(child, f"{path}.{raw_key}", depth + 1)
            return
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for index, child in enumerate(item):
                inspect(child, f"{path}[{index}]", depth + 1)
            return
        raise UnsafePayloadError("payload_type_forbidden", path)

    inspect(value, field, 0)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise UnsafePayloadError("payload_not_canonical_json", field) from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise UnsafePayloadError("payload_size_limit_exceeded", field)
    return encoded
