from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from backend.data import non_pit_cleanup as cleanup


def _ready_report() -> dict[str, object]:
    return {
        "runtime_ready": True,
        "universe_point_in_time": True,
        "point_in_time": {
            "universe": {"bitemporal_availability_verified": True},
        },
        "price_ledger": {
            "dual_ledger_complete": True,
            "bitemporal_availability_verified": True,
            "roles": {
                "raw_execution": {"available": True, "trusted": True},
                "research_adjusted": {"available": True, "trusted": True},
            },
        },
        "canonical_runtime_price_bound": True,
        "authoritative_trading_calendar_bound": True,
        "point_in_time_benchmark_bound": True,
        "ready_for_unbiased_return_research": True,
    }


async def _ready_provider(_pool: str, _start: str, _end: str) -> dict[str, object]:
    return _ready_report()


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "cache" / "daily").mkdir(parents=True)
    (root / "cache" / "daily" / "csi300.parquet").write_bytes(b"legacy-prices")
    (root / "pit_evidence").mkdir()
    (root / "pit_evidence" / "governance.db").write_bytes(b"not-a-stock-target")
    connection = sqlite3.connect(root / "experiment.db")
    connection.execute("CREATE TABLE experiments (id INTEGER PRIMARY KEY, result TEXT)")
    connection.commit()
    connection.close()
    return root


def test_inventory_targets_only_cache_and_preserves_durable_stores(tmp_path: Path) -> None:
    root = _data_root(tmp_path)

    inventory = cleanup.build_inventory(root)

    assert [item.relative_path for item in inventory.targets] == ["cache"]
    assert inventory.targets[0].file_count == 1
    assert "pit_evidence" in inventory.protected_paths
    assert "experiment.db" in inventory.protected_paths
    assert inventory.database_inventory[0]["recognized_domains"] == ["experiments"]
    assert len(inventory.inventory_sha256) == 64


def test_gate_fails_closed_for_current_or_incomplete_pit_reports() -> None:
    async def incomplete(_pool: str, _start: str, _end: str) -> dict[str, object]:
        return {"runtime_ready": False, "failure_code": "point_in_time_store_uninitialized"}

    gate = asyncio.run(
        cleanup.inspect_cleanup_gate(
            coverage_start="2016-01-01",
            coverage_end="2026-07-31",
            runtime_report_provider=incomplete,
            listener_probe=lambda: ("127.0.0.1:8000",),
        )
    )

    assert not gate.ready
    assert "csi300:pit_membership_available_at_not_verified" in gate.blockers
    assert "csi1000:runtime_binding_not_ready" in gate.blockers
    assert "service_listener_active:127.0.0.1:8000" in gate.blockers


def test_execute_archives_then_restores_only_after_explicit_confirmations(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    inventory = cleanup.build_inventory(root)
    gate = asyncio.run(
        cleanup.inspect_cleanup_gate(
            coverage_start="2016-01-01",
            coverage_end="2026-07-31",
            runtime_report_provider=_ready_provider,
            listener_probe=tuple,
        )
    )
    assert gate.ready

    with pytest.raises(cleanup.NonPitCleanupBlocked):
        cleanup.execute_cleanup(
            inventory=inventory,
            gate=gate,
            maintenance_window_id="maint-20260802-001",
            inventory_sha256=inventory.inventory_sha256,
            second_confirmation="wrong",
        )

    receipt = cleanup.execute_cleanup(
        inventory=inventory,
        gate=gate,
        maintenance_window_id="maint-20260802-001",
        inventory_sha256=inventory.inventory_sha256,
        second_confirmation="ARCHIVE_NON_PIT_CACHE",
    )
    run_id = str(receipt["run_id"])
    assert not any((root / "cache").iterdir())
    archived = root / "maintenance" / "non_pit_cleanup" / run_id / "cache" / "daily" / "csi300.parquet"
    assert archived.read_bytes() == b"legacy-prices"
    assert (root / "experiment.db").exists()

    restored = cleanup.restore_archive(
        data_root=root,
        run_id=run_id,
        second_confirmation="RESTORE_NON_PIT_CACHE",
    )
    assert restored["kind"] == "restore_receipt"
    assert (root / "cache" / "daily" / "csi300.parquet").read_bytes() == b"legacy-prices"


def test_write_failure_rolls_back_the_source_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _data_root(tmp_path)
    inventory = cleanup.build_inventory(root)
    gate = asyncio.run(
        cleanup.inspect_cleanup_gate(
            coverage_start="2016-01-01",
            coverage_end="2026-07-31",
            runtime_report_provider=_ready_provider,
            listener_probe=tuple,
        )
    )
    original_writer = cleanup._write_json

    def fail_receipt(path: Path, payload: dict[str, object]) -> None:
        if path.name == "receipt.json":
            raise OSError("injected receipt write failure")
        original_writer(path, payload)

    monkeypatch.setattr(cleanup, "_write_json", fail_receipt)
    with pytest.raises(cleanup.NonPitCleanupError, match="rolled back"):
        cleanup.execute_cleanup(
            inventory=inventory,
            gate=gate,
            maintenance_window_id="maint-20260802-002",
            inventory_sha256=inventory.inventory_sha256,
            second_confirmation="ARCHIVE_NON_PIT_CACHE",
        )
    assert (root / "cache" / "daily" / "csi300.parquet").read_bytes() == b"legacy-prices"


def test_inventory_drift_rejects_before_any_archive_move(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    inventory = cleanup.build_inventory(root)
    gate = asyncio.run(
        cleanup.inspect_cleanup_gate(
            coverage_start="2016-01-01",
            coverage_end="2026-07-31",
            runtime_report_provider=_ready_provider,
            listener_probe=tuple,
        )
    )
    (root / "cache" / "daily" / "new.parquet").write_bytes(b"changed-after-audit")

    with pytest.raises(cleanup.NonPitCleanupIntegrityError, match="inventory changed"):
        cleanup.execute_cleanup(
            inventory=inventory,
            gate=gate,
            maintenance_window_id="maint-20260802-003",
            inventory_sha256=inventory.inventory_sha256,
            second_confirmation="ARCHIVE_NON_PIT_CACHE",
        )
    assert (root / "cache" / "daily" / "csi300.parquet").exists()
    assert not (root / "maintenance" / "non_pit_cleanup").exists()


def test_dry_run_report_is_json_and_never_archives(tmp_path: Path) -> None:
    root = _data_root(tmp_path)
    inventory = cleanup.build_inventory(root)
    gate = asyncio.run(
        cleanup.inspect_cleanup_gate(
            coverage_start="2016-01-01",
            coverage_end="2026-07-31",
            runtime_report_provider=_ready_provider,
            listener_probe=tuple,
        )
    )
    report_path = tmp_path / "dry-run.json"

    payload = cleanup.write_dry_run_report(
        inventory=inventory,
        gate=gate,
        output_path=report_path,
    )

    assert payload["execute_performed"] is False
    assert json.loads(report_path.read_text("utf-8"))["production_data_deleted"] is False
    assert (root / "cache" / "daily" / "csi300.parquet").exists()


def test_maintenance_script_bootstraps_repo_import_when_run_directly() -> None:
    script = Path(__file__).parents[2] / "scripts" / "cleanup_non_pit_data.py"

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "")},
    )

    assert result.returncode == 0, result.stderr
    assert "--coverage-start" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr
