"""Safe API projection for server-side model storage paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.config import settings


def _relative_storage_key(value: object, root: Path) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = Path(value)
    try:
        candidate = raw if raw.is_absolute() else root / raw
        relative = candidate.resolve(strict=False).relative_to(
            root.resolve(strict=False)
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return relative.as_posix()


def redact_model_storage_paths(item: dict[str, Any]) -> dict[str, Any]:
    """Replace local filesystem paths with non-sensitive storage keys."""

    root = settings.abs_path(settings.MODEL_STORE_DIR)
    result = dict(item)
    result["model_storage_key"] = _relative_storage_key(
        result.pop("model_file_path", None),
        root,
    )
    result["metadata_storage_key"] = _relative_storage_key(
        result.pop("metadata_file_path", None),
        root,
    )
    return result
