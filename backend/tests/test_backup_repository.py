from __future__ import annotations
from backend.core.hashing import file_sha256

import hashlib
import json
import os
import shutil
import stat
import struct
from pathlib import Path
from typing import Any

import pytest

from backend.ops import backup_repository
from backend.ops.backup_repository import (
    RepositoryBackupError,
    configure_repository,
    download_verified_archive,
    load_config,
    sync_archive,
)
from backend.ops.disaster_recovery import _MAGIC


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _archive(
    directory: Path,
    *,
    name: str = "quant-platform-20260801T031500Z-0123456789ab.qpbak",
    size: int | None = None,
) -> Path:
    archive = directory / name
    header = json.dumps(
        {
            "schema": "quant-platform-encrypted-backup/v1",
            "cipher": "AES-256-GCM",
            "kdf": {"name": "scrypt", "n": 2**15, "r": 8, "p": 1},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    archive.write_bytes(
        _MAGIC
        + struct.pack(">I", len(header))
        + header
        + b"ciphertext"
        + (b"authentication-tag"[:16])
    )
    if size is not None:
        with archive.open("r+b") as stream:
            stream.truncate(size)
    archive.chmod(0o600)
    return archive


def _config(state: Path, *, retention_count: int = 30) -> Path:
    config = state / "backup-repository.json"
    configure_repository(
        config,
        owner="feng653",
        repository="quant-platform-backups",
        retention_count=retention_count,
        verifier=lambda _owner, _repository: None,
    )
    return config


def _sync_paths(state: Path) -> tuple[Path, Path]:
    return (
        state / "backup-repository.lock",
        state / "backup-repository-audit.jsonl",
    )


class FakeReleaseRemote:
    def __init__(self) -> None:
        self.exists = False
        self.assets: list[dict[str, Any]] = []
        self.uploads = 0
        self.downloads = 0
        self.deletions: list[int] = []
        self.payloads: dict[str, Path] = {}

    def release(self, config: dict[str, Any]) -> dict[str, Any] | None:
        if not self.exists:
            return None
        return {"tag_name": config["release_tag"], "assets": list(self.assets)}

    def create(self, _config: dict[str, Any]) -> None:
        self.exists = True

    def upload(self, _config: dict[str, Any], archive: Path) -> None:
        self.uploads += 1
        self.exists = True
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assets.append(
            {
                "id": 1000 + self.uploads,
                "name": archive.name,
                "size": archive.stat().st_size,
                "digest": f"sha256:{digest}",
                "created_at": f"2026-08-01T03:15:{self.uploads:02d}Z",
            }
        )
        self.payloads[archive.name] = archive

    def upload_sparse(self, _config: dict[str, Any], archive: Path) -> None:
        self.uploads += 1
        self.exists = True
        digest = file_sha256(archive)
        self.assets.append(
            {
                "id": 1000 + self.uploads,
                "name": archive.name,
                "size": archive.stat().st_size,
                "digest": f"sha256:{digest}",
                "created_at": "2026-08-01T03:15:00Z",
            }
        )

    def delete(self, _config: dict[str, Any], asset_id: int) -> None:
        self.deletions.append(asset_id)
        self.assets = [asset for asset in self.assets if asset["id"] != asset_id]

    def download(
        self,
        _config: dict[str, Any],
        asset_name: str,
        destination: Path,
    ) -> None:
        self.downloads += 1
        shutil.copyfile(self.payloads[asset_name], destination / asset_name)
        (destination / asset_name).chmod(0o600)


def _install_remote(
    monkeypatch: pytest.MonkeyPatch,
    remote: FakeReleaseRemote,
) -> None:
    monkeypatch.setattr(backup_repository, "_get_release", remote.release)
    monkeypatch.setattr(backup_repository, "_create_release", remote.create)
    monkeypatch.setattr(backup_repository, "_upload_release_asset", remote.upload)
    monkeypatch.setattr(backup_repository, "_delete_release_asset", remote.delete)
    monkeypatch.setattr(backup_repository, "_download_release_asset", remote.download)


def test_configure_requires_private_verification_and_writes_no_secret(
    tmp_path: Path,
) -> None:
    state = _private_directory(tmp_path / "state")
    config = state / "backup-repository.json"
    verified: list[tuple[str, str]] = []
    result = configure_repository(
        config,
        owner="feng653",
        repository="quant-platform-backups",
        retention_count=45,
        verifier=lambda owner, repository: verified.append((owner, repository)),
    )
    payload = json.loads(config.read_text(encoding="utf-8"))

    assert verified == [("feng653", "quant-platform-backups")]
    assert result["private_verified"] is True
    assert payload["schema"] == "quant-platform-backup-repository/v2"
    assert payload["remote_url"] == "https://github.com/feng653/quant-platform-backups"
    assert payload["release_tag"] == "quant-platform-encrypted-backups-v1"
    assert payload["retention_count"] == 45
    assert "token" not in config.read_text(encoding="utf-8").lower()
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600

    with pytest.raises(RepositoryBackupError, match="not private"):
        configure_repository(
            state / "rejected.json",
            owner="feng653",
            repository="public-repo",
            verifier=lambda _owner, _repository: (_ for _ in ()).throw(
                RepositoryBackupError("repository is not private")
            ),
        )
    assert not (state / "rejected.json").exists()


def test_private_verification_requires_a_release_anchor_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_repository, "_gh_path", lambda: "/usr/bin/gh")
    responses = iter(
        [
            "",
            json.dumps(
                {
                    "nameWithOwner": "feng653/quant-platform-backups",
                    "visibility": "PRIVATE",
                    "defaultBranchRef": None,
                }
            ),
        ]
    )
    monkeypatch.setattr(
        backup_repository,
        "_run",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(RepositoryBackupError, match="anchor commit"):
        backup_repository.verify_private_github_repository(
            "feng653", "quant-platform-backups"
        )


@pytest.mark.parametrize(
    ("owner", "repository", "remote_url", "retention"),
    [
        (
            "feng653",
            "quant-platform-backups",
            "https://credential@github.com/feng653/quant-platform-backups",
            30,
        ),
        ("../feng653", "quant-platform-backups", "https://github.com/x/y", 30),
        ("feng653", "../secrets", "https://github.com/x/y", 30),
        ("feng653", "quant-platform-backups", "https://github.com/x/y", 0),
    ],
)
def test_config_rejects_unsafe_target_or_retention(
    tmp_path: Path,
    owner: str,
    repository: str,
    remote_url: str,
    retention: int,
) -> None:
    state = _private_directory(tmp_path / "state")
    config = state / "backup-repository.json"
    config.write_text(
        json.dumps(
            {
                "schema": "quant-platform-backup-repository/v2",
                "owner": owner,
                "repository": repository,
                "remote_url": remote_url,
                "release_tag": "quant-platform-encrypted-backups-v1",
                "retention_count": retention,
                "private_verified": True,
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    with pytest.raises(RepositoryBackupError):
        load_config(config)


def test_release_upload_accepts_asset_larger_than_git_object_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "state")
    backups = _private_directory(state / "backups")
    archive = _archive(backups, size=(101 * 1024 * 1024) + 1)
    config = _config(state)
    lock, audit = _sync_paths(state)
    remote = FakeReleaseRemote()
    _install_remote(monkeypatch, remote)
    monkeypatch.setattr(backup_repository, "_upload_release_asset", remote.upload_sparse)

    result = sync_archive(
        archive,
        config,
        lock,
        audit,
        verifier=lambda _owner, _repository: None,
    )

    assert result["status"] == "uploaded"
    assert result["archive_size_bytes"] > 100 * 1024 * 1024
    assert remote.uploads == 1
    assert [asset["name"] for asset in remote.assets] == [archive.name]
    assert not (state / "backup-git").exists()


def test_release_upload_is_idempotent_and_enforces_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "state")
    backups = _private_directory(state / "backups")
    archive = _archive(backups)
    config = _config(state, retention_count=2)
    lock, audit = _sync_paths(state)
    remote = FakeReleaseRemote()
    _install_remote(monkeypatch, remote)
    old_names = [
        "quant-platform-20260730T031500Z-111111111111.qpbak",
        "quant-platform-20260731T031500Z-222222222222.qpbak",
    ]
    for index, name in enumerate(old_names, start=1):
        remote.assets.append(
            {
                "id": index,
                "name": name,
                "size": 50,
                "digest": "sha256:" + ("0" * 64),
                "created_at": f"2026-07-{29 + index}T03:15:00Z",
            }
        )
    remote.exists = True

    first = sync_archive(
        archive,
        config,
        lock,
        audit,
        verifier=lambda _owner, _repository: None,
    )
    second = sync_archive(
        archive,
        config,
        lock,
        audit,
        verifier=lambda _owner, _repository: None,
    )

    assert first["deleted_asset_count"] == 1
    assert second["deleted_asset_count"] == 0
    assert remote.uploads == 1
    assert remote.deletions == [1]
    audits = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [item["status"] for item in audits] == ["uploaded", "uploaded"]
    assert all("remote_url" not in item for item in audits)


def test_upload_failure_is_redacted_and_retains_local_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "state")
    backups = _private_directory(state / "backups")
    archive = _archive(backups)
    config = _config(state)
    lock, audit = _sync_paths(state)
    remote = FakeReleaseRemote()
    _install_remote(monkeypatch, remote)

    def fail_upload(_config: dict[str, Any], _archive: Path) -> None:
        raise RepositoryBackupError("upload failed for " + "ghp_" + ("a" * 26))

    monkeypatch.setattr(backup_repository, "_upload_release_asset", fail_upload)
    with pytest.raises(RepositoryBackupError, match="REDACTED"):
        sync_archive(
            archive,
            config,
            lock,
            audit,
            verifier=lambda _owner, _repository: None,
        )

    assert archive.is_file()
    audit_text = audit.read_text(encoding="utf-8")
    assert "ghp_" not in audit_text
    assert json.loads(audit_text.splitlines()[-1])["local_archive_retained"] is True


def test_server_digest_is_mandatory_after_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "state")
    backups = _private_directory(state / "backups")
    archive = _archive(backups)
    config = _config(state)
    lock, audit = _sync_paths(state)
    remote = FakeReleaseRemote()
    _install_remote(monkeypatch, remote)

    def upload_without_digest(_config: dict[str, Any], uploaded: Path) -> None:
        remote.exists = True
        remote.uploads += 1
        remote.assets.append(
            {
                "id": 1,
                "name": uploaded.name,
                "size": uploaded.stat().st_size,
                "digest": None,
                "created_at": "2026-08-01T03:15:00Z",
            }
        )

    monkeypatch.setattr(
        backup_repository, "_upload_release_asset", upload_without_digest
    )
    with pytest.raises(RepositoryBackupError, match="digest mismatch or unavailable"):
        sync_archive(
            archive,
            config,
            lock,
            audit,
            verifier=lambda _owner, _repository: None,
        )
    assert archive.is_file()


def test_release_with_any_plaintext_asset_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "state")
    backups = _private_directory(state / "backups")
    archive = _archive(backups)
    config = _config(state)
    lock, audit = _sync_paths(state)
    remote = FakeReleaseRemote()
    _install_remote(monkeypatch, remote)
    remote.exists = True
    remote.assets.append({"id": 9, "name": "audit.json", "size": 1})

    with pytest.raises(RepositoryBackupError, match="non-ciphertext"):
        sync_archive(
            archive,
            config,
            lock,
            audit,
            verifier=lambda _owner, _repository: None,
        )
    assert archive.is_file()
    assert remote.uploads == 0


def test_sync_rejects_plaintext_and_concurrent_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "state")
    backups = _private_directory(state / "backups")
    archive = _archive(backups)
    config = _config(state)
    lock, audit = _sync_paths(state)
    monkeypatch.setattr(
        backup_repository.fcntl,
        "flock",
        lambda *_args: (_ for _ in ()).throw(BlockingIOError()),
    )
    with pytest.raises(RepositoryBackupError, match="already running"):
        sync_archive(
            archive,
            config,
            lock,
            audit,
            verifier=lambda _owner, _repository: None,
        )

    archive.write_text("plain database bytes", encoding="utf-8")
    archive.chmod(0o600)
    with pytest.raises(RepositoryBackupError, match="header is invalid"):
        sync_archive(
            archive,
            config,
            lock,
            audit,
            verifier=lambda _owner, _repository: None,
        )
    assert archive.is_file()


def test_download_verifies_server_digest_and_is_ready_for_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_state = _private_directory(tmp_path / "remote")
    source = _archive(remote_state)
    state = _private_directory(tmp_path / "state")
    destination = _private_directory(state / "backups")
    config = _config(state)
    lock, audit = _sync_paths(state)
    remote = FakeReleaseRemote()
    _install_remote(monkeypatch, remote)
    remote.upload({}, source)

    archive, result = download_verified_archive(
        source.name,
        destination,
        config,
        lock,
        audit,
        verifier=lambda _owner, _repository: None,
    )

    assert archive == destination / source.name
    assert archive.read_bytes() == source.read_bytes()
    assert result["status"] == "verified"
    assert result["ready_for_restore_drill"] is True
    assert remote.downloads == 1

    # Even a valid same-named local archive must never bypass the remote read.
    # The second call proves that the Release is fetched again rather than
    # accepting the first call's local output as remote-recovery evidence.
    archive, repeated = download_verified_archive(
        source.name,
        destination,
        config,
        lock,
        audit,
        verifier=lambda _owner, _repository: None,
    )
    assert archive.read_bytes() == source.read_bytes()
    assert repeated["status"] == "verified"
    assert remote.downloads == 2

    archive.unlink()
    remote.assets[0]["digest"] = "sha256:" + ("0" * 64)
    with pytest.raises(RepositoryBackupError, match="integrity failed"):
        download_verified_archive(
            source.name,
            destination,
            config,
            lock,
            audit,
            verifier=lambda _owner, _repository: None,
        )
    assert not archive.exists()


def test_macos_backup_job_uses_release_helper_without_plist_secrets() -> None:
    deploy_root = Path(__file__).resolve().parents[2] / "deploy/macos"
    backup_script = (deploy_root / "quant-platform-backup.sh").read_text()
    helper = (deploy_root / "quant-platform-backup-repository.sh").read_text()
    plist = (deploy_root / "com.quant-platform.backup.plist").read_text()

    assert '"$REPOSITORY_SCRIPT" sync "$archive_name"' in backup_script
    assert "backend.ops.backup_repository" in helper
    assert "download-verify" in helper
    assert "backup-git" not in helper
    assert "backup-repository.json" not in plist
    assert "TOKEN" not in plist.upper()
    assert "PASSWORD" not in plist.upper()
