"""Store encrypted disaster-recovery archives as private GitHub Release assets.

GitHub Git objects are limited to 100 MiB, while production encrypted backups
are larger.  This module therefore never creates a Git commit.  It admits only
``.qpbak`` ciphertext, verifies the private repository before every mutation,
and checks GitHub's server-side asset digest after upload and before download.
"""

from __future__ import annotations
from backend.core.hashing import file_sha256

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.ops.disaster_recovery import _read_header

_CONFIG_SCHEMA = "quant-platform-backup-repository/v2"
_AUDIT_SCHEMA = "quant-platform-backup-repository-audit/v2"
_RELEASE_TAG = "quant-platform-encrypted-backups-v1"
_OWNER_RE = re.compile(r"(?=.{1,39}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z")
_REPOSITORY_RE = re.compile(r"(?=.{1,100}\Z)[A-Za-z0-9._-]+\Z")
_ARCHIVE_RE = re.compile(
    r"quant-platform-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\.qpbak\Z"
)
_TOKEN_RE = re.compile(r"(?i)(?:gh[opsu]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+)")
_CREDENTIAL_URL_RE = re.compile(r"https://[^/@\s]+@github\.com", re.IGNORECASE)
_HAS_POSIX_PERMISSION_SECURITY = os.name == "posix"


class RepositoryBackupError(RuntimeError):
    """Raised when a remote backup cannot satisfy its safety contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _redact(value: object) -> str:
    text = _TOKEN_RE.sub("[REDACTED]", str(value))
    return _CREDENTIAL_URL_RE.sub("https://[REDACTED]@github.com", text)


def _require_absolute_unlinked(path: Path, *, label: str, exists: bool) -> Path:
    if not path.is_absolute():
        raise RepositoryBackupError(f"{label} must be an absolute path")
    lexical = Path(os.path.abspath(path))
    try:
        resolved = path.resolve(strict=exists)
    except OSError as exc:
        raise RepositoryBackupError(f"{label} cannot be resolved safely") from exc
    if lexical != resolved:
        raise RepositoryBackupError(f"{label} must not traverse a symlink")
    return resolved


def _require_private_directory(path: Path, *, label: str) -> Path:
    path = _require_absolute_unlinked(path, label=label, exists=False)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _require_absolute_unlinked(path, label=label, exists=True)
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RepositoryBackupError(f"{label} must be a directory")
    if _HAS_POSIX_PERMISSION_SECURITY and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RepositoryBackupError(f"{label} permissions must be 0700 or stricter")
    return path


def _require_private_file(path: Path, *, label: str) -> Path:
    path = _require_absolute_unlinked(path, label=label, exists=True)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RepositoryBackupError(f"{label} must be a regular file")
    if _HAS_POSIX_PERMISSION_SECURITY and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RepositoryBackupError(f"{label} permissions must be 0600 or stricter")
    return path


def _validate_owner_repository(owner: str, repository: str) -> None:
    if not _OWNER_RE.fullmatch(owner):
        raise RepositoryBackupError("GitHub owner is invalid")
    if not _REPOSITORY_RE.fullmatch(repository) or repository in {".", ".."}:
        raise RepositoryBackupError("GitHub repository name is invalid")
    if repository.lower().endswith(".git"):
        raise RepositoryBackupError("GitHub repository name must not end in .git")


def _validate_config(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != _CONFIG_SCHEMA:
        raise RepositoryBackupError("backup repository config schema is invalid")
    owner = payload.get("owner")
    repository = payload.get("repository")
    remote_url = payload.get("remote_url")
    release_tag = payload.get("release_tag")
    retention_count = payload.get("retention_count")
    if not isinstance(owner, str) or not isinstance(repository, str):
        raise RepositoryBackupError("backup repository config fields are invalid")
    _validate_owner_repository(owner, repository)
    expected_url = f"https://github.com/{owner}/{repository}"
    if remote_url != expected_url:
        raise RepositoryBackupError("GitHub remote URL does not match owner/repository")
    if release_tag != _RELEASE_TAG:
        raise RepositoryBackupError("backup release tag is invalid")
    if (
        isinstance(retention_count, bool)
        or not isinstance(retention_count, int)
        or not 1 <= retention_count <= 1000
    ):
        raise RepositoryBackupError("backup retention count must be between 1 and 1000")
    if payload.get("private_verified") is not True:
        raise RepositoryBackupError("backup repository lacks private verification")
    return {
        "owner": owner,
        "repository": repository,
        "remote_url": remote_url,
        "release_tag": release_tag,
        "retention_count": retention_count,
    }


def load_config(config_path: Path) -> dict[str, Any]:
    config = _require_private_file(config_path, label="backup repository config")
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryBackupError("backup repository config is invalid") from exc
    return _validate_config(payload)


def _command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GH_HOST"] = "github.com"
    environment["GH_PROMPT_DISABLED"] = "1"
    environment.pop("GH_REPO", None)
    return environment


def _execute(arguments: Sequence[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_command_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositoryBackupError("GitHub operation could not be executed") from exc


def _run(arguments: Sequence[str], *, operation: str, timeout: int = 300) -> str:
    result = _execute(arguments, timeout=timeout)
    if result.returncode != 0:
        raise RepositoryBackupError(f"{operation} failed (exit {result.returncode})")
    return result.stdout


def _gh_path() -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise RepositoryBackupError("gh is required for private Release backup")
    return gh


def verify_private_github_repository(owner: str, repository: str) -> None:
    """Use authenticated GitHub metadata; never infer privacy from asset access."""
    _validate_owner_repository(owner, repository)
    gh = _gh_path()
    _run(
        [gh, "auth", "status", "--hostname", "github.com"],
        operation="GitHub authentication check",
    )
    output = _run(
        [
            gh,
            "repo",
            "view",
            f"{owner}/{repository}",
            "--json",
            "nameWithOwner,visibility,defaultBranchRef",
        ],
        operation="GitHub private repository verification",
    )
    try:
        metadata = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RepositoryBackupError("GitHub repository metadata is invalid") from exc
    if (
        str(metadata.get("nameWithOwner", "")).casefold()
        != f"{owner}/{repository}".casefold()
        or metadata.get("visibility") != "PRIVATE"
    ):
        raise RepositoryBackupError("GitHub repository is not the verified private target")
    default_branch = metadata.get("defaultBranchRef")
    if (
        not isinstance(default_branch, dict)
        or not isinstance(default_branch.get("name"), str)
        or not default_branch["name"]
    ):
        raise RepositoryBackupError(
            "private backup repository needs a file-free anchor commit"
        )


def configure_repository(
    config_path: Path,
    *,
    owner: str,
    repository: str,
    retention_count: int = 30,
    verifier: Callable[[str, str], None] = verify_private_github_repository,
) -> dict[str, Any]:
    """Write non-secret config only after a read-only PRIVATE verification."""
    _validate_owner_repository(owner, repository)
    if isinstance(retention_count, bool) or not 1 <= retention_count <= 1000:
        raise RepositoryBackupError("backup retention count must be between 1 and 1000")
    verifier(owner, repository)
    parent = _require_private_directory(config_path.parent, label="config directory")
    target = _require_absolute_unlinked(config_path, label="config path", exists=False)
    if target.parent != parent:
        raise RepositoryBackupError("config must be a direct child of config directory")
    payload = {
        "schema": _CONFIG_SCHEMA,
        "owner": owner,
        "repository": repository,
        "remote_url": f"https://github.com/{owner}/{repository}",
        "release_tag": _RELEASE_TAG,
        "retention_count": retention_count,
        "private_verified": True,
        "private_verified_at": _utc_now(),
    }
    descriptor, temporary_name = tempfile.mkstemp(prefix=".backup-repo-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_json(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "configured",
        "repository": f"github.com/{owner}/{repository}",
        "release_tag": _RELEASE_TAG,
        "retention_count": retention_count,
        "private_verified": True,
    }


def _validate_archive(archive_path: Path) -> Path:
    archive = _require_private_file(archive_path, label="encrypted backup archive")
    if not _ARCHIVE_RE.fullmatch(archive.name):
        raise RepositoryBackupError("encrypted backup archive name is invalid")
    try:
        with archive.open("rb") as stream:
            header, _header_bytes = _read_header(stream)
            encrypted_payload_bytes = archive.stat().st_size - stream.tell()
    except Exception as exc:
        raise RepositoryBackupError("encrypted backup archive header is invalid") from exc
    kdf = header.get("kdf")
    if (
        header.get("cipher") != "AES-256-GCM"
        or not isinstance(kdf, dict)
        or kdf.get("name") != "scrypt"
        or kdf.get("n") != 2**15
        or kdf.get("r") != 8
        or kdf.get("p") != 1
        or encrypted_payload_bytes < 17
    ):
        raise RepositoryBackupError("backup archive is not an encrypted platform payload")
    return archive


def _release_endpoint(config: dict[str, Any]) -> str:
    return (
        f"repos/{config['owner']}/{config['repository']}/releases/tags/"
        f"{config['release_tag']}"
    )


def _get_release(config: dict[str, Any]) -> dict[str, Any] | None:
    result = _execute(
        [_gh_path(), "api", "--hostname", "github.com", _release_endpoint(config)]
    )
    if result.returncode != 0:
        return None
    try:
        release = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RepositoryBackupError("GitHub Release metadata is invalid") from exc
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != config["release_tag"]
        or not isinstance(release.get("id"), int)
    ):
        raise RepositoryBackupError("GitHub Release identity is invalid")
    assets_endpoint = (
        f"repos/{config['owner']}/{config['repository']}/releases/"
        f"{release['id']}/assets?per_page=100"
    )
    assets_output = _run(
        [
            _gh_path(),
            "api",
            "--hostname",
            "github.com",
            "--paginate",
            "--slurp",
            assets_endpoint,
        ],
        operation="GitHub Release asset inventory",
    )
    try:
        asset_pages = json.loads(assets_output)
    except json.JSONDecodeError as exc:
        raise RepositoryBackupError("GitHub Release asset metadata is invalid") from exc
    if not isinstance(asset_pages, list) or any(
        not isinstance(page, list) for page in asset_pages
    ):
        raise RepositoryBackupError("GitHub Release asset metadata is invalid")
    release["assets"] = [asset for page in asset_pages for asset in page]
    return release


def _create_release(config: dict[str, Any]) -> None:
    repository = f"{config['owner']}/{config['repository']}"
    result = _execute(
        [
            _gh_path(),
            "release",
            "create",
            config["release_tag"],
            "--repo",
            repository,
            "--title",
            "Quant Platform encrypted backups",
            "--notes",
            "Release assets are encrypted disaster-recovery archives only.",
        ]
    )
    if result.returncode != 0 and _get_release(config) is None:
        raise RepositoryBackupError(
            f"private backup Release creation failed (exit {result.returncode})"
        )


def _ensure_release(config: dict[str, Any]) -> dict[str, Any]:
    release = _get_release(config)
    if release is None:
        _create_release(config)
        release = _get_release(config)
    if release is None:
        raise RepositoryBackupError("private backup Release is unavailable")
    names: set[str] = set()
    for asset in release["assets"]:
        name = asset.get("name") if isinstance(asset, dict) else None
        if not isinstance(name, str) or not _ARCHIVE_RE.fullmatch(name):
            raise RepositoryBackupError(
                "private backup Release contains a non-ciphertext asset"
            )
        if name in names:
            raise RepositoryBackupError("GitHub Release has duplicate backup asset names")
        names.add(name)
    return release


def _asset_by_name(release: dict[str, Any], name: str) -> dict[str, Any] | None:
    matches = [asset for asset in release["assets"] if asset.get("name") == name]
    if len(matches) > 1:
        raise RepositoryBackupError("GitHub Release has duplicate backup asset names")
    return matches[0] if matches else None


def _verify_asset(asset: dict[str, Any], archive: Path) -> str:
    expected_digest = file_sha256(archive)
    if asset.get("size") != archive.stat().st_size:
        raise RepositoryBackupError("GitHub Release asset size mismatch")
    if asset.get("digest") != f"sha256:{expected_digest}":
        raise RepositoryBackupError("GitHub Release asset digest mismatch or unavailable")
    if not isinstance(asset.get("id"), int):
        raise RepositoryBackupError("GitHub Release asset ID is invalid")
    return expected_digest


def _upload_release_asset(config: dict[str, Any], archive: Path) -> None:
    repository = f"{config['owner']}/{config['repository']}"
    _run(
        [
            _gh_path(),
            "release",
            "upload",
            config["release_tag"],
            str(archive),
            "--repo",
            repository,
        ],
        operation="encrypted Release asset upload",
        timeout=3600,
    )


def _delete_release_asset(config: dict[str, Any], asset_id: int) -> None:
    endpoint = (
        f"repos/{config['owner']}/{config['repository']}/releases/assets/{asset_id}"
    )
    _run(
        [_gh_path(), "api", "--hostname", "github.com", "--method", "DELETE", endpoint],
        operation="expired encrypted Release asset deletion",
    )


def _apply_retention(
    config: dict[str, Any],
    release: dict[str, Any],
    protected_name: str,
) -> list[str]:
    encrypted_assets = [
        asset
        for asset in release["assets"]
        if isinstance(asset.get("name"), str)
        and _ARCHIVE_RE.fullmatch(asset["name"])
        and isinstance(asset.get("id"), int)
    ]
    encrypted_assets.sort(
        key=lambda asset: (str(asset.get("created_at", "")), asset["name"]),
        reverse=True,
    )
    protected = [asset for asset in encrypted_assets if asset["name"] == protected_name]
    if len(protected) != 1:
        raise RepositoryBackupError("verified Release asset is not retention-safe")
    other_assets = [asset for asset in encrypted_assets if asset["name"] != protected_name]
    keep_other_count = config["retention_count"] - 1
    expired = other_assets[keep_other_count:]
    for asset in expired:
        _delete_release_asset(config, asset["id"])
    return [asset["name"] for asset in expired]


def _append_audit(audit_path: Path, record: dict[str, Any]) -> None:
    parent = _require_private_directory(audit_path.parent, label="audit directory")
    target = _require_absolute_unlinked(audit_path, label="audit path", exists=False)
    if target.parent != parent:
        raise RepositoryBackupError("audit must be a direct child of audit directory")
    if target.exists():
        _require_private_file(target, label="repository audit log")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        os.write(descriptor, _canonical_json(record) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    target.chmod(0o600)


def _lock_descriptor(lock_path: Path) -> int:
    parent = _require_private_directory(lock_path.parent, label="lock directory")
    lock = _require_absolute_unlinked(lock_path, label="lock path", exists=False)
    if lock.parent != parent:
        raise RepositoryBackupError("lock must be a direct child of lock directory")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock, flags, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise RepositoryBackupError("backup repository lock must be a regular file")
    if _HAS_POSIX_PERMISSION_SECURITY and stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(descriptor)
        raise RepositoryBackupError("backup repository lock permissions are unsafe")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise RepositoryBackupError("backup repository operation is already running") from exc
    return descriptor


def sync_archive(
    archive_path: Path,
    config_path: Path,
    lock_path: Path,
    audit_path: Path,
    *,
    verifier: Callable[[str, str], None] = verify_private_github_repository,
) -> dict[str, Any]:
    """Idempotently upload one encrypted archive as a private Release asset."""
    started_at = _utc_now()
    archive_name = archive_path.name
    try:
        archive = _validate_archive(archive_path)
        config = load_config(config_path)
        descriptor = _lock_descriptor(lock_path)
        try:
            verifier(config["owner"], config["repository"])
            release = _ensure_release(config)
            asset = _asset_by_name(release, archive.name)
            if asset is None:
                try:
                    _upload_release_asset(config, archive)
                except RepositoryBackupError:
                    # A concurrent remote uploader may have won after our read.
                    if _asset_by_name(_ensure_release(config), archive.name) is None:
                        raise
                release = _ensure_release(config)
                asset = _asset_by_name(release, archive.name)
            if asset is None:
                raise RepositoryBackupError("uploaded GitHub Release asset is unavailable")
            digest = _verify_asset(asset, archive)
            deleted_assets = _apply_retention(config, release, archive.name)
        finally:
            os.close(descriptor)
        result = {
            "schema": _AUDIT_SCHEMA,
            "operation": "encrypted_release_upload",
            "status": "uploaded",
            "started_at": started_at,
            "completed_at": _utc_now(),
            "repository": f"github.com/{config['owner']}/{config['repository']}",
            "release_tag": config["release_tag"],
            "archive_name": archive.name,
            "archive_size_bytes": archive.stat().st_size,
            "archive_sha256": digest,
            "retention_count": config["retention_count"],
            "deleted_asset_count": len(deleted_assets),
        }
        _append_audit(audit_path, result)
        return result
    except (RepositoryBackupError, OSError) as exc:
        error = _redact(exc)
        failure = {
            "schema": _AUDIT_SCHEMA,
            "operation": "encrypted_release_upload",
            "status": "failed",
            "started_at": started_at,
            "completed_at": _utc_now(),
            "archive_name": archive_name,
            "local_archive_retained": archive_path.exists(),
            "error": error,
        }
        try:
            _append_audit(audit_path, failure)
        except (RepositoryBackupError, OSError):
            pass
        raise RepositoryBackupError(error) from exc


def _download_release_asset(
    config: dict[str, Any],
    asset_name: str,
    destination: Path,
) -> None:
    repository = f"{config['owner']}/{config['repository']}"
    _run(
        [
            _gh_path(),
            "release",
            "download",
            config["release_tag"],
            "--repo",
            repository,
            "--pattern",
            asset_name,
            "--dir",
            str(destination),
        ],
        operation="encrypted Release asset download",
        timeout=3600,
    )


def download_verified_archive(
    archive_name: str,
    destination_directory: Path,
    config_path: Path,
    lock_path: Path,
    audit_path: Path,
    *,
    verifier: Callable[[str, str], None] = verify_private_github_repository,
) -> tuple[Path, dict[str, Any]]:
    """Download a Release asset and verify it before publishing for restore."""
    started_at = _utc_now()
    if not _ARCHIVE_RE.fullmatch(archive_name):
        raise RepositoryBackupError("encrypted backup archive name is invalid")
    config = load_config(config_path)
    destination = _require_private_directory(
        destination_directory,
        label="download destination",
    )
    descriptor = _lock_descriptor(lock_path)
    try:
        verifier(config["owner"], config["repository"])
        release = _ensure_release(config)
        asset = _asset_by_name(release, archive_name)
        if asset is None:
            raise RepositoryBackupError("encrypted Release asset is unavailable")
        expected_size = asset.get("size")
        expected_digest = asset.get("digest")
        if not isinstance(expected_size, int) or not isinstance(expected_digest, str):
            raise RepositoryBackupError("GitHub Release asset integrity is unavailable")
        target = destination / archive_name
        # A recovery exercise must prove that GitHub can return the bytes. Do
        # not short-circuit on a same-named local archive: that would allow a
        # convincing but false "remote restore" success without reading the
        # network or Release asset. An interrupted download leaves any prior
        # verified target untouched.
        with tempfile.TemporaryDirectory(
            prefix=".release-download-", dir=destination
        ) as tmp:
            temporary_root = Path(tmp)
            temporary_root.chmod(0o700)
            _download_release_asset(config, archive_name, temporary_root)
            downloaded_path = temporary_root / archive_name
            if downloaded_path.is_symlink() or not downloaded_path.is_file():
                raise RepositoryBackupError(
                    "downloaded Release asset is not a regular file"
                )
            downloaded_path.chmod(0o600)
            downloaded = _validate_archive(downloaded_path)
            if (
                downloaded.stat().st_size != expected_size
                or f"sha256:{file_sha256(downloaded)}" != expected_digest
            ):
                raise RepositoryBackupError("downloaded Release asset integrity failed")
            os.replace(downloaded, target)
            target.chmod(0o600)
        archive = target
        digest = _verify_asset(asset, archive)
    finally:
        os.close(descriptor)
    audit = {
        "schema": _AUDIT_SCHEMA,
        "operation": "encrypted_release_download_verify",
        "status": "verified",
        "started_at": started_at,
        "completed_at": _utc_now(),
        "repository": f"github.com/{config['owner']}/{config['repository']}",
        "release_tag": config["release_tag"],
        "archive_name": archive_name,
        "archive_size_bytes": archive.stat().st_size,
        "archive_sha256": digest,
        "ready_for_restore_drill": True,
    }
    _append_audit(audit_path, audit)
    return archive, audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure = subparsers.add_parser("configure")
    configure.add_argument("--config", type=Path, required=True)
    configure.add_argument("--owner", required=True)
    configure.add_argument("--repository", required=True)
    configure.add_argument("--retention-count", type=int, default=30)
    check = subparsers.add_parser("check")
    check.add_argument("--config", type=Path, required=True)
    sync = subparsers.add_parser("sync")
    sync.add_argument("--archive", type=Path, required=True)
    sync.add_argument("--config", type=Path, required=True)
    sync.add_argument("--lock", type=Path, required=True)
    sync.add_argument("--audit", type=Path, required=True)
    download = subparsers.add_parser("download-verify")
    download.add_argument("--archive-name", required=True)
    download.add_argument("--destination", type=Path, required=True)
    download.add_argument("--config", type=Path, required=True)
    download.add_argument("--lock", type=Path, required=True)
    download.add_argument("--audit", type=Path, required=True)
    return parser


def _print_result(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: Iterable[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "configure":
            result = configure_repository(
                arguments.config,
                owner=arguments.owner,
                repository=arguments.repository,
                retention_count=arguments.retention_count,
            )
        elif arguments.command == "check":
            config = load_config(arguments.config)
            verify_private_github_repository(config["owner"], config["repository"])
            release = _get_release(config)
            result = {
                "status": "verified",
                "repository": f"github.com/{config['owner']}/{config['repository']}",
                "private": True,
                "release_exists": release is not None,
            }
        elif arguments.command == "sync":
            result = sync_archive(
                arguments.archive,
                arguments.config,
                arguments.lock,
                arguments.audit,
            )
        else:
            archive, audit = download_verified_archive(
                arguments.archive_name,
                arguments.destination,
                arguments.config,
                arguments.lock,
                arguments.audit,
            )
            result = {**audit, "archive": str(archive)}
        _print_result(result)
        return 0
    except (RepositoryBackupError, OSError) as exc:
        _print_result({"status": "failed", "error": _redact(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
