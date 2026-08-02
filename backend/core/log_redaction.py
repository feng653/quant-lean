"""Defense-in-depth redaction for credentials accidentally placed in URLs."""

from __future__ import annotations

import logging
import re
from typing import Any

_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:token|access_token|authorization)=)([^&\s\"']*)"
)
_JWT_VALUE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
)
_FORMAT_PLACEHOLDER = re.compile(r"%[-+#0 ]*(?:\d+|\*)?(?:\.\d+)?[a-zA-Z]")


def redact_sensitive_url_values(value: str) -> str:
    """Redact credential-like query values without hiding the request path."""
    def replace(match: re.Match[str]) -> str:
        query_value = match.group(2)
        # Preserve lazy logging placeholders.  Their matching argument is
        # redacted separately, so the format string remains valid.
        if _FORMAT_PLACEHOLDER.fullmatch(query_value):
            return match.group(0)
        return f"{match.group(1)}<redacted>"

    return _SENSITIVE_QUERY_VALUE.sub(replace, value)


class SensitiveUrlFilter(logging.Filter):
    """Sanitize URL-bearing log messages and interpolation arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_url_values(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact_value(value) for key, value in record.args.items()
            }
        return True


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        value = redact_sensitive_url_values(value)
        return _JWT_VALUE.sub("<redacted-jwt>", value)
    return value


_FILTER = SensitiveUrlFilter()


def install_sensitive_url_log_filter() -> None:
    """Install redaction on Uvicorn and all currently configured handlers."""
    for logger_name in ("uvicorn.access", "uvicorn.error", "quant_platform"):
        logger = logging.getLogger(logger_name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)

    root = logging.getLogger()
    for handler in root.handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)
