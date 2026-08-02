"""Append-only evidence for licensed/public provider candidate observations.

Provider responses recorded here are deliberately *not* PIT master data.  The
store gives reviewers reproducible bytes and bitemporal declarations while
keeping every observation quarantined until the existing governance workflow
has independent evidence, licence approval, and a reviewed import package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


PROVIDER_ARTIFACT_SCHEMA = "provider-candidate-artifact/v1"
PROVIDER_RUN_SCHEMA = "provider-candidate-run/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"(?:authorization|api[_-]?key|password|secret|token)", re.IGNORECASE
)
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class ProviderArtifactError(RuntimeError):
    """Candidate evidence is unsafe, malformed, or has changed."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderArtifactError("candidate evidence is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _assert_no_secret(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            if _SECRET_KEY.search(key_text):
                raise ProviderArtifactError(
                    f"credential-like field is forbidden in evidence: {path}.{key_text}"
                )
            _assert_no_secret(nested, path=f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_no_secret(nested, path=f"{path}[{index}]")


def _validate_timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderArtifactError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderArtifactError(f"{field} must contain a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_component(value: str, field: str) -> str:
    normalized = str(value).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", normalized):
        raise ProviderArtifactError(f"{field} is invalid")
    return normalized


def build_candidate_artifact_manifest(
    *,
    provider: str,
    dataset: str,
    endpoint: str,
    request: Mapping[str, Any],
    response_payload: bytes,
    response_fields: Sequence[str],
    row_count: int,
    ingested_at: str,
    temporal_contract: Mapping[str, Any],
    temporal_coverage: Mapping[str, int] | None = None,
    provider_revision: str | None = None,
    licence_status: str = "unverified",
) -> dict[str, Any]:
    """Bind exact response bytes to a fail-closed candidate declaration."""

    provider = _safe_component(provider, "provider")
    dataset = _safe_component(dataset, "dataset")
    if licence_status not in {
        "unverified",
        "approved_local_research_retention",
        "retention_prohibited",
    }:
        raise ProviderArtifactError("licence_status is invalid")
    endpoint = str(endpoint).strip()
    if not endpoint.startswith("https://"):
        raise ProviderArtifactError("provider endpoint must use HTTPS")
    if not response_payload or len(response_payload) > _MAX_RESPONSE_BYTES:
        raise ProviderArtifactError("provider response size is invalid")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise ProviderArtifactError("row_count is invalid")
    fields = [str(item).strip() for item in response_fields]
    if not fields or any(not field for field in fields) or len(fields) != len(set(fields)):
        raise ProviderArtifactError("response fields are invalid")
    sanitized_request = dict(request)
    temporal = dict(temporal_contract)
    coverage = {
        str(field): int(count)
        for field, count in (temporal_coverage or {}).items()
    }
    if any(count < 0 or count > row_count for count in coverage.values()):
        raise ProviderArtifactError("temporal field coverage is invalid")
    _assert_no_secret(sanitized_request)
    _assert_no_secret(temporal)
    normalized_ingested_at = _validate_timestamp(ingested_at, "ingested_at")
    effective_at = temporal.get("effective_at")
    available_at = temporal.get("available_at")
    if not isinstance(effective_at, Mapping) or not isinstance(available_at, Mapping):
        raise ProviderArtifactError(
            "temporal contract requires effective_at and available_at declarations"
        )
    if available_at.get("evidence") not in {
        "provider_field",
        "declared_ingestion_time",
    }:
        raise ProviderArtifactError("available_at evidence declaration is invalid")
    digest = hashlib.sha256(response_payload).hexdigest()
    revision_value = str(provider_revision or digest).strip()
    if not revision_value or len(revision_value) > 256 or "\n" in revision_value:
        raise ProviderArtifactError("provider revision is invalid")
    revision_evidence = "provider_field" if provider_revision else "declared_observation"
    blockers = [
        "candidate_quarantine_only",
        "independent_authoritative_evidence_required",
        "reviewed_governed_import_required",
    ]
    if licence_status != "approved_local_research_retention":
        blockers.append("provider_retention_terms_unverified")
    if available_at.get("evidence") != "provider_field":
        blockers.append("provider_available_at_missing")
    elif any(
        coverage.get(str(field), 0) < row_count
        for field in available_at.get("fields", [])
    ):
        blockers.append("available_at_row_coverage_incomplete")
    payload: dict[str, Any] = {
        "schema_version": PROVIDER_ARTIFACT_SCHEMA,
        "provider": provider,
        "dataset": dataset,
        "endpoint": endpoint,
        "request": sanitized_request,
        "response": {
            "content_sha256": digest,
            "size_bytes": len(response_payload),
            "fields": fields,
            "row_count": row_count,
        },
        "bitemporal": {
            **temporal,
            "field_non_null_counts": coverage,
            "ingested_at": normalized_ingested_at,
            "revision": {
                "value": revision_value,
                "evidence": revision_evidence,
            },
        },
        "licence_status": licence_status,
        "classification": "quarantine",
        "promotion": {
            "eligible": False,
            "blockers": sorted(blockers),
        },
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    return payload


def verify_candidate_artifact_manifest(
    manifest: Mapping[str, Any], response_payload: bytes
) -> dict[str, Any]:
    payload = dict(manifest)
    manifest_hash = payload.pop("manifest_sha256", None)
    if not isinstance(manifest_hash, str) or not _SHA256.fullmatch(manifest_hash):
        raise ProviderArtifactError("candidate manifest digest is invalid")
    if canonical_sha256(payload) != manifest_hash:
        raise ProviderArtifactError("candidate manifest digest changed")
    if payload.get("schema_version") != PROVIDER_ARTIFACT_SCHEMA:
        raise ProviderArtifactError("candidate manifest schema is unsupported")
    _assert_no_secret(payload)
    response = payload.get("response")
    if not isinstance(response, Mapping):
        raise ProviderArtifactError("candidate response evidence is missing")
    if response.get("content_sha256") != hashlib.sha256(response_payload).hexdigest():
        raise ProviderArtifactError("candidate response bytes changed")
    if response.get("size_bytes") != len(response_payload):
        raise ProviderArtifactError("candidate response size changed")
    promotion = payload.get("promotion")
    if (
        payload.get("classification") != "quarantine"
        or not isinstance(promotion, Mapping)
        or promotion.get("eligible") is not False
    ):
        raise ProviderArtifactError("candidate artifact may not be promoted")
    return dict(manifest)


class ContentAddressedProviderArtifactStore:
    """Content-address exact bytes and immutable candidate manifests."""

    def __init__(self, root: str | Path) -> None:
        # ``resolve()`` would silently follow an attacker-controlled evidence
        # root symlink. Keep the lexical absolute path and validate each
        # managed component with lstat before using it.
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self.artifact_root = self.root / "artifacts" / "sha256"
        self.manifest_root = self.root / "manifests" / "sha256"
        self.report_root = self.root / "reports" / "sha256"

        self._ensure_secure_directory(self.root)
        for path in (self.artifact_root, self.manifest_root, self.report_root):
            self._ensure_secure_directory(path)

    @staticmethod
    def _ensure_secure_directory_node(
        directory: Path,
        *,
        create_parents: bool = False,
    ) -> None:
        """Create/repair exactly one managed directory without following links."""

        if not directory.exists():
            try:
                directory.mkdir(parents=create_parents, mode=0o700)
            except OSError as exc:
                raise ProviderArtifactError(
                    "candidate evidence directory is unavailable"
                ) from exc
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise ProviderArtifactError(
                "candidate evidence directory is unavailable"
            ) from exc
        if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ProviderArtifactError("candidate evidence directory is unsafe")
        try:
            os.chmod(directory, stat.S_IRWXU)
            mode = stat.S_IMODE(directory.lstat().st_mode)
        except OSError as exc:
            raise ProviderArtifactError(
                "candidate evidence directory permissions are unsafe"
            ) from exc
        if mode != stat.S_IRWXU:
            raise ProviderArtifactError(
                "candidate evidence directory permissions are unsafe"
            )

    def _ensure_secure_directory(self, path: Path) -> None:
        """Secure every component from evidence root through ``path``."""

        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ProviderArtifactError("candidate evidence path escaped root") from exc
        self._ensure_secure_directory_node(self.root, create_parents=True)
        current = self.root
        for component in relative.parts:
            current = current / component
            self._ensure_secure_directory_node(current)

    def _ensure_secure_target_parent(self, target: Path) -> None:
        self._ensure_secure_directory(target.parent)

    @staticmethod
    def _ensure_secure_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ProviderArtifactError("candidate evidence object is unavailable") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ProviderArtifactError("candidate evidence object is unsafe")
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            mode = stat.S_IMODE(path.lstat().st_mode)
        except OSError as exc:
            raise ProviderArtifactError(
                "candidate evidence object permissions are unsafe"
            ) from exc
        if mode != (stat.S_IRUSR | stat.S_IWUSR):
            raise ProviderArtifactError(
                "candidate evidence object permissions are unsafe"
            )

    @staticmethod
    def _target(root: Path, digest: str, suffix: str = "") -> Path:
        if not _SHA256.fullmatch(digest):
            raise ProviderArtifactError("content digest is invalid")
        return root / digest[:2] / f"{digest}{suffix}"

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        self._ensure_secure_target_parent(target)
        if target.exists():
            self._ensure_secure_file(target)
            existing = target.read_bytes()
            if existing != payload:
                raise ProviderArtifactError("content-addressed object changed")
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            self._ensure_secure_file(target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def record(
        self, *, response_payload: bytes, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        verified = verify_candidate_artifact_manifest(manifest, response_payload)
        response_digest = str(verified["response"]["content_sha256"])
        manifest_digest = str(verified["manifest_sha256"])
        self._atomic_write(
            self._target(self.artifact_root, response_digest), response_payload
        )
        self._atomic_write(
            self._target(self.manifest_root, manifest_digest, ".json"),
            canonical_json_bytes(verified),
        )
        return {
            "artifact_sha256": response_digest,
            "manifest_sha256": manifest_digest,
            "classification": "quarantine",
        }

    def read(self, manifest_digest: str) -> tuple[dict[str, Any], bytes]:
        manifest_path = self._target(self.manifest_root, manifest_digest, ".json")
        try:
            self._ensure_secure_target_parent(manifest_path)
            self._ensure_secure_file(manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            response_digest = str(manifest["response"]["content_sha256"])
            response_path = self._target(self.artifact_root, response_digest)
            self._ensure_secure_target_parent(response_path)
            self._ensure_secure_file(response_path)
            response = response_path.read_bytes()
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderArtifactError("candidate artifact is unavailable") from exc
        return verify_candidate_artifact_manifest(manifest, response), response

    def record_report(self, report: Mapping[str, Any]) -> str:
        payload = dict(report)
        _assert_no_secret(payload)
        encoded = canonical_json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        self._atomic_write(self._target(self.report_root, digest, ".json"), encoded)
        return digest
