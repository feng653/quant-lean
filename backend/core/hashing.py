"""统一哈希/指纹工具（v0.5.0 去重：全项目唯一实现）。

语义与历史主流实现保持一致：
- canonical JSON：``json.dumps(..., allow_nan=False, ensure_ascii=False,
  separators=(",", ":"), sort_keys=True)`` 后 UTF-8 编码。
- 内容指纹：sha256 hexdigest。
- 文件指纹：分块读取（1 MiB chunk），不整载入内存。

任何新代码必须从本模块导入，不得再手写私有实现。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


class HashingError(ValueError):
    """canonical JSON 序列化或指纹计算失败。"""


def canonical_bytes(value: Any) -> bytes:
    """Deterministic canonical UTF-8 JSON bytes (keys sorted)."""
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HashingError(
            "canonical JSON requires finite, JSON-serialisable values"
        ) from exc


def canonical_json(value: Any) -> str:
    """Deterministic canonical JSON string (keys sorted)."""
    return canonical_bytes(value).decode("utf-8")


def content_sha256(value: Mapping[str, Any] | Any) -> str:
    """sha256 hexdigest of canonical JSON bytes."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def text_sha256(value: str) -> str:
    """sha256 hexdigest of UTF-8 text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """sha256 hexdigest of a file, read in 1 MiB chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
