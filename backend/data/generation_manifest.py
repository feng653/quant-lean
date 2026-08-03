"""Fail-closed atomic publication of a coherent multi-file cache generation.

``rename(2)`` only makes one path atomic.  A Parquet cache and its JSON
provenance therefore cannot safely be published by replacing each pathname in
turn: another process can bind the new Parquet to the old provenance.  This
module publishes immutable generation directories first, then atomically
replaces one small manifest which names every artifact in that generation.

Readers validate the manifest and every artifact before returning paths.  A
failed or interrupted publish leaves an unreferenced generation behind, which
is deliberately invisible.  Cleanup is a separate retention operation and
must never remove the active generation.
"""

from __future__ import annotations
from backend.core.hashing import file_sha256

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Mapping


GENERATION_MANIFEST_SCHEMA = "cache-generation-manifest/v1"
_IDENTIFIER = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_GENERATION = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GenerationManifestError(RuntimeError):
    """A generation is absent, malformed, tampered with, or incomplete."""


@dataclass(frozen=True)
class GenerationView:
    """Verified, immutable artifact paths selected by one manifest read."""

    generation_id: str
    artifacts: Mapping[str, Path]
    manifest_sha256: str


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the host supports directory fsync."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        # Windows cannot open directories this way.  os.replace remains atomic
        # there; durability is delegated to the platform filesystem.
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            return
    finally:
        os.close(descriptor)


class GenerationManifestStore:
    """Publish and read immutable artifact generations under one root.

    The input files supplied to :meth:`publish_staged` must live on the same
    filesystem as ``root`` so their installation is an atomic rename.  Callers
    retain ownership of abandoned staging files on failure; successfully moved
    files are owned by this store.
    """

    def __init__(self, root: Path | str, *, required_artifacts: set[str]) -> None:
        if not required_artifacts or any(
            _IDENTIFIER.fullmatch(name) is None for name in required_artifacts
        ):
            raise ValueError("generation artifact names are invalid")
        self.root = Path(root)
        self.required_artifacts = frozenset(required_artifacts)
        self._generations_root = self.root / "generations"
        self._manifests_root = self.root / "generation-manifests"
        self._generations_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._manifests_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _identifier(value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise GenerationManifestError("generation identifier is invalid")
        return value

    def _manifest_path(self, identifier: str) -> Path:
        return self._manifests_root / f"{self._identifier(identifier)}.json"

    def _generation_root(self, identifier: str, generation_id: str) -> Path:
        if _GENERATION.fullmatch(generation_id) is None:
            raise GenerationManifestError("generation id is invalid")
        return self._generations_root / self._identifier(identifier) / generation_id

    @staticmethod
    def _assert_regular(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise GenerationManifestError("generation artifact is unavailable") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise GenerationManifestError("generation artifact must be a regular file")

    @staticmethod
    def _write_file(path: Path, payload: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                written += os.write(descriptor, view[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _install_staged_artifact(self, source: Path, target: Path) -> None:
        """Move one fully-written staged file.  Kept overridable for tests."""

        self._assert_regular(source)
        os.replace(source, target)
        self._assert_regular(target)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())

    def publish_staged(
        self,
        identifier: str,
        staged: Mapping[str, Path],
    ) -> GenerationView:
        """Atomically make a complete staged artifact set the active view.

        Any exception before the manifest replacement leaves the previous
        manifest untouched.  The new directory is intentionally retained for
        diagnosis/retention but cannot be observed by normal readers.
        """

        identifier = self._identifier(identifier)
        if set(staged) != self.required_artifacts:
            raise GenerationManifestError("generation artifact set is incomplete")
        generation_id = uuid.uuid4().hex
        generation_root = self._generation_root(identifier, generation_id)
        generation_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        generation_root.mkdir(mode=0o700)
        artifacts: dict[str, dict[str, object]] = {}
        try:
            for name in sorted(self.required_artifacts):
                source = Path(staged[name])
                target = generation_root / name
                self._install_staged_artifact(source, target)
                size = target.stat().st_size
                if size <= 0:
                    raise GenerationManifestError("generation artifact is empty")
                artifacts[name] = {
                    "relative_path": str(target.relative_to(self.root)),
                    "size_bytes": size,
                    "sha256": file_sha256(target),
                }
            _fsync_directory(generation_root)
            _fsync_directory(generation_root.parent)

            manifest = {
                "schema_version": GENERATION_MANIFEST_SCHEMA,
                "identifier": identifier,
                "generation_id": generation_id,
                "artifacts": artifacts,
            }
            payload = _canonical_bytes(manifest)
            manifest_path = self._manifest_path(identifier)
            temporary = manifest_path.with_name(
                f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                self._write_file(temporary, payload)
                os.replace(temporary, manifest_path)
                _fsync_directory(self._manifests_root)
            finally:
                temporary.unlink(missing_ok=True)
            return GenerationView(
                generation_id=generation_id,
                artifacts={name: generation_root / name for name in artifacts},
                manifest_sha256=hashlib.sha256(payload).hexdigest(),
            )
        except Exception:
            # Do not delete the generation: a failed cleanup must never turn a
            # publish failure into an accidental deletion of an active view.
            raise

    def load(self, identifier: str) -> GenerationView | None:
        """Return one fully validated active generation, or ``None`` if absent."""

        identifier = self._identifier(identifier)
        manifest_path = self._manifest_path(identifier)
        if not manifest_path.exists():
            return None
        self._assert_regular(manifest_path)
        try:
            payload = manifest_path.read_bytes()
            manifest = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GenerationManifestError("generation manifest is unreadable") from exc
        if not isinstance(manifest, dict):
            raise GenerationManifestError("generation manifest is invalid")
        generation_id = manifest.get("generation_id")
        artifacts = manifest.get("artifacts")
        if (
            manifest.get("schema_version") != GENERATION_MANIFEST_SCHEMA
            or manifest.get("identifier") != identifier
            or not isinstance(generation_id, str)
            or _GENERATION.fullmatch(generation_id) is None
            or not isinstance(artifacts, dict)
            or set(artifacts) != self.required_artifacts
        ):
            raise GenerationManifestError("generation manifest contract is invalid")
        generation_root = self._generation_root(identifier, generation_id)
        resolved: dict[str, Path] = {}
        for name in sorted(self.required_artifacts):
            row = artifacts.get(name)
            if not isinstance(row, dict):
                raise GenerationManifestError("generation artifact descriptor is invalid")
            expected = generation_root / name
            relative = str(expected.relative_to(self.root))
            digest = row.get("sha256")
            try:
                size = int(row.get("size_bytes"))
            except (TypeError, ValueError) as exc:
                raise GenerationManifestError("generation artifact size is invalid") from exc
            if (
                row.get("relative_path") != relative
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
                or size <= 0
            ):
                raise GenerationManifestError("generation artifact descriptor is invalid")
            self._assert_regular(expected)
            if expected.stat().st_size != size or file_sha256(expected) != digest:
                raise GenerationManifestError("generation artifact integrity check failed")
            resolved[name] = expected
        return GenerationView(
            generation_id=generation_id,
            artifacts=resolved,
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
        )
