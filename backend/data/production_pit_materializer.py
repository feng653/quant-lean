"""Generation-aware materialisation of an authorised production PIT release.

The materialiser never mutates the application's current SQLite database.  It
revalidates an append-only :class:`ProductionPitRelease` authorisation and all
signed provider artifacts, builds the native PIT master and dual-price ledger
inside an invisible staging database, then atomically publishes one generation
manifest naming that database and its release evidence.  Readers therefore see
either the complete previous generation or the complete new generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.data.generation_manifest import (
    GenerationManifestError,
    GenerationManifestStore,
)
from backend.data.point_in_time_master import (
    BITEMPORAL_IMPORT_SCHEMA_VERSION as PIT_IMPORT_SCHEMA,
    PointInTimeMasterStore,
    _authorize_production_release_import as authorize_pit_import,
)
from backend.data.point_in_time_universe import resolve_point_in_time_universe
from backend.data.price_ledger import (
    BITEMPORAL_IMPORT_SCHEMA_VERSION as PRICE_IMPORT_SCHEMA,
    PriceLedgerStore,
    _authorize_production_release as authorize_price_operation,
)
from backend.data.production_pit_release import (
    ApprovedArtifactError,
    ApprovedProviderArtifactStore,
    AtomicPitReleaseRegistry,
    ProductionPitReleaseOrchestrator,
    ProductionPitReleasePolicy,
    ReleaseActivationBlocked,
)


MATERIALIZATION_SCHEMA = "production-pit-materialization/v1"
RUNTIME_EVIDENCE_SCHEMA = "production-pit-runtime-evidence/v1"
RUNTIME_IDENTIFIER = "production_pit_runtime"
_RUNTIME_ARTIFACTS = {"runtime_db", "release_evidence"}


class ProductionPitMaterializationError(RuntimeError):
    """An authorisation or staged runtime generation is not trustworthy."""


@dataclass(frozen=True, slots=True)
class ProductionPitRuntimeView:
    generation_id: str
    generation_manifest_sha256: str
    plan_sha256: str
    runtime_database: Path
    release_evidence: Path
    evidence: Mapping[str, Any]

    def pit_master(self) -> PointInTimeMasterStore:
        return PointInTimeMasterStore(self.runtime_database)

    def price_ledger(self) -> PriceLedgerStore:
        return PriceLedgerStore(self.runtime_database)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProductionPitMaterializationError(
            "materialization document is not canonicalisable"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _source(
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    adjustment: str | None = None,
) -> dict[str, Any]:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ProductionPitMaterializationError(
            "approved artifact payload is empty"
        )
    try:
        available_at = max(str(row["available_at"]) for row in rows)
        retrieved_at = max(str(row["ingested_at"]) for row in rows)
        revisions = {int(row["revision"]) for row in rows}
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionPitMaterializationError(
            "approved artifact lacks bitemporal source evidence"
        ) from exc
    if not revisions or min(revisions) < 1:
        raise ProductionPitMaterializationError(
            "approved artifact revision evidence is invalid"
        )
    result: dict[str, Any] = {
        "provider": manifest["provider"],
        "dataset": manifest["dataset"],
        "version": manifest["provider_version"],
        "evidence_level": manifest["evidence_level"],
        "retrieved_at": retrieved_at,
        "available_at": available_at,
        "content_sha256": manifest["payload_sha256"],
        "revision": max(revisions),
    }
    if adjustment is not None:
        result["adjustment"] = adjustment
    return result


def _pit_records(kind: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    allowed = {
        "security_master": {
            "security_code",
            "effective_from",
            "effective_to",
            "effective_at",
            "available_at",
            "name",
            "exchange",
            "listing_status",
        },
        "index_membership": {
            "security_code",
            "effective_from",
            "effective_to",
            "effective_at",
            "available_at",
            "member_name",
        },
        "industry": {
            "security_code",
            "effective_from",
            "effective_to",
            "effective_at",
            "available_at",
            "industry_code",
            "industry_name",
        },
    }[kind]
    records = [{key: row[key] for key in allowed if key in row} for row in rows]
    required = {
        "security_master": {"name", "exchange", "listing_status"},
        "index_membership": set(),
        "industry": {"industry_code", "industry_name"},
    }[kind]
    if any(required - set(record) for record in records):
        raise ProductionPitMaterializationError(
            f"{kind} lacks fields required by the runtime PIT master"
        )
    return records


def _policy_from_plan(plan: Mapping[str, Any]) -> ProductionPitReleasePolicy:
    policy = plan.get("policy")
    if not isinstance(policy, Mapping):
        raise ProductionPitMaterializationError(
            "authorised release policy is missing"
        )
    try:
        return ProductionPitReleasePolicy(
            coverage_from=str(policy["coverage_from"]),
            coverage_to=str(policy["coverage_to"]),
            pools=tuple(policy["pools"]),
            member_counts={
                str(key): int(value)
                for key, value in dict(policy["member_counts"]).items()
            },
            security_scope=str(policy["security_scope"]),
            industry_scope=str(policy["industry_scope"]),
            ledger_scope=str(policy["ledger_scope"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionPitMaterializationError(
            "authorised release policy is invalid"
        ) from exc


class ProductionPitRuntimeReader:
    """Resolve and reverify the one active PIT runtime generation."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.store = GenerationManifestStore(
            self.root,
            required_artifacts=set(_RUNTIME_ARTIFACTS),
        )

    def load(self) -> ProductionPitRuntimeView | None:
        try:
            generation = self.store.load(RUNTIME_IDENTIFIER)
        except GenerationManifestError as exc:
            raise ProductionPitMaterializationError(
                "active PIT runtime generation is invalid"
            ) from exc
        if generation is None:
            return None
        database = generation.artifacts["runtime_db"]
        evidence_path = generation.artifacts["release_evidence"]
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductionPitMaterializationError(
                "runtime release evidence is unreadable"
            ) from exc
        if (
            not isinstance(evidence, dict)
            or evidence.get("schema_version") != RUNTIME_EVIDENCE_SCHEMA
            or evidence.get("release_id") != evidence.get("plan_sha256")
            or evidence.get("runtime_database_sha256") != _file_digest(database)
            or _digest(evidence.get("authorised_plan"))
            != evidence.get("plan_sha256")
        ):
            raise ProductionPitMaterializationError(
                "runtime release evidence integrity mismatch"
            )
        try:
            with sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro", uri=True
            ) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ProductionPitMaterializationError(
                        "runtime PIT database integrity check failed"
                    )
                metadata = connection.execute(
                    """
                    SELECT schema_version, plan_sha256
                    FROM production_pit_release_metadata
                    WHERE singleton=1
                    """
                ).fetchone()
                required = {
                    "pit_master_batches",
                    "pit_master_intervals",
                    "price_ledger_batches",
                    "price_ledger_prices",
                    "price_ledger_runtime_bindings",
                    "production_pit_artifact_rows",
                }
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if (
                    metadata is None
                    or metadata[0] != MATERIALIZATION_SCHEMA
                    or metadata[1] != evidence["plan_sha256"]
                    or not required <= tables
                ):
                    raise ProductionPitMaterializationError(
                        "runtime PIT database contract is incomplete"
                    )
        except sqlite3.Error as exc:
            raise ProductionPitMaterializationError(
                "runtime PIT database cannot be verified"
            ) from exc
        return ProductionPitRuntimeView(
            generation_id=generation.generation_id,
            generation_manifest_sha256=generation.manifest_sha256,
            plan_sha256=str(evidence["plan_sha256"]),
            runtime_database=database,
            release_evidence=evidence_path,
            evidence=evidence,
        )


class ProductionPitReleaseMaterializer:
    """Build and atomically publish one complete authorised runtime release."""

    def __init__(
        self,
        *,
        registry: AtomicPitReleaseRegistry,
        artifact_store: ApprovedProviderArtifactStore,
        runtime_root: str | Path,
        generation_store: GenerationManifestStore | None = None,
    ) -> None:
        self.registry = registry
        self.artifact_store = artifact_store
        self.runtime_root = Path(runtime_root)
        self.generation_store = generation_store or GenerationManifestStore(
            self.runtime_root,
            required_artifacts=set(_RUNTIME_ARTIFACTS),
        )

    def _revalidate(
        self, plan_sha256: str
    ) -> tuple[
        dict[str, Any],
        ProductionPitReleasePolicy,
        list[tuple[str, dict[str, Any], dict[str, Any]]],
    ]:
        try:
            authorization = self.registry.load_authorization(plan_sha256)
            plan = authorization["plan"]
            policy = _policy_from_plan(plan)
            artifacts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
            for binding in authorization["artifacts"]:
                manifest_sha256 = str(binding["manifest_sha256"])
                manifest, payload = self.artifact_store.read(manifest_sha256)
                if (
                    manifest["payload_sha256"] != binding["payload_sha256"]
                    or manifest["artifact_kind"] != binding["artifact_kind"]
                    or manifest["scope_id"] != binding["scope_id"]
                ):
                    raise ProductionPitMaterializationError(
                        "authorised artifact binding changed"
                    )
                artifacts.append((manifest_sha256, manifest, payload))
            bundle = {
                "schema_version": "production-pit-release-bundle/v1",
                "coverage_from": policy.coverage_from,
                "coverage_to": policy.coverage_to,
                "artifact_manifest_sha256s": sorted(
                    item[0] for item in artifacts
                ),
            }
            fresh = ProductionPitReleaseOrchestrator(
                self.artifact_store, policy=policy
            ).dry_run(bundle)
            if not fresh["ready"]:
                raise ProductionPitMaterializationError(
                    "authorised release no longer has zero blockers"
                )
            expected_artifacts = sorted(
                plan["artifacts"],
                key=lambda item: (
                    item["artifact_kind"],
                    item["scope_id"],
                    item["manifest_sha256"],
                ),
            )
            if (
                fresh["plan"]["artifacts"] != expected_artifacts
                or fresh["plan"]["coverage"] != plan["coverage"]
            ):
                raise ProductionPitMaterializationError(
                    "fresh release validation differs from authorization"
                )
            return authorization, policy, artifacts
        except (ReleaseActivationBlocked, ApprovedArtifactError) as exc:
            raise ProductionPitMaterializationError(
                "production PIT release authorization is invalid"
            ) from exc

    @staticmethod
    def _record_exact_artifacts(
        database: Path,
        *,
        plan_sha256: str,
        authorization: Mapping[str, Any],
        artifacts: Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    ) -> None:
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE production_pit_release_metadata (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    schema_version TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    authorised_at TEXT NOT NULL,
                    authorised_by_user_id INTEGER NOT NULL,
                    materialized_at TEXT NOT NULL
                );
                CREATE TABLE production_pit_artifact_rows (
                    manifest_sha256 TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    artifact_kind TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    row_json TEXT NOT NULL,
                    row_sha256 TEXT NOT NULL,
                    PRIMARY KEY(manifest_sha256, row_index)
                );
                CREATE TRIGGER production_pit_metadata_no_update
                BEFORE UPDATE ON production_pit_release_metadata
                BEGIN SELECT RAISE(ABORT, 'production PIT metadata is immutable'); END;
                CREATE TRIGGER production_pit_metadata_no_delete
                BEFORE DELETE ON production_pit_release_metadata
                BEGIN SELECT RAISE(ABORT, 'production PIT metadata cannot be deleted'); END;
                CREATE TRIGGER production_pit_rows_no_update
                BEFORE UPDATE ON production_pit_artifact_rows
                BEGIN SELECT RAISE(ABORT, 'production PIT evidence is immutable'); END;
                CREATE TRIGGER production_pit_rows_no_delete
                BEFORE DELETE ON production_pit_artifact_rows
                BEGIN SELECT RAISE(ABORT, 'production PIT evidence cannot be deleted'); END;
                """
            )
            connection.execute(
                """
                INSERT INTO production_pit_release_metadata VALUES(1,?,?,?,?,?)
                """,
                (
                    MATERIALIZATION_SCHEMA,
                    plan_sha256,
                    authorization["authorised_at"],
                    int(authorization["authorised_by_user_id"]),
                    _timestamp_now(),
                ),
            )
            for manifest_sha256, manifest, payload in artifacts:
                for index, row in enumerate(payload["rows"]):
                    row_json = _canonical_bytes(row).decode("utf-8")
                    connection.execute(
                        """
                        INSERT INTO production_pit_artifact_rows VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            manifest_sha256,
                            manifest["payload_sha256"],
                            manifest["artifact_kind"],
                            manifest["scope_id"],
                            index,
                            row_json,
                            hashlib.sha256(row_json.encode()).hexdigest(),
                        ),
                    )
            connection.commit()

    def _build_database(
        self,
        database: Path,
        *,
        authorization: Mapping[str, Any],
        policy: ProductionPitReleasePolicy,
        artifacts: Sequence[tuple[str, dict[str, Any], dict[str, Any]]],
    ) -> dict[str, Any]:
        plan_sha256 = str(authorization["plan_sha256"])
        pit = PointInTimeMasterStore(database, initialize=True)
        ledger = PriceLedgerStore(database, initialize=True)
        self._record_exact_artifacts(
            database,
            plan_sha256=plan_sha256,
            authorization=authorization,
            artifacts=artifacts,
        )
        pit_results: list[dict[str, Any]] = []
        price_results: list[dict[str, Any]] = []
        by_identity: dict[
            tuple[str, str], list[tuple[str, dict[str, Any], dict[str, Any]]]
        ] = {}
        for item in artifacts:
            by_identity.setdefault(
                (item[1]["artifact_kind"], item[1]["scope_id"]), []
            ).append(item)

        for manifest_sha256, manifest, payload in artifacts:
            kind = str(manifest["artifact_kind"])
            if kind not in {"security_master", "index_membership", "industry"}:
                continue
            domain = {
                "security_master": "security",
                "index_membership": "index_membership",
                "industry": "industry",
            }[kind]
            runtime_scope = (
                "cn_equity" if kind == "security_master" else manifest["scope_id"]
            )
            source = _source(manifest, payload)
            records = _pit_records(kind, payload["rows"])
            document = {
                "schema_version": PIT_IMPORT_SCHEMA,
                "domain": domain,
                "scope_id": runtime_scope,
                "evidence_kind": "effective_dated_history",
                "coverage_from": manifest["coverage_from"],
                "coverage_to": manifest["coverage_to"],
                "source": source,
                "records": records,
            }
            result = pit.import_batch(
                **document,
                imported_by_user_id=int(
                    authorization["authorised_by_user_id"]
                ),
                _production_release_authorization=authorize_pit_import(
                    plan_sha256=plan_sha256,
                    manifest_sha256=manifest_sha256,
                    document_sha256=_digest(document),
                ),
            )
            pit_results.append(
                {
                    "artifact_kind": kind,
                    "source_scope_id": manifest["scope_id"],
                    "runtime_scope_id": runtime_scope,
                    **result,
                }
            )

        action_items = by_identity[("corporate_action_evidence", policy.security_scope)]
        action_rows = [
            (manifest, row)
            for _digest_value, manifest, payload in action_items
            for row in payload["rows"]
            if row.get("evidence_kind") == "event"
        ]
        price_batch_ids: list[str] = []
        for manifest_sha256, manifest, payload in by_identity[
            ("dual_price_ledger", policy.ledger_scope)
        ]:
            raw_prices = []
            research_prices = []
            for row in payload["rows"]:
                raw_prices.append(
                    {"security_code": row["security_code"], "date": row["trading_date"], **row["raw"]}
                )
                research_prices.append(
                    {
                        "security_code": row["security_code"],
                        "date": row["trading_date"],
                        **row["research_adjusted"],
                    }
                )
            relevant_actions = [
                {
                    "security_code": row["security_code"],
                    "effective_date": row["effective_from"],
                    "action_type": row["action_type"],
                    "adjustment_multiplier": row.get("adjustment_multiplier"),
                    "reference_id": row["reference_id"],
                }
                for _action_manifest, row in action_rows
                if manifest["coverage_from"] <= row["effective_from"] <= manifest["coverage_to"]
            ]
            raw_source = _source(manifest, payload, adjustment="raw")
            research_source = _source(manifest, payload, adjustment="hfq")
            action_source = None
            if relevant_actions:
                action_manifests = {item[0]["payload_sha256"]: item[0] for item in action_rows}
                providers = {
                    (item["provider"], item["dataset"], item["provider_version"], item["evidence_level"])
                    for item in action_manifests.values()
                }
                if len(providers) != 1:
                    raise ProductionPitMaterializationError(
                        "corporate-action shards have incompatible source identities"
                    )
                provider, dataset, version, level = next(iter(providers))
                action_payloads = sorted(action_manifests)
                action_source = {
                    "provider": provider,
                    "dataset": dataset,
                    "version": version,
                    "evidence_level": level,
                    "adjustment": "corporate_action",
                    "retrieved_at": max(
                        row["ingested_at"] for _item, row in action_rows
                    ),
                    "available_at": max(
                        row["available_at"] for _item, row in action_rows
                    ),
                    "content_sha256": _digest(action_payloads),
                }
            price_document = {
                "schema_version": PRICE_IMPORT_SCHEMA,
                "scope_id": policy.ledger_scope,
                "coverage_from": manifest["coverage_from"],
                "coverage_to": manifest["coverage_to"],
                "raw_source": raw_source,
                "research_source": research_source,
                "corporate_action_source": action_source,
                "raw_prices": raw_prices,
                "research_prices": research_prices,
                "corporate_actions": relevant_actions,
                "revision": int(_source(manifest, payload)["revision"]),
                "supersedes_batch_id": None,
            }
            result = ledger.import_batch(
                **price_document,
                imported_by_user_id=int(
                    authorization["authorised_by_user_id"]
                ),
                _production_release_authorization=authorize_price_operation(
                    operation="import_batch",
                    plan_sha256=plan_sha256,
                    manifest_sha256=manifest_sha256,
                    document_sha256=_digest(price_document),
                ),
            )
            price_batch_ids.append(result["batch_id"])
            price_results.append(result)

        sessions = sorted(
            row["trading_date"]
            for _digest_value, _manifest, payload in by_identity[
                ("trading_calendar", "cn_equity")
            ]
            for row in payload["rows"]
        )
        status_items = by_identity[("market_status", policy.security_scope)]
        status_manifest_sha256 = status_items[0][0]
        status_rows = [
            row
            for _digest_value, _manifest, payload in status_items
            for row in payload["rows"]
        ]
        status_manifests = [item[1] for item in status_items]
        status_source = {
            "provider": status_manifests[0]["provider"],
            "dataset": status_manifests[0]["dataset"],
            "version": status_manifests[0]["provider_version"],
            "evidence_level": status_manifests[0]["evidence_level"],
            "adjustment": "trading_status",
            "retrieved_at": max(row["ingested_at"] for row in status_rows),
            "available_at": max(row["available_at"] for row in status_rows),
            "content_sha256": _digest(
                sorted(item["payload_sha256"] for item in status_manifests)
            ),
        }
        as_known_at = _timestamp_now()
        bindings: list[dict[str, Any]] = []
        for pool in policy.pools:
            timeline = resolve_point_in_time_universe(
                pit,
                pool_id=pool,
                trading_dates=sessions,
                expected_count=int(policy.member_counts[pool]),
                as_known_at=as_known_at,
            )
            members = {
                (day, code)
                for day, codes in zip(
                    timeline.dates, timeline.members_by_date, strict=True
                )
                for code in codes
            }
            nontradable = [
                {
                    "security_code": row["security_code"],
                    "date": row["trading_date"],
                    "status": row["status"],
                }
                for row in status_rows
                if (row["trading_date"], row["security_code"]) in members
                and row["status"] != "tradable"
            ]
            unsupported = sorted(
                {item["status"] for item in nontradable} - {"suspended"}
            )
            if unsupported:
                raise ProductionPitMaterializationError(
                    "runtime binding cannot represent non-trading status: "
                    + ",".join(unsupported)
                )
            binding_document = {
                "scope_id": pool,
                "timeline_identity": timeline.identity(),
                "trading_dates": list(timeline.dates),
                "batch_ids": price_batch_ids,
                "status_source": status_source,
                "suspension_observations": nontradable,
                "as_known_at": as_known_at,
            }
            binding = ledger.bind_runtime_scope(
                **binding_document,
                bound_by_user_id=int(
                    authorization["authorised_by_user_id"]
                ),
                _production_release_authorization=authorize_price_operation(
                    operation="bind_runtime_scope",
                    plan_sha256=plan_sha256,
                    manifest_sha256=status_manifest_sha256,
                    document_sha256=_digest(binding_document),
                ),
            )
            bindings.append({"scope_id": pool, **binding})
        with sqlite3.connect(database) as connection:
            check = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise ProductionPitMaterializationError(
                    "staged runtime database integrity check failed"
                )
        os.chmod(database, 0o600)
        return {
            "pit_batches": pit_results,
            "price_batches": price_results,
            "runtime_bindings": bindings,
            "trading_session_count": len(sessions),
        }

    def materialize(self, plan_sha256: str) -> dict[str, Any]:
        authorization, policy, artifacts = self._revalidate(plan_sha256)
        current = ProductionPitRuntimeReader(self.runtime_root).load()
        if current is not None and current.plan_sha256 == plan_sha256:
            return {
                "schema_version": MATERIALIZATION_SCHEMA,
                "plan_sha256": plan_sha256,
                "generation_id": current.generation_id,
                "generation_manifest_sha256": (
                    current.generation_manifest_sha256
                ),
                "runtime_materialised": True,
                "idempotent": True,
            }
        staging_root = self.runtime_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix="pit-release-", dir=staging_root))
        database = stage / "runtime_db"
        evidence_path = stage / "release_evidence"
        build = self._build_database(
            database,
            authorization=authorization,
            policy=policy,
            artifacts=artifacts,
        )
        evidence = {
            "schema_version": RUNTIME_EVIDENCE_SCHEMA,
            "release_id": plan_sha256,
            "plan_sha256": plan_sha256,
            "authorised_plan": authorization["plan"],
            "authorised_at": authorization["authorised_at"],
            "authorised_by_user_id": authorization["authorised_by_user_id"],
            "materialized_at": _timestamp_now(),
            "runtime_database_sha256": _file_digest(database),
            "artifact_manifest_sha256s": sorted(item[0] for item in artifacts),
            "build": build,
        }
        evidence_path.write_bytes(_canonical_bytes(evidence))
        os.chmod(evidence_path, 0o600)
        published = self.generation_store.publish_staged(
            RUNTIME_IDENTIFIER,
            {"runtime_db": database, "release_evidence": evidence_path},
        )
        # Reader validates the physical manifest and plan-scoped evidence.
        view = ProductionPitRuntimeReader(self.runtime_root).load()
        if view is None or view.generation_id != published.generation_id:
            raise ProductionPitMaterializationError(
                "published runtime generation could not be reloaded"
            )
        return {
            "schema_version": MATERIALIZATION_SCHEMA,
            "plan_sha256": plan_sha256,
            "generation_id": published.generation_id,
            "generation_manifest_sha256": published.manifest_sha256,
            "runtime_materialised": True,
            "idempotent": False,
            "build": build,
        }
