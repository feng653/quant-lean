from __future__ import annotations

import os
import plistlib
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.ops.disaster_recovery import (
    BackupError,
    _metadata_is_link_or_reparse_point,
    create_backup,
    read_passphrase_file,
    restore_backup,
)

_PASSPHRASE = b"unit-test-recovery-key-with-32-bytes"
_HAS_POSIX_PERMISSION_SECURITY = os.name == "posix"


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES (?)", (value,))
        connection.commit()


def _source_tree(tmp_path: Path) -> Path:
    source = _private_directory(tmp_path / "source")
    data = _private_directory(source / "data")
    for name in ("users.db", "experiment.db", "trading_sim.db", "jobs.db"):
        _database(data / name, name)
    (source / ".env").write_text(
        "JWT_SECRET=never-appear-in-ciphertext\n",
        encoding="utf-8",
    )
    (source / ".env").chmod(0o600)
    snapshots = source / "data" / "research_snapshots"
    snapshots.mkdir(mode=0o700)
    (snapshots / "factor-evidence.json").write_text(
        '{"run":"completed"}',
        encoding="utf-8",
    )
    models = source / "data" / "models"
    models.mkdir(mode=0o700)
    (models / "manifest.json").write_text(
        '{"sha256":"abc"}',
        encoding="utf-8",
    )
    return source


def test_encrypted_backup_and_isolated_restore_round_trip(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    destination = _private_directory(tmp_path / "backups")

    # Leave a committed WAL-mode update in the live source.  sqlite3.backup
    # must snapshot it without copying a potentially inconsistent db/wal pair.
    with sqlite3.connect(source / "data" / "experiment.db") as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO evidence VALUES ('wal-row')")
        connection.commit()
        archive, backup_audit = create_backup(
            source,
            destination,
            _PASSPHRASE,
        )

    assert backup_audit["status"] == "verified"
    assert backup_audit["encryption"] == "AES-256-GCM"
    assert archive.is_file()
    assert archive.with_suffix(".qpbak.audit.json").is_file()
    if _HAS_POSIX_PERMISSION_SECURITY:
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(
            archive.with_suffix(".qpbak.audit.json").stat().st_mode
        ) == 0o600
    encrypted = archive.read_bytes()
    assert b"never-appear-in-ciphertext" not in encrypted
    assert b"factor-evidence.json" not in encrypted

    restore_parent = _private_directory(tmp_path / "restore")
    restored = restore_parent / "verified-copy"
    restore_audit = restore_backup(archive, restored, _PASSPHRASE)

    assert restore_audit["status"] == "verified"
    assert restore_audit["destination_is_isolated"] is True
    assert restore_audit["backup_id"] == backup_audit["backup_id"]
    assert set(restore_audit["database_integrity"]) == {
        "users.db",
        "experiment.db",
        "trading_sim.db",
        "jobs.db",
    }
    with sqlite3.connect(restored / "payload/data/experiment.db") as connection:
        values = {
            row[0] for row in connection.execute("SELECT value FROM evidence")
        }
    assert values == {"experiment.db", "wal-row"}
    assert (
        restored / "payload/data/research_snapshots/factor-evidence.json"
    ).is_file()
    restored_database = restored / "payload/data/experiment.db"
    assert restored_database.is_file()
    if _HAS_POSIX_PERMISSION_SECURITY:
        assert stat.S_IMODE(restored_database.stat().st_mode) == 0o600


@pytest.mark.parametrize("tamper", [False, True])
def test_restore_authentication_failure_leaves_no_partial_tree(
    tmp_path: Path,
    tamper: bool,
) -> None:
    source = _source_tree(tmp_path)
    backup_directory = _private_directory(tmp_path / "backups")
    archive, _ = create_backup(source, backup_directory, _PASSPHRASE)
    passphrase = b"different-unit-test-key-with-32-bytes"
    if tamper:
        damaged = bytearray(archive.read_bytes())
        damaged[-1] ^= 0x01
        archive.write_bytes(damaged)
        archive.chmod(0o600)
        passphrase = _PASSPHRASE

    restore_parent = _private_directory(tmp_path / "restore")
    destination = restore_parent / "must-disappear"
    with pytest.raises(BackupError, match="authentication failed"):
        restore_backup(archive, destination, passphrase)
    assert not destination.exists()


def test_backup_rejects_destination_inside_source_and_evidence_symlink(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    inside = _private_directory(source / "unsafe-backups")
    with pytest.raises(BackupError, match="outside the source tree"):
        create_backup(source, inside, _PASSPHRASE)

    outside = _private_directory(tmp_path / "safe-backups")
    evidence_link = source / "data/research_snapshots/link"
    evidence_link.symlink_to(source / ".env")
    with pytest.raises(BackupError, match="contains a symlink"):
        create_backup(source, outside, _PASSPHRASE)


def test_passphrase_file_requires_private_regular_file(tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.write_bytes(_PASSPHRASE + b"\n")
    key.chmod(0o644)
    if _HAS_POSIX_PERMISSION_SECURITY:
        with pytest.raises(BackupError, match="0600"):
            read_passphrase_file(key)
    else:
        # Windows access is governed by DACLs; synthesized POSIX bits are not
        # evidence that the file is public and must not block recovery.
        assert read_passphrase_file(key) == _PASSPHRASE

    key.chmod(0o600)
    assert read_passphrase_file(key) == _PASSPHRASE
    link = tmp_path / "backup-link.key"
    link.symlink_to(key)
    with pytest.raises(BackupError, match="symlink"):
        read_passphrase_file(link)


def test_macos_service_templates_bind_only_to_loopback() -> None:
    deploy_root = Path(__file__).resolve().parents[2] / "deploy/macos"
    for name, port in (
        ("com.quant-platform.backend.plist", "8000"),
        ("com.quant-platform.frontend.plist", "5173"),
    ):
        with (deploy_root / name).open("rb") as stream:
            payload = plistlib.load(stream)
        arguments = payload["ProgramArguments"]
        host_index = arguments.index("--host")
        port_index = arguments.index("--port")
        assert arguments[host_index + 1] == "127.0.0.1"
        assert arguments[port_index + 1] == port
        assert payload["Umask"] == 0o77

    caddy = (deploy_root / "Caddyfile.secure").read_text(encoding="utf-8")
    assert "not remote_ip private_ranges 127.0.0.1 ::1" in caddy
    assert "respond @remote_admin" in caddy
    assert "reverse_proxy 127.0.0.1:8000" in caddy
    assert "/openapi.json" in caddy
    assert "ALIYUN_ACCESS_KEY_SECRET=" not in caddy


def test_backup_archive_is_not_group_or_world_readable(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    destination = _private_directory(tmp_path / "backups")
    previous_umask = os.umask(0)
    try:
        archive, _ = create_backup(source, destination, _PASSPHRASE)
    finally:
        os.umask(previous_umask)
    assert archive.is_file()
    if _HAS_POSIX_PERMISSION_SECURITY:
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_backup_and_restore_explicitly_close_all_sqlite_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path)
    destination = _private_directory(tmp_path / "backups")
    connections: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        pass

    def tracked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = TrackedConnection
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(
        "backend.ops.disaster_recovery.sqlite3.connect",
        tracked_connect,
    )

    archive, _ = create_backup(source, destination, _PASSPHRASE)
    restore_parent = _private_directory(tmp_path / "restore")
    restore_backup(archive, restore_parent / "verified-copy", _PASSPHRASE)

    assert archive.is_file()
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def test_windows_reparse_attribute_is_treated_as_a_link_boundary() -> None:
    ordinary = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0)
    reparse = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700,
        st_file_attributes=0x0400,
    )

    assert _metadata_is_link_or_reparse_point(ordinary) is False
    assert _metadata_is_link_or_reparse_point(reparse) is True


def test_posix_private_directory_mode_remains_fail_closed(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    destination = tmp_path / "public-backups"
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)

    if _HAS_POSIX_PERMISSION_SECURITY:
        with pytest.raises(BackupError, match="0700"):
            create_backup(source, destination, _PASSPHRASE)
    else:
        archive, audit = create_backup(source, destination, _PASSPHRASE)
        assert archive.is_file()
        assert audit["status"] == "verified"
