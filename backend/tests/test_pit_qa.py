from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.config import settings
from backend.data.pit_qa import (
    QA_ATTESTATION_SCHEMA,
    QA_MARKER_SCHEMA,
    PitQaIsolationError,
    qa_attestation_sha256,
    verified_qa_runtime_attestation,
)


def _configure(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "PIT_QA_FIXTURE_ROOT", str(root))
    attestation = root / "pit-qa-attestation.json"
    monkeypatch.setattr(settings, "PIT_QA_ATTESTATION", str(attestation))
    paths = {
        "DATABASE_DIR": root,
        "USERS_DB": root / "users.db",
        "EXPERIMENT_DB": root / "experiment.db",
        "TRADING_SIM_DB": root / "trading.db",
        "TRADING_LIVE_DB": root / "live.db",
        "DATA_CACHE_DIR": root / "cache",
        "DATA_STAGING_DIR": root / "staging",
        "PIT_EVIDENCE_DIR": root / "pit-evidence",
        "PIT_EVIDENCE_DB": root / "pit-evidence" / "governance.db",
        "MODEL_STORE_DIR": root / "models",
        "RESEARCH_SNAPSHOT_DIR": root / "snapshots",
    }
    for name, value in paths.items():
        monkeypatch.setattr(settings, name, str(value))
    (root / ".pit-qa-only.json").write_text(
        json.dumps(
            {"schema_version": QA_MARKER_SCHEMA, "non_production": True}
        ),
        encoding="utf-8",
    )
    benchmark = root / "cache" / "indexes" / "000300.parquet"
    benchmark.parent.mkdir(parents=True)
    benchmark.write_bytes(b"isolated benchmark")
    payload = {
        "schema_version": QA_ATTESTATION_SCHEMA,
        "non_production": True,
        "production_eligible": False,
        "pool_id": "csi300",
        "coverage_from": "2022-01-01",
        "coverage_to": "2024-12-31",
        "timeline_hash": "a" * 64,
        "binding_id": "plr_" + "b" * 32,
        "binding_digest": "c" * 64,
        "benchmark_artifact": "cache/indexes/000300.parquet",
        "benchmark_artifact_sha256": hashlib.sha256(
            benchmark.read_bytes()
        ).hexdigest(),
    }
    payload["attestation_sha256"] = qa_attestation_sha256(payload)
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    return attestation


def test_qa_attestation_is_inert_outside_test_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "PIT_QA_FIXTURE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        settings,
        "PIT_QA_ATTESTATION",
        str(tmp_path / "pit-qa-attestation.json"),
    )
    assert verified_qa_runtime_attestation(
        pool_id="csi300",
        required_start="2024-01-01",
        required_end="2024-03-01",
        timeline_identity={"timeline_hash": "a" * 64},
        runtime_price_binding={
            "binding_id": "plr_" + "b" * 32,
            "binding_digest": "c" * 64,
        },
    ) is None


def test_exact_isolated_qa_attestation_is_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, tmp_path)
    result = verified_qa_runtime_attestation(
        pool_id="csi300",
        required_start="2024-01-01",
        required_end="2024-03-01",
        timeline_identity={"timeline_hash": "a" * 64},
        runtime_price_binding={
            "binding_id": "plr_" + "b" * 32,
            "binding_digest": "c" * 64,
        },
    )
    assert result is not None
    assert result["production_eligible"] is False
    assert result["fixture_kind"] == "deterministic_isolated_pit_e2e"


def test_qa_attestation_tamper_and_path_escape_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _configure(monkeypatch, tmp_path)
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    payload["binding_digest"] = "d" * 64
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PitQaIsolationError, match="integrity"):
        verified_qa_runtime_attestation(
            pool_id="csi300",
            required_start="2024-01-01",
            required_end="2024-03-01",
            timeline_identity={"timeline_hash": "a" * 64},
            runtime_price_binding={
                "binding_id": "plr_" + "b" * 32,
                "binding_digest": "c" * 64,
            },
        )

    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path.parent / "escape"))
    with pytest.raises(PitQaIsolationError, match="escaped"):
        verified_qa_runtime_attestation(
            pool_id="csi300",
            required_start="2024-01-01",
            required_end="2024-03-01",
            timeline_identity={"timeline_hash": "a" * 64},
            runtime_price_binding={
                "binding_id": "plr_" + "b" * 32,
                "binding_digest": "c" * 64,
            },
        )
