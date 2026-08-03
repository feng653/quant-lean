"""Recoverable, request-bound staging for long market-data validation.

Staging is deliberately outside the formal cache contract.  It can only reuse
an intact, unexpired primary-source response for the exact same request and
source adapter; downstream research readers never inspect this directory.
"""

from __future__ import annotations
from backend.core.hashing import file_sha256

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence
import uuid

import pandas as pd

from backend.data.source_validation import (
    DailyFetchResult,
    validate_daily_fetch_evidence,
)

STAGING_SCHEMA = "validated-daily-staging/v1"
_HAS_POSIX_PERMISSION_BITS = os.name != "nt"


class StagingIntegrityError(RuntimeError):
    """A staged response is expired, unsafe, incomplete, or modified."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise StagingIntegrityError("staging timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _secure_regular_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise StagingIntegrityError("staging file is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StagingIntegrityError("staging path is not a regular file")
    # Windows exposes synthesized Unix mode bits that do not represent its
    # ACL. Keep the strict 0600 check on POSIX hosts; integrity, regular-file
    # and anti-symlink checks remain mandatory on every platform.
    if _HAS_POSIX_PERMISSION_BITS and stat.S_IMODE(info.st_mode) & 0o077:
        raise StagingIntegrityError("staging file permissions are too broad")


class ValidatedDailyStaging:
    """Hash-verified primary-response checkpoint with a bounded lifetime."""

    def __init__(self, root: str | Path, *, ttl_hours: int = 48) -> None:
        if ttl_hours < 1 or ttl_hours > 168:
            raise ValueError("staging ttl_hours must be between 1 and 168")
        self.root = Path(root)
        self.ttl = timedelta(hours=ttl_hours)

    @staticmethod
    def _request(
        codes: Sequence[str],
        start: str,
        end: str,
        source_identity: Mapping[str, str],
    ) -> dict[str, Any]:
        return {
            "codes": sorted(
                {str(code).strip() for code in codes if str(code).strip()}
            ),
            "start": pd.Timestamp(start).normalize().strftime("%Y-%m-%d"),
            "end": pd.Timestamp(end).normalize().strftime("%Y-%m-%d"),
            "source_identity": dict(sorted(source_identity.items())),
        }

    @staticmethod
    def _key(request: Mapping[str, Any]) -> str:
        return hashlib.sha256(_canonical_bytes(request)).hexdigest()

    def _paths(self, request: Mapping[str, Any]) -> tuple[Path, Path]:
        key = self._key(request)
        return self.root / f"{key}.parquet", self.root / f"{key}.json"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise StagingIntegrityError("staging root is not a safe directory")
        os.chmod(self.root, 0o700)

    def _verify_existing_root(self) -> bool:
        try:
            info = self.root.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StagingIntegrityError("staging root is not a safe directory")
        if (
            _HAS_POSIX_PERMISSION_BITS
            and stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise StagingIntegrityError("staging directory permissions are too broad")
        return True

    def save(
        self,
        result: DailyFetchResult,
        *,
        codes: Sequence[str],
        start: str,
        end: str,
        source_identity: Mapping[str, str],
    ) -> None:
        evidence = validate_daily_fetch_evidence(
            result.evidence,
            frame=result.frame,
        )
        if not evidence["complete_code_coverage"] or result.frame.empty:
            raise StagingIntegrityError(
                "only a complete non-empty primary response can be staged"
            )
        request = self._request(codes, start, end, source_identity)
        data_path, metadata_path = self._paths(request)
        self._ensure_root()
        suffix = uuid.uuid4().hex
        data_temp = self.root / f".{data_path.name}.{suffix}.tmp"
        metadata_temp = self.root / f".{metadata_path.name}.{suffix}.tmp"
        try:
            result.frame.to_parquet(data_temp)
            os.chmod(data_temp, 0o600)
            # Windows rejects fsync on a read-only descriptor. Reopen the
            # already-written parquet read/write so durability is portable.
            with data_temp.open("r+b") as handle:
                os.fsync(handle.fileno())
            created = _utc_now()
            metadata: dict[str, Any] = {
                "schema_version": STAGING_SCHEMA,
                "created_at": created.isoformat(),
                "expires_at": (created + self.ttl).isoformat(),
                "request": request,
                "data_file": data_path.name,
                "data_size": data_temp.stat().st_size,
                "data_sha256": file_sha256(data_temp),
                "evidence": evidence,
            }
            metadata["content_sha256"] = hashlib.sha256(
                _canonical_bytes(metadata)
            ).hexdigest()
            descriptor = _canonical_bytes(metadata)
            file_descriptor = os.open(
                metadata_temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(file_descriptor, descriptor)
                os.fsync(file_descriptor)
            finally:
                os.close(file_descriptor)
            os.replace(data_temp, data_path)
            os.chmod(data_path, 0o600)
            os.replace(metadata_temp, metadata_path)
            os.chmod(metadata_path, 0o600)
        finally:
            for temporary in (data_temp, metadata_temp):
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def load(
        self,
        *,
        codes: Sequence[str],
        start: str,
        end: str,
        source_identity: Mapping[str, str],
    ) -> DailyFetchResult | None:
        request = self._request(codes, start, end, source_identity)
        data_path, metadata_path = self._paths(request)
        if not self._verify_existing_root():
            return None
        if not metadata_path.exists():
            return None
        _secure_regular_file(metadata_path)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StagingIntegrityError("staging metadata is unreadable") from exc
        if not isinstance(metadata, dict):
            raise StagingIntegrityError("staging metadata is not an object")
        claimed_hash = metadata.pop("content_sha256", None)
        if (
            not isinstance(claimed_hash, str)
            or len(claimed_hash) != 64
            or hashlib.sha256(_canonical_bytes(metadata)).hexdigest()
            != claimed_hash
        ):
            raise StagingIntegrityError("staging metadata hash changed")
        if metadata.get("schema_version") != STAGING_SCHEMA:
            raise StagingIntegrityError("staging schema is unsupported")
        if metadata.get("request") != request:
            raise StagingIntegrityError("staging request identity changed")
        if _parse_utc(metadata.get("expires_at")) <= _utc_now():
            raise StagingIntegrityError("staging response expired")
        if metadata.get("data_file") != data_path.name:
            raise StagingIntegrityError("staging data filename changed")
        _secure_regular_file(data_path)
        if (
            int(metadata.get("data_size", -1)) != data_path.stat().st_size
            or metadata.get("data_sha256") != file_sha256(data_path)
        ):
            raise StagingIntegrityError("staging parquet integrity check failed")
        try:
            frame = pd.read_parquet(data_path)
        except Exception as exc:
            raise StagingIntegrityError("staging parquet is unreadable") from exc
        evidence = validate_daily_fetch_evidence(
            metadata.get("evidence", {}),
            frame=frame,
        )
        if not evidence["complete_code_coverage"] or frame.empty:
            raise StagingIntegrityError("staging primary response is incomplete")
        return DailyFetchResult(frame, evidence)

    def discard(
        self,
        *,
        codes: Sequence[str],
        start: str,
        end: str,
        source_identity: Mapping[str, str],
    ) -> None:
        request = self._request(codes, start, end, source_identity)
        for path in self._paths(request):
            try:
                if path.is_symlink():
                    raise StagingIntegrityError(
                        "refusing to remove symlinked staging path"
                    )
                path.unlink()
            except FileNotFoundError:
                pass
