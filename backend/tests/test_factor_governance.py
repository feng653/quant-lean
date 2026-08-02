from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from pydantic import ValidationError

from backend.api.factor_research import (
    ExportFactorStrategyBody,
    FactorLifecycleBody,
)
from backend.data.factor_governance import (
    FactorGovernanceError,
    FactorGovernanceStore,
)
from backend.data.factor_research_runs import FactorResearchRunStore
from backend.db.migrate import migrate_experiment
from backend.research.factor_catalog import get_factor_definition


def _result(factor_id: str = "momentum_20") -> dict:
    return {
        "factor": get_factor_definition(factor_id),
        "dataset": {"content_sha256": "a" * 64},
        "request": {"factor_id": factor_id},
    }


def _run(path: Path, *, owner: int = 7, factor_id: str = "momentum_20") -> dict:
    return FactorResearchRunStore(path).create(
        owner_user_id=owner,
        factor_id=factor_id,
        request={"factor_id": factor_id},
        result=_result(factor_id),
    )


def _publish(
    store: FactorGovernanceStore,
    run_id: str,
    *,
    key: str = "strategy-create-0001",
    name: str = "证据组合",
    strategy_id: str | None = None,
    expected_version: int | None = None,
) -> dict:
    return store.publish_strategy(
        name=name,
        components=[{"factor_id": "momentum_20", "weight": 1.0}],
        top_k_pct=0.1,
        research_run_ids=[run_id],
        owner_user_id=7,
        actor_user_id=7,
        idempotency_key=key,
        strategy_id=strategy_id,
        expected_version=expected_version,
    )


def test_catalog_has_immutable_versions_and_transactional_lifecycle(
    tmp_path: Path,
) -> None:
    store = FactorGovernanceStore(tmp_path / "governance.db")
    old_run = _run(store.path)
    factor = next(
        item for item in store.list_catalog() if item["factor_id"] == "momentum_20"
    )
    assert factor["version"] == "1.0.0"
    assert len(factor["definition_digest"]) == 64
    assert factor["parameter_schema"]["additionalProperties"] is False
    assert factor["status"] == "published"
    assert factor["current"] is True
    with pytest.raises(FactorGovernanceError) as digest_error:
        store.set_factor_status(
            factor_id=factor["factor_id"],
            version=factor["version"],
            definition_digest="0" * 64,
            status="deprecated",
            expected_revision=factor["revision"],
            actor_user_id=1,
            idempotency_key="factor-wrong-digest",
        )
    assert digest_error.value.code == "factor_code_manifest_not_found"

    deprecated = store.set_factor_status(
        factor_id=factor["factor_id"],
        version=factor["version"],
        definition_digest=factor["definition_digest"],
        status="deprecated",
        expected_revision=factor["revision"],
        actor_user_id=1,
        idempotency_key="factor-deprecate-0001",
    )
    replay = store.set_factor_status(
        factor_id=factor["factor_id"],
        version=factor["version"],
        definition_digest=factor["definition_digest"],
        status="deprecated",
        expected_revision=factor["revision"],
        actor_user_id=1,
        idempotency_key="factor-deprecate-0001",
    )
    assert replay == deprecated
    assert deprecated["revision"] == 2
    assert not any(
        item["factor_id"] == factor["factor_id"]
        for item in store.list_catalog(include_deprecated=False)
    )
    # Lifecycle state never changes the immutable definition snapshot on an
    # already-completed run.
    old_factor = FactorResearchRunStore(store.path).get(
        owner_user_id=7,
        run_id=old_run["run_id"],
    )
    assert old_factor is not None
    assert old_factor["factor_version"] == "1.0.0"

    with pytest.raises(FactorGovernanceError, match="已被其他请求"):
        store.set_factor_status(
            factor_id=factor["factor_id"],
            version=factor["version"],
            definition_digest=factor["definition_digest"],
            status="published",
            expected_revision=1,
            actor_user_id=1,
            idempotency_key="factor-publish-stale",
        )
    with sqlite3.connect(store.path) as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="immutable",
    ):
        connection.execute(
            """
            UPDATE factor_catalog_versions SET manifest_json='{}'
            WHERE factor_id='momentum_20'
            """
        )


def test_code_manifest_rejects_missing_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.data import factor_governance
    from backend.research.factor_catalog import factor_definition_digest

    manifest: dict[str, object] = {
        "factor_id": "dependent_factor",
        "version": "1.0.0",
        "name": "依赖测试",
        "description": "依赖不存在时拒绝注册。",
        "direction": "high",
        "lookback": 1,
        "required_fields": ["close"],
        "category": "quality",
        "parameters": {},
        "parameter_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        "dependencies": [{"factor_id": "missing_factor", "version": "1.0.0"}],
        "supersedes": None,
    }
    manifest["definition_digest"] = factor_definition_digest(manifest)
    monkeypatch.setattr(factor_governance, "FACTOR_CATALOG", [manifest])
    with pytest.raises(FactorGovernanceError) as error:
        FactorGovernanceStore(tmp_path / "missing-dependency.db")
    assert error.value.code == "factor_dependency_missing"


def test_strategy_publish_requires_owned_integral_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.db"
    owned = _run(path)
    foreign = _run(path, owner=8)
    store = FactorGovernanceStore(path)

    with pytest.raises(FactorGovernanceError, match="至少需要一个"):
        store.publish_strategy(
            name="无证据",
            components=[{"factor_id": "momentum_20", "weight": 1}],
            top_k_pct=0.1,
            research_run_ids=[],
            owner_user_id=7,
            actor_user_id=7,
            idempotency_key="strategy-no-evidence",
        )
    with pytest.raises(FactorGovernanceError) as foreign_error:
        _publish(store, foreign["run_id"], key="strategy-foreign-01")
    assert foreign_error.value.status_code == 404

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE factor_research_runs SET archived_at='2026-07-31T00:00:00Z'
            WHERE run_id=?
            """,
            (owned["run_id"],),
        )
    definition = _publish(store, owned["run_id"])
    assert definition["legacy_unbound"] is False
    assert definition["strategy_version"] == 1
    assert definition["research_evidence"][0]["run_id"] == owned["run_id"]
    assert definition["research_evidence"][0]["factor_version"] == "1.0.0"


def test_strategy_publish_is_idempotent_versioned_and_rollback_preserves_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "versions.db"
    run = _run(path)
    store = FactorGovernanceStore(path)
    first = _publish(store, run["run_id"])
    assert _publish(store, run["run_id"]) == first

    with pytest.raises(FactorGovernanceError) as conflict:
        _publish(
            store,
            run["run_id"],
            name="不同请求",
            key="strategy-create-0001",
        )
    assert conflict.value.code == "idempotency_conflict"

    second = _publish(
        store,
        run["run_id"],
        key="strategy-version-0002",
        name="证据组合 v2",
        strategy_id=first["strategy_id"],
        expected_version=1,
    )
    assert second["strategy_version"] == 2
    with pytest.raises(FactorGovernanceError) as stale:
        _publish(
            store,
            run["run_id"],
            key="strategy-stale-version",
            name="并发旧版本",
            strategy_id=first["strategy_id"],
            expected_version=1,
        )
    assert stale.value.code == "factor_strategy_version_conflict"
    history = store.list_strategy_versions(
        strategy_id=first["strategy_id"],
        owner_user_id=7,
    )
    assert history is not None
    assert history["current_version"] == 2
    assert len(history["versions"]) == 2

    rolled_back = store.rollback_strategy(
        strategy_id=first["strategy_id"],
        target_version=1,
        expected_version=2,
        owner_user_id=7,
        actor_user_id=7,
        idempotency_key="strategy-rollback-0001",
    )
    assert rolled_back["strategy_version"] == 1
    assert store.rollback_strategy(
        strategy_id=first["strategy_id"],
        target_version=1,
        expected_version=2,
        owner_user_id=7,
        actor_user_id=7,
        idempotency_key="strategy-rollback-0001",
    ) == rolled_back
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM factor_strategy_versions
            WHERE strategy_id=?
            """,
            (first["strategy_id"],),
        ).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                DELETE FROM factor_strategy_versions
                WHERE strategy_id=? AND version=2
                """,
                (first["strategy_id"],),
            )


def test_governance_bodies_reject_executable_or_unknown_web_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ExportFactorStrategyBody.model_validate(
            {
                "name": "危险策略",
                "components": [{"factor_id": "momentum_20", "weight": 1}],
                "research_run_ids": ["frun_" + "1" * 32],
                "python": "__import__('os').system('id')",
            }
        )
    with pytest.raises(ValidationError):
        FactorLifecycleBody.model_validate(
            {
                "definition_digest": "a" * 64,
                "expected_revision": 1,
                "idempotency_key": "factor-state-0001",
                "expression": "close.pct_change()",
            }
        )


def test_existing_json_strategy_is_explicitly_legacy_unbound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.strategies.factor import _configured_factor

    path = tmp_path / "factor_strategies.json"
    monkeypatch.setattr(_configured_factor, "_definitions_path", lambda: path)
    from backend.strategies import registry as registry_module

    class Registry:
        @staticmethod
        def register_strategy_class(_strategy_class: object) -> None:
            return None

    monkeypatch.setattr(registry_module, "get_registry", Registry)
    definition = _configured_factor.export_factor_strategy(
        name="旧版组合",
        components=[{"factor_id": "momentum_20", "weight": 1}],
        top_k_pct=0.1,
        owner_user_id=7,
    )
    loaded = _configured_factor.load_factor_strategy_definitions()[0]
    assert loaded == definition
    assert definition["legacy_unbound"] is True
    metadata = _configured_factor.make_factor_strategy_class(loaded).metadata()
    assert "legacy_unbound" in metadata.tags
    assert "不可晋级" in metadata.tags


def test_experiment_migration_creates_governance_tables(
    tmp_path: Path,
) -> None:
    async def migrate() -> set[str]:
        path = tmp_path / "migration.db"
        async with aiosqlite.connect(path) as connection:
            await connection.executescript(
                """
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                CREATE TABLE experiments (id INTEGER PRIMARY KEY);
                """
            )
            await migrate_experiment(connection)
            await connection.commit()
            cursor = await connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name LIKE 'factor_%'
                """
            )
            return {row[0] for row in await cursor.fetchall()}

    names = asyncio.run(migrate())
    assert {
        "factor_catalog_versions",
        "factor_strategy_series",
        "factor_strategy_versions",
        "factor_governance_requests",
        "factor_governance_events",
    } <= names
