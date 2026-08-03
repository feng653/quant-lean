"""Encrypted, auditable local backups and isolated restore verification.

The production wrapper fixes all paths.  This module keeps the implementation
testable while enforcing the same path, permission, archive, and integrity
boundaries for every caller.
"""

from __future__ import annotations
from backend.core.hashing import file_sha256

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import struct
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_MAGIC = b"QPBACKUP1\n"
_SCHEMA = "quant-platform-encrypted-backup/v1"
_MANIFEST_SCHEMA = "quant-platform-backup-manifest/v1"
_CHUNK_SIZE = 1024 * 1024
_MIN_PASSPHRASE_BYTES = 24
_HAS_POSIX_PERMISSION_SECURITY = os.name == "posix"
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_REQUIRED_DATABASES = (
    "users.db",
    "experiment.db",
    "trading_sim.db",
    "jobs.db",
)
_OPTIONAL_DATABASES = ("trading_live.db",)
_EVIDENCE_DIRECTORIES = (
    "data/research_snapshots",
    "data/models",
)
_CONFIG_FILES = (
    "deploy/macos/Caddyfile.secure",
    "deploy/macos/com.quant-platform.backend.plist",
    "deploy/macos/com.quant-platform.frontend.plist",
    "deploy/macos/com.quant-platform.backup.plist",
    "deploy/macos/com.quant-platform.watchdog.plist",
    "deploy/macos/quant-platform-backup.sh",
    "deploy/macos/quant-platform-backup-repository.sh",
    "deploy/macos/quant-platform-restore-drill.sh",
    "deploy/macos/quant-platform-watchdog.sh",
)


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot satisfy its safety contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _metadata_is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _is_link_or_reparse_point(path: Path) -> bool:
    """Return whether an existing path redirects filesystem traversal.

    ``Path.is_symlink`` does not cover every Windows reparse-point type
    (notably directory junctions on older Python versions).  ``lstat`` keeps
    the metadata for the path entry itself so Windows can reject all reparse
    points without invoking platform-specific shell tools.
    """
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return _metadata_is_link_or_reparse_point(metadata)


def _require_not_link_or_reparse_point(path: Path, *, message: str) -> None:
    if _is_link_or_reparse_point(path):
        raise BackupError(message)


def _require_private_posix_mode(
    metadata: os.stat_result,
    *,
    prohibited_bits: int,
    message: str,
) -> None:
    """Enforce Unix confidentiality bits only where they have security meaning.

    Windows ``st_mode`` permission bits are synthesized and ``chmod`` only
    controls a limited read-only flag.  Treating those bits as an ACL check
    rejects safe paths while proving nothing about their Windows DACL.
    """
    if (
        _HAS_POSIX_PERMISSION_SECURITY
        and stat.S_IMODE(metadata.st_mode) & prohibited_bits
    ):
        raise BackupError(message)


def _validate_absolute_directory(
    path: Path,
    *,
    label: str,
    create: bool = False,
) -> Path:
    if not path.is_absolute():
        raise BackupError(f"{label} must be an absolute path")
    _require_not_link_or_reparse_point(
        path,
        message=f"{label} must not be a symlink or reparse point",
    )
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_not_link_or_reparse_point(
            path,
            message=f"{label} must not be a symlink or reparse point",
        )
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise BackupError(f"{label} must be a directory")
    return resolved


def _validate_private_directory(path: Path, *, label: str) -> Path:
    resolved = _validate_absolute_directory(path, label=label, create=True)
    _require_private_posix_mode(
        resolved.stat(),
        prohibited_bits=0o077,
        message=f"{label} permissions must be 0700 or stricter",
    )
    return resolved


def read_passphrase_file(path: Path) -> bytes:
    """Read a private, non-link passphrase file without logging its value."""
    if not path.is_absolute():
        raise BackupError("passphrase file must be an absolute path")
    _require_not_link_or_reparse_point(
        path,
        message="passphrase file must not be a symlink or reparse point",
    )
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupError("passphrase file must be a regular file")
    _require_private_posix_mode(
        metadata,
        prohibited_bits=0o077,
        message="passphrase file permissions must be 0600 or stricter",
    )
    if (
        _HAS_POSIX_PERMISSION_SECURITY
        and metadata.st_uid not in {0, os.geteuid()}
    ):
        raise BackupError("passphrase file must be owned by the current user or root")
    with resolved.open("rb") as stream:
        passphrase = stream.readline().rstrip(b"\r\n")
        if stream.read(1):
            raise BackupError("passphrase file must contain exactly one line")
    if len(passphrase) < _MIN_PASSPHRASE_BYTES:
        raise BackupError(
            f"backup passphrase must contain at least {_MIN_PASSPHRASE_BYTES} bytes"
        )
    return passphrase


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    return Scrypt(
        salt=salt,
        length=32,
        n=2**15,
        r=8,
        p=1,
    ).derive(passphrase)


def _copy_regular_file(source: Path, target: Path) -> None:
    _require_not_link_or_reparse_point(
        source,
        message=f"backup scope contains a symlink or reparse point: {source.name}",
    )
    metadata = source.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupError(f"backup scope contains a non-regular file: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, target)
    target.chmod(0o600)


def _copy_evidence_tree(source: Path, target: Path) -> None:
    _require_not_link_or_reparse_point(
        source,
        message=(
            "evidence directory must not be a symlink or reparse point: "
            f"{source.name}"
        ),
    )
    def copy_directory(directory: Path, destination_root: Path) -> None:
        # Do not use rglob here.  On older Python versions a Windows junction
        # may be descended before the caller gets an opportunity to inspect
        # its reparse-point attributes.
        for child in sorted(directory.iterdir()):
            relative = child.relative_to(source)
            metadata = child.lstat()
            if _metadata_is_link_or_reparse_point(metadata):
                raise BackupError(
                    "evidence tree contains a symlink or reparse point: "
                    f"{relative}"
                )
            destination = destination_root / child.name
            if stat.S_ISDIR(metadata.st_mode):
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                copy_directory(child, destination)
            elif stat.S_ISREG(metadata.st_mode):
                _copy_regular_file(child, destination)
            else:
                raise BackupError(
                    f"evidence tree contains an unsupported entry: {relative}"
                )

    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    copy_directory(source, target)


def _sqlite_snapshot(source: Path, target: Path) -> str:
    if _is_link_or_reparse_point(source) or not stat.S_ISREG(source.stat().st_mode):
        raise BackupError(f"database is not a regular file: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with (
            closing(
                sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            ) as source_connection,
            closing(sqlite3.connect(target)) as target_connection,
        ):
            source_check = source_connection.execute("PRAGMA integrity_check").fetchone()
            if source_check is None or source_check[0] != "ok":
                raise BackupError(
                    f"source database integrity check failed: {source.name}"
                )
            source_connection.backup(target_connection)
            target_connection.commit()
        target.chmod(0o600)
        with closing(
            sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        ) as verification:
            result = verification.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"database backup failed: {source.name}") from exc
    if result is None or result[0] != "ok":
        raise BackupError(f"backup database integrity check failed: {source.name}")
    return "ok"


def _git_commit(source_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    if len(commit) == 40 and all(character in "0123456789abcdef" for character in commit):
        return commit
    return None


def _manifest_entries(payload_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(payload_root.rglob("*")):
        if not path.is_file():
            continue
        entries.append(
            {
                "path": path.relative_to(payload_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return entries


def _write_tar(staging_root: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, mode="w") as archive:
        for path in sorted(staging_root.rglob("*")):
            relative = path.relative_to(staging_root)
            info = archive.gettarinfo(str(path), arcname=relative.as_posix())
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o700 if path.is_dir() else 0o600
            if path.is_file():
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)


def _encrypt_file(
    plaintext_path: Path,
    output_path: Path,
    passphrase: bytes,
    *,
    backup_id: str,
    created_at: str,
) -> dict[str, Any]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = {
        "schema": _SCHEMA,
        "backup_id": backup_id,
        "created_at": created_at,
        "cipher": "AES-256-GCM",
        "kdf": {
            "name": "scrypt",
            "n": 2**15,
            "r": 8,
            "p": 1,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "plaintext_sha256": file_sha256(plaintext_path),
    }
    header_bytes = _canonical_json(header)
    key = _derive_key(passphrase, salt)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header_bytes)
    with plaintext_path.open("rb") as source, output_path.open("xb") as target:
        target.write(_MAGIC)
        target.write(struct.pack(">I", len(header_bytes)))
        target.write(header_bytes)
        while chunk := source.read(_CHUNK_SIZE):
            target.write(encryptor.update(chunk))
        encryptor.finalize()
        target.write(encryptor.tag)
        target.flush()
        os.fsync(target.fileno())
    output_path.chmod(0o600)
    return header


def _read_header(stream: BinaryIO) -> tuple[dict[str, Any], bytes]:
    if stream.read(len(_MAGIC)) != _MAGIC:
        raise BackupError("backup magic is invalid")
    raw_length = stream.read(4)
    if len(raw_length) != 4:
        raise BackupError("backup header is truncated")
    header_length = struct.unpack(">I", raw_length)[0]
    if header_length < 32 or header_length > 64 * 1024:
        raise BackupError("backup header length is invalid")
    header_bytes = stream.read(header_length)
    if len(header_bytes) != header_length:
        raise BackupError("backup header is truncated")
    try:
        header = json.loads(header_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup header is invalid") from exc
    if not isinstance(header, dict):
        raise BackupError("backup header is invalid")
    if header.get("schema") != _SCHEMA:
        raise BackupError("backup schema is unsupported")
    return header, header_bytes


def _decrypt_file(
    encrypted_path: Path,
    plaintext_path: Path,
    passphrase: bytes,
) -> dict[str, Any]:
    if _is_link_or_reparse_point(encrypted_path) or not stat.S_ISREG(
        encrypted_path.stat().st_mode
    ):
        raise BackupError("backup archive must be a regular file")
    with encrypted_path.open("rb") as source:
        header, header_bytes = _read_header(source)
        ciphertext_offset = source.tell()
        total_size = encrypted_path.stat().st_size
        ciphertext_length = total_size - ciphertext_offset - 16
        if ciphertext_length < 1:
            raise BackupError("backup ciphertext is truncated")
        source.seek(total_size - 16)
        tag = source.read(16)
        try:
            kdf = header["kdf"]
            if (
                kdf["name"] != "scrypt"
                or kdf["n"] != 2**15
                or kdf["r"] != 8
                or kdf["p"] != 1
                or header["cipher"] != "AES-256-GCM"
            ):
                raise BackupError("backup cryptographic parameters are unsupported")
            salt = base64.b64decode(kdf["salt"], validate=True)
            nonce = base64.b64decode(header["nonce"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupError("backup cryptographic header is invalid") from exc
        if len(salt) != 16 or len(nonce) != 12:
            raise BackupError("backup salt or nonce length is invalid")
        key = _derive_key(passphrase, salt)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header_bytes)
        source.seek(ciphertext_offset)
        remaining = ciphertext_length
        try:
            with plaintext_path.open("xb") as target:
                while remaining:
                    chunk = source.read(min(_CHUNK_SIZE, remaining))
                    if not chunk:
                        raise BackupError("backup ciphertext is truncated")
                    remaining -= len(chunk)
                    target.write(decryptor.update(chunk))
                target.write(decryptor.finalize())
                target.flush()
                os.fsync(target.fileno())
        except InvalidTag as exc:
            plaintext_path.unlink(missing_ok=True)
            raise BackupError("backup authentication failed") from exc
    plaintext_path.chmod(0o600)
    if file_sha256(plaintext_path) != header.get("plaintext_sha256"):
        plaintext_path.unlink(missing_ok=True)
        raise BackupError("decrypted archive digest does not match the header")
    return header


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    names: set[str] = set()
    for member in members:
        pure = PurePosixPath(member.name)
        if (
            pure.is_absolute()
            or not pure.parts
            or ".." in pure.parts
            or member.issym()
            or member.islnk()
            or member.isdev()
            or member.name in names
        ):
            raise BackupError("backup archive contains an unsafe member")
        if not (member.isdir() or member.isfile()):
            raise BackupError("backup archive contains an unsupported member")
        names.add(member.name)
    return members


def _extract_archive(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, mode="r:") as archive:
        members = _safe_members(archive)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if not _is_relative_to(target.resolve(strict=False), destination):
                raise BackupError("backup member escapes the restore directory")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.chmod(0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise BackupError("backup regular file has no content")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=_CHUNK_SIZE)
            target.chmod(0o600)


def _load_and_verify_manifest(restore_root: Path) -> dict[str, Any]:
    manifest_path = restore_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is unavailable or invalid") from exc
    if manifest.get("schema") != _MANIFEST_SCHEMA:
        raise BackupError("backup manifest schema is unsupported")
    payload_root = restore_root / "payload"
    expected_paths: set[str] = set()
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict):
            raise BackupError("backup manifest entry is invalid")
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            raise BackupError("backup manifest path is invalid")
        pure = PurePosixPath(path_value)
        if pure.is_absolute() or ".." in pure.parts or path_value in expected_paths:
            raise BackupError("backup manifest path is unsafe")
        expected_paths.add(path_value)
        path = payload_root.joinpath(*pure.parts)
        if _is_link_or_reparse_point(path) or not path.is_file():
            raise BackupError(f"backup payload is missing: {path_value}")
        if path.stat().st_size != entry.get("size_bytes"):
            raise BackupError(f"backup payload size mismatch: {path_value}")
        if file_sha256(path) != entry.get("sha256"):
            raise BackupError(f"backup payload digest mismatch: {path_value}")
    actual_paths = {
        path.relative_to(payload_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise BackupError("backup payload contains unmanifested files")
    return manifest


def _verify_restored_databases(payload_root: Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for path in sorted((payload_root / "data").glob("*.db")):
        try:
            with closing(
                sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            ) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise BackupError(f"restored database cannot be opened: {path.name}") from exc
        if result is None or result[0] != "ok":
            raise BackupError(f"restored database integrity failed: {path.name}")
        results[path.name] = "ok"
    if not set(_REQUIRED_DATABASES).issubset(results):
        raise BackupError("restored backup is missing a required database")
    return results


def create_backup(
    source_root: Path,
    destination_directory: Path,
    passphrase: bytes,
) -> tuple[Path, dict[str, Any]]:
    """Create one encrypted, SQLite-consistent backup outside the source tree."""
    source = _validate_absolute_directory(source_root, label="source root")
    destination = _validate_private_directory(
        destination_directory,
        label="backup destination",
    )
    if _is_relative_to(destination, source):
        raise BackupError("backup destination must be outside the source tree")
    if len(passphrase) < _MIN_PASSPHRASE_BYTES:
        raise BackupError("backup passphrase is too short")
    env_path = source / ".env"
    if _is_link_or_reparse_point(env_path) or not env_path.is_file():
        raise BackupError("source .env is required and must be a regular file")

    created_at = _utc_now()
    backup_id = uuid.uuid4().hex
    filename = (
        "quant-platform-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + backup_id[:12]
        + ".qpbak"
    )
    final_path = destination / filename
    if final_path.exists():
        raise BackupError("refusing to overwrite an existing backup")

    with tempfile.TemporaryDirectory(
        prefix=".quant-platform-backup-",
        dir=destination,
    ) as temporary:
        temporary_root = Path(temporary)
        temporary_root.chmod(0o700)
        staging_root = temporary_root / "staging"
        payload_root = staging_root / "payload"
        payload_root.mkdir(parents=True, mode=0o700)

        database_integrity: dict[str, str] = {}
        data_root = source / "data"
        for name in (*_REQUIRED_DATABASES, *_OPTIONAL_DATABASES):
            database_path = data_root / name
            if not database_path.exists():
                if name in _REQUIRED_DATABASES:
                    raise BackupError(f"required database is missing: {name}")
                continue
            database_integrity[name] = _sqlite_snapshot(
                database_path,
                payload_root / "data" / name,
            )

        _copy_regular_file(env_path, payload_root / ".env")
        runtime_config = [".env"]
        for relative in _CONFIG_FILES:
            config_source = source / relative
            if not config_source.exists():
                continue
            _copy_regular_file(config_source, payload_root / relative)
            runtime_config.append(relative)
        evidence_scopes: list[str] = []
        for relative in _EVIDENCE_DIRECTORIES:
            evidence_source = source / relative
            if not evidence_source.exists():
                continue
            if _is_link_or_reparse_point(evidence_source) or not evidence_source.is_dir():
                raise BackupError(f"evidence scope is not a directory: {relative}")
            _copy_evidence_tree(evidence_source, payload_root / relative)
            evidence_scopes.append(relative)

        entries = _manifest_entries(payload_root)
        manifest = {
            "schema": _MANIFEST_SCHEMA,
            "backup_id": backup_id,
            "created_at": created_at,
            "source_commit": _git_commit(source),
            "scope": {
                "databases": sorted(database_integrity),
                "runtime_config": runtime_config,
                "research_evidence": evidence_scopes,
                "excluded_regenerable_data": ["data/cache", "frontend/dist"],
            },
            "database_integrity": database_integrity,
            "entries": entries,
        }
        manifest_path = staging_root / "manifest.json"
        manifest_path.write_bytes(_canonical_json(manifest))
        manifest_path.chmod(0o600)

        tar_path = temporary_root / "backup.tar"
        _write_tar(staging_root, tar_path)
        tar_path.chmod(0o600)
        encrypted_path = temporary_root / filename
        header = _encrypt_file(
            tar_path,
            encrypted_path,
            passphrase,
            backup_id=backup_id,
            created_at=created_at,
        )
        os.replace(encrypted_path, final_path)
        final_path.chmod(0o600)

    audit = {
        "schema": "quant-platform-backup-audit/v1",
        "operation": "backup",
        "status": "verified",
        "backup_id": backup_id,
        "created_at": created_at,
        "archive_name": filename,
        "archive_size_bytes": final_path.stat().st_size,
        "archive_sha256": file_sha256(final_path),
        "manifest_sha256": hashlib.sha256(_canonical_json(manifest)).hexdigest(),
        "database_integrity": database_integrity,
        "file_count": len(entries),
        "encryption": header["cipher"],
        "source_commit": manifest["source_commit"],
    }
    audit_path = final_path.with_suffix(final_path.suffix + ".audit.json")
    audit_path.write_bytes(_canonical_json(audit))
    audit_path.chmod(0o600)
    return final_path, audit


def restore_backup(
    archive_path: Path,
    destination_directory: Path,
    passphrase: bytes,
) -> dict[str, Any]:
    """Restore to an empty, isolated directory and verify all evidence."""
    if not archive_path.is_absolute():
        raise BackupError("backup archive must be an absolute path")
    _require_not_link_or_reparse_point(
        archive_path,
        message="backup archive must not be a symlink or reparse point",
    )
    archive = archive_path.resolve(strict=True)
    destination_parent = _validate_private_directory(
        destination_directory.parent,
        label="restore parent",
    )
    if destination_directory.exists():
        _require_not_link_or_reparse_point(
            destination_directory,
            message="restore destination must not be a symlink or reparse point",
        )
    if destination_directory.exists() and any(destination_directory.iterdir()):
        raise BackupError("restore destination must be empty")
    destination_directory.mkdir(mode=0o700, exist_ok=True)
    destination = destination_directory.resolve(strict=True)
    if destination.parent != destination_parent:
        raise BackupError("restore destination must be a direct child of restore parent")
    if len(passphrase) < _MIN_PASSPHRASE_BYTES:
        raise BackupError("backup passphrase is too short")

    success = False
    try:
        with tempfile.TemporaryDirectory(
            prefix=".quant-platform-restore-",
            dir=destination_parent,
        ) as temporary:
            tar_path = Path(temporary) / "backup.tar"
            header = _decrypt_file(archive, tar_path, passphrase)
            _extract_archive(tar_path, destination)
        manifest = _load_and_verify_manifest(destination)
        if manifest.get("backup_id") != header.get("backup_id"):
            raise BackupError("backup header and manifest identities differ")
        database_integrity = _verify_restored_databases(destination / "payload")
        audit = {
            "schema": "quant-platform-restore-audit/v1",
            "operation": "restore_drill",
            "status": "verified",
            "backup_id": manifest["backup_id"],
            "restored_at": _utc_now(),
            "source_commit": manifest.get("source_commit"),
            "archive_sha256": file_sha256(archive),
            "manifest_sha256": hashlib.sha256(_canonical_json(manifest)).hexdigest(),
            "database_integrity": database_integrity,
            "file_count": len(manifest["entries"]),
            "destination_is_isolated": True,
        }
        audit_path = destination / "restore-audit.json"
        audit_path.write_bytes(_canonical_json(audit))
        audit_path.chmod(0o600)
        success = True
        return audit
    finally:
        if not success and destination.exists():
            shutil.rmtree(destination)


def _print_result(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--source-root", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--passphrase-file", type=Path, required=True)
    restore = subparsers.add_parser("restore-drill")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--passphrase-file", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        passphrase = read_passphrase_file(arguments.passphrase_file)
        if arguments.command == "backup":
            archive, audit = create_backup(
                arguments.source_root,
                arguments.destination,
                passphrase,
            )
            _print_result({**audit, "archive": str(archive)})
        else:
            _print_result(
                restore_backup(
                    arguments.archive,
                    arguments.destination,
                    passphrase,
                )
            )
    except (BackupError, OSError) as exc:
        _print_result({"status": "failed", "error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
