"""v0.5.0 去重：统一哈希工具的单测与语义锁定。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.core.hashing import (
    HashingError,
    canonical_bytes,
    canonical_json,
    content_sha256,
    file_sha256,
    text_sha256,
)


class TestCanonical:
    def test_keys_sorted_and_compact(self):
        raw = {"b": 1, "a": 2}
        assert canonical_json(raw) == '{"a":2,"b":1}'
        assert canonical_json({"a": 1}) == '{"a":1}'

    def test_non_ascii_kept(self):
        assert canonical_json({"k": "中文"}) == '{"k":"中文"}'

    def test_nan_rejected(self):
        with pytest.raises(HashingError):
            canonical_bytes({"v": float("nan")})

    def test_deterministic(self):
        a = canonical_bytes({"x": [3, 2, 1], "y": {"z": True}})
        b = canonical_bytes({"y": {"z": True}, "x": [3, 2, 1]})
        assert a == b


class TestDigests:
    def test_content_sha256_matches_reference(self):
        value = {"a": 1, "b": [1, 2]}
        expected = hashlib.sha256(canonical_bytes(value)).hexdigest()
        assert content_sha256(value) == expected

    def test_text_sha256(self):
        assert text_sha256("hello") == hashlib.sha256(b"hello").hexdigest()

    def test_file_sha256_matches_reference(self, tmp_path: Path):
        f = tmp_path / "payload.bin"
        f.write_bytes(b"x" * (2 * 1024 * 1024) + b"tail")
        assert file_sha256(f) == hashlib.sha256(f.read_bytes()).hexdigest()

    def test_file_sha256_matches_legacy_impl(self, tmp_path: Path):
        # 与历史 11 份文件哈希实现语义逐字节一致
        f = tmp_path / "legacy.bin"
        f.write_bytes(b"legacy-payload-123")
        digest = hashlib.sha256()
        with f.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        assert file_sha256(f) == digest.hexdigest()
