"""Small, idempotent SQLite migrations for the three application databases.

The original project relied only on ``CREATE TABLE IF NOT EXISTS`` in
``backend.main``.  That creates fresh databases but never upgrades an existing
one.  These migrations deliberately inspect columns before altering tables so
they are safe to run on every startup.
"""

from __future__ import annotations

from collections.abc import Iterable

import aiosqlite


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cursor = await conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name=?
        """,
        (table,),
    )
    return await cursor.fetchone() is not None


async def _add_columns(
    conn: aiosqlite.Connection,
    table: str,
    definitions: Iterable[tuple[str, str]],
) -> None:
    existing = await _columns(conn, table)
    for name, definition in definitions:
        if name not in existing:
            try:
                await conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )
            except aiosqlite.OperationalError as exc:
                # Another startup worker may win after the PRAGMA above.
                if "duplicate column name" not in str(exc).lower():
                    raise
            existing.add(name)


async def migrate_experiment(conn: aiosqlite.Connection) -> None:
    # Some maintenance/test deployments initialize only a minimal experiment
    # schema before running migrations. Keep this migration independently safe.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS param_sweeps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            strategy_id TEXT NOT NULL,
            name TEXT,
            sweep_config TEXT NOT NULL,
            total_experiments INTEGER DEFAULT 0,
            completed_experiments INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    await _add_columns(
        conn,
        "trade_log",
        [("signal_date", "TEXT")],
    )
    await _add_columns(
        conn,
        "experiment_metrics",
        [("cumulative_return", "REAL")],
    )
    await _add_columns(
        conn,
        "experiments",
        [
            ("run_spec", "TEXT"),
            ("source_experiment_id", "INTEGER"),
            ("code_version", "TEXT"),
        ],
    )
    if await _table_exists(conn, "model_artifacts"):
        await _add_columns(
            conn,
            "model_artifacts",
            [
                ("artifact_sha256", "TEXT"),
                ("artifact_size", "INTEGER"),
                ("run_manifest_hash", "TEXT"),
            ],
        )
    if await _table_exists(conn, "factor_research_runs"):
        await _add_columns(
            conn,
            "factor_research_runs",
            [
                ("factor_version", "TEXT"),
                ("factor_definition_digest", "TEXT"),
                ("factor_definition_json", "TEXT"),
            ],
        )
    await _add_columns(
        conn,
        "param_sweeps",
        [
            ("selection_start", "TEXT"),
            ("selection_end", "TEXT"),
            ("locked_test_start", "TEXT"),
            ("locked_test_end", "TEXT"),
            (
                "research_trust",
                "TEXT NOT NULL DEFAULT 'legacy_unlocked'",
            ),
            ("promoted_experiment_id", "INTEGER"),
            ("promotion_source_experiment_id", "INTEGER"),
            ("promoted_at", "TEXT"),
        ],
    )
    if await _table_exists(conn, "sweep_experiments"):
        # Old sweep rows did not persist their selection window. All members of
        # one legacy sweep shared the same test window, so recover it once.
        await conn.execute(
            """
            UPDATE param_sweeps
            SET selection_start = COALESCE(
                    selection_start,
                    (
                        SELECT e.test_start
                        FROM sweep_experiments se
                        JOIN experiments e ON e.id = se.experiment_id
                        WHERE se.sweep_id = param_sweeps.id
                        ORDER BY e.id
                        LIMIT 1
                    )
                ),
                selection_end = COALESCE(
                    selection_end,
                    (
                        SELECT e.test_end
                        FROM sweep_experiments se
                        JOIN experiments e ON e.id = se.experiment_id
                        WHERE se.sweep_id = param_sweeps.id
                        ORDER BY e.id
                        LIMIT 1
                    )
                )
            WHERE selection_start IS NULL OR selection_end IS NULL
            """
        )
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS factor_catalog_versions (
            factor_id TEXT NOT NULL,
            version TEXT NOT NULL,
            definition_digest TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'published'
                CHECK(status IN ('published', 'deprecated')),
            supersedes_version TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            registered_at TEXT NOT NULL,
            published_at TEXT NOT NULL,
            published_by INTEGER,
            deprecated_at TEXT,
            deprecated_by INTEGER,
            PRIMARY KEY(factor_id, version),
            UNIQUE(factor_id, definition_digest)
        );

        CREATE TABLE IF NOT EXISTS factor_strategy_series (
            strategy_id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            current_version INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS factor_strategy_versions (
            strategy_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            definition_json TEXT NOT NULL,
            definition_digest TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(strategy_id, version),
            UNIQUE(strategy_id, definition_digest),
            FOREIGN KEY(strategy_id)
                REFERENCES factor_strategy_series(strategy_id)
                ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS factor_governance_requests (
            actor_user_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(actor_user_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS factor_governance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_revision INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_factor_governance_events_entity
        ON factor_governance_events(entity_type, entity_id, id);

        CREATE TRIGGER IF NOT EXISTS factor_catalog_identity_immutable
        BEFORE UPDATE OF
            factor_id, version, definition_digest, manifest_json,
            supersedes_version, registered_at
        ON factor_catalog_versions
        BEGIN
            SELECT RAISE(ABORT, 'factor definition is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS factor_strategy_versions_no_update
        BEFORE UPDATE ON factor_strategy_versions
        BEGIN
            SELECT RAISE(ABORT, 'factor strategy evidence is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS factor_strategy_versions_no_delete
        BEFORE DELETE ON factor_strategy_versions
        BEGIN
            SELECT RAISE(ABORT, 'factor strategy evidence is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS factor_governance_events_no_update
        BEFORE UPDATE ON factor_governance_events
        BEGIN
            SELECT RAISE(ABORT, 'factor governance audit is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS factor_governance_events_no_delete
        BEFORE DELETE ON factor_governance_events
        BEGIN
            SELECT RAISE(ABORT, 'factor governance audit is append-only');
        END;

        CREATE TABLE IF NOT EXISTS factor_research_protocol_series (
            protocol_id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            current_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS factor_research_protocol_versions (
            protocol_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft', 'locked')),
            created_at TEXT NOT NULL,
            locked_at TEXT,
            PRIMARY KEY(protocol_id, version),
            UNIQUE(protocol_id, payload_digest),
            FOREIGN KEY(protocol_id)
                REFERENCES factor_research_protocol_series(protocol_id)
                ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_factor_protocol_owner_updated
        ON factor_research_protocol_series(
            owner_user_id, updated_at DESC, protocol_id
        );

        CREATE TRIGGER IF NOT EXISTS factor_protocol_payload_immutable
        BEFORE UPDATE OF
            protocol_id, version, payload_json, payload_digest, created_at
        ON factor_research_protocol_versions
        BEGIN
            SELECT RAISE(ABORT, 'factor research protocol is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS factor_protocol_version_no_delete
        BEFORE DELETE ON factor_research_protocol_versions
        BEGIN
            SELECT RAISE(ABORT, 'factor research protocol cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS factor_protocol_lock_no_reversal
        BEFORE UPDATE OF status, locked_at
        ON factor_research_protocol_versions
        WHEN (
            OLD.status = 'locked'
            AND (
                NEW.status IS NOT OLD.status
                OR NEW.locked_at IS NOT OLD.locked_at
            )
        ) OR (
            NEW.status = 'locked' AND NEW.locked_at IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'factor research protocol lock is immutable');
        END;

        CREATE TABLE IF NOT EXISTS parameter_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            params TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'batch',
            pool_preset TEXT,
            pool_custom_codes TEXT,
            pool_industries TEXT,
            source_experiment_id INTEGER,
            metrics_snapshot TEXT,
            notes TEXT,
            labels TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, strategy_id, name)
        );

        CREATE INDEX IF NOT EXISTS idx_parameter_presets_user_strategy
        ON parameter_presets(user_id, strategy_id, updated_at DESC);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_parameter_presets_one_default
        ON parameter_presets(user_id, strategy_id)
        WHERE is_default = 1;

        CREATE TABLE IF NOT EXISTS remote_training_tasks (
            task_uuid TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            experiment_id INTEGER NOT NULL,
            strategy_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created'
                CHECK(status IN (
                    'created', 'running', 'completed', 'failed', 'cancelled'
                )),
            token_hash TEXT NOT NULL,
            token_expires_at TEXT NOT NULL,
            params TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            train_start TEXT NOT NULL,
            train_end TEXT NOT NULL,
            data_start TEXT NOT NULL,
            data_end TEXT NOT NULL,
            data_version TEXT NOT NULL,
            data_sha256 TEXT NOT NULL,
            data_rows INTEGER NOT NULL,
            data_columns INTEGER NOT NULL,
            data_fields TEXT NOT NULL,
            manifest TEXT NOT NULL,
            snapshot_path TEXT NOT NULL,
            max_upload_bytes INTEGER NOT NULL,
            progress REAL NOT NULL DEFAULT 0,
            progress_message TEXT,
            report_json TEXT,
            artifact_path TEXT,
            artifact_sha256 TEXT,
            artifact_size INTEGER,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            FOREIGN KEY (experiment_id)
                REFERENCES experiments(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_remote_training_user_created
        ON remote_training_tasks(user_id, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_remote_training_experiment_created
        ON remote_training_tasks(experiment_id, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_remote_training_status
        ON remote_training_tasks(status, updated_at);

        CREATE TABLE IF NOT EXISTS research_run_manifests (
            experiment_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            schema_version TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (experiment_id)
                REFERENCES experiments(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_research_manifest_user
        ON research_run_manifests(user_id, experiment_id);

        CREATE TRIGGER IF NOT EXISTS trg_research_manifest_no_update
        BEFORE UPDATE ON research_run_manifests
        BEGIN
            SELECT RAISE(ABORT, 'research run manifest is immutable');
        END;

        CREATE TABLE IF NOT EXISTS research_artifact_manifests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER NOT NULL,
            run_manifest_hash TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            artifact_size INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(
                experiment_id, artifact_kind, artifact_sha256
            ),
            FOREIGN KEY (experiment_id)
                REFERENCES experiments(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_research_artifact_experiment
        ON research_artifact_manifests(experiment_id, id);

        CREATE TRIGGER IF NOT EXISTS trg_research_artifact_no_update
        BEFORE UPDATE ON research_artifact_manifests
        BEGIN
            SELECT RAISE(ABORT, 'research artifact manifest is immutable');
        END;

        CREATE TABLE IF NOT EXISTS research_rerun_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            source_experiment_id INTEGER NOT NULL,
            new_experiment_id INTEGER NOT NULL,
            allow_environment_drift INTEGER NOT NULL DEFAULT 0,
            environment_drift_json TEXT NOT NULL DEFAULT '[]',
            job_uuid TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, idempotency_key),
            FOREIGN KEY (source_experiment_id)
                REFERENCES experiments(id) ON DELETE CASCADE,
            FOREIGN KEY (new_experiment_id)
                REFERENCES experiments(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS research_hypotheses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            falsifiable_statement TEXT NOT NULL,
            preregistered_metrics_json TEXT NOT NULL,
            risk_acceptance_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft', 'submitted', 'withdrawn')),
            version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            submitted_at TEXT,
            withdrawn_at TEXT,
            UNIQUE(user_id, idempotency_key)
        );

        CREATE INDEX IF NOT EXISTS idx_research_hypothesis_owner
        ON research_hypotheses(user_id, created_at DESC);

        CREATE TRIGGER IF NOT EXISTS trg_research_hypothesis_core_immutable
        BEFORE UPDATE ON research_hypotheses
        WHEN OLD.status <> 'draft' AND (
            NEW.title IS NOT OLD.title OR
            NEW.falsifiable_statement IS NOT OLD.falsifiable_statement OR
            NEW.preregistered_metrics_json
                IS NOT OLD.preregistered_metrics_json OR
            NEW.risk_acceptance_json IS NOT OLD.risk_acceptance_json
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'submitted hypothesis core is immutable'
            );
        END;

        CREATE TABLE IF NOT EXISTS research_experiment_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            hypothesis_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            selection_protocol_json TEXT NOT NULL,
            locked_protocol_json TEXT NOT NULL,
            manifest_policy_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft', 'active', 'closed')),
            version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            activated_at TEXT,
            closed_at TEXT,
            UNIQUE(user_id, idempotency_key),
            FOREIGN KEY (hypothesis_id)
                REFERENCES research_hypotheses(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_research_group_owner
        ON research_experiment_groups(user_id, created_at DESC);

        CREATE TRIGGER IF NOT EXISTS trg_research_group_protocol_immutable
        BEFORE UPDATE ON research_experiment_groups
        WHEN OLD.status <> 'draft' AND (
            NEW.strategy_id IS NOT OLD.strategy_id OR
            NEW.selection_protocol_json IS NOT OLD.selection_protocol_json OR
            NEW.locked_protocol_json IS NOT OLD.locked_protocol_json OR
            NEW.manifest_policy_json IS NOT OLD.manifest_policy_json
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'active research group protocol is immutable'
            );
        END;

        CREATE TABLE IF NOT EXISTS research_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            experiment_id INTEGER NOT NULL,
            role TEXT NOT NULL
                CHECK(role IN ('selection', 'locked_test')),
            status TEXT NOT NULL DEFAULT 'linked'
                CHECK(status='linked'),
            version INTEGER NOT NULL DEFAULT 1,
            source_trial_id INTEGER,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, idempotency_key),
            UNIQUE(group_id, experiment_id),
            FOREIGN KEY (group_id)
                REFERENCES research_experiment_groups(id) ON DELETE RESTRICT,
            FOREIGN KEY (experiment_id)
                REFERENCES experiments(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_trial_id)
                REFERENCES research_trials(id) ON DELETE RESTRICT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_research_group_locked_trial
        ON research_trials(group_id) WHERE role='locked_test';

        CREATE TRIGGER IF NOT EXISTS trg_research_trial_no_update
        BEFORE UPDATE ON research_trials
        BEGIN
            SELECT RAISE(ABORT, 'research trial links are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_research_trial_no_delete
        BEFORE DELETE ON research_trials
        BEGIN
            SELECT RAISE(ABORT, 'research trial links are immutable');
        END;

        CREATE TABLE IF NOT EXISTS research_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            report_type TEXT NOT NULL
                CHECK(report_type IN ('selection', 'final')),
            snapshot_json TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'generated'
                CHECK(status='generated'),
            version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, idempotency_key),
            UNIQUE(group_id, report_type),
            FOREIGN KEY (group_id)
                REFERENCES research_experiment_groups(id) ON DELETE RESTRICT
        );

        CREATE TRIGGER IF NOT EXISTS trg_research_report_no_update
        BEFORE UPDATE ON research_reports
        BEGIN
            SELECT RAISE(ABORT, 'research reports are immutable snapshots');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_research_report_no_delete
        BEFORE DELETE ON research_reports
        BEGIN
            SELECT RAISE(ABORT, 'research reports are immutable snapshots');
        END;

        CREATE TABLE IF NOT EXISTS research_promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            report_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN (
                    'draft', 'reviewed', 'approved', 'rejected', 'revoked'
                )),
            rationale TEXT NOT NULL,
            blockers_json TEXT NOT NULL DEFAULT '[]',
            version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reviewed_at TEXT,
            decided_at TEXT,
            revoked_at TEXT,
            reviewed_by INTEGER,
            decided_by INTEGER,
            revoked_by INTEGER,
            UNIQUE(user_id, idempotency_key),
            UNIQUE(group_id),
            FOREIGN KEY (group_id)
                REFERENCES research_experiment_groups(id) ON DELETE RESTRICT,
            FOREIGN KEY (report_id)
                REFERENCES research_reports(id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_research_promotion_status
        ON research_promotions(status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS research_workflow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            actor_user_id INTEGER NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            entity_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_research_event_entity
        ON research_workflow_events(
            owner_user_id, entity_type, entity_id, id
        );

        CREATE TRIGGER IF NOT EXISTS trg_research_event_no_update
        BEFORE UPDATE ON research_workflow_events
        BEGIN
            SELECT RAISE(ABORT, 'research audit events are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_research_event_no_delete
        BEFORE DELETE ON research_workflow_events
        BEGIN
            SELECT RAISE(ABORT, 'research audit events are append-only');
        END;

        CREATE TABLE IF NOT EXISTS ai_cache (
            cache_key TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_ai_cache_expires
        ON ai_cache(expires_at);

        CREATE TABLE IF NOT EXISTS ai_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL,
            user_id INTEGER,
            cache_key TEXT NOT NULL,
            model TEXT,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms REAL NOT NULL DEFAULT 0,
            cache_hit INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 1,
            error_type TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_ai_usage_user_created
        ON ai_usage(user_id, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_ai_usage_endpoint_created
        ON ai_usage(endpoint, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_param_sweeps_promotion
        ON param_sweeps(promoted_experiment_id);
        """
    )
    await _add_columns(
        conn,
        "ai_usage",
        [("latency_ms", "REAL NOT NULL DEFAULT 0")],
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    await conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version) VALUES ('experiment-005-ai')"
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('experiment-006-remote-training')
        """
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('experiment-007-locked-test-sweeps')
        """
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('experiment-007-research-manifest')
        """
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('experiment-008-research-workflow')
        """
    )
    from backend.data.point_in_time_master import PIT_MASTER_SCHEMA_SQL

    await conn.executescript(PIT_MASTER_SCHEMA_SQL)
    await _add_columns(
        conn,
        "pit_master_batches",
        [
            ("available_at", "TEXT"),
            ("ingested_at", "TEXT"),
            ("revision", "INTEGER"),
            ("supersedes_batch_id", "TEXT"),
        ],
    )
    await _add_columns(
        conn,
        "pit_master_intervals",
        [
            ("effective_at", "TEXT"),
            ("available_at", "TEXT"),
            ("ingested_at", "TEXT"),
            ("revision", "INTEGER"),
        ],
    )
    await conn.executescript(
        """
        DROP INDEX IF EXISTS uq_pit_master_interval_identity;
        CREATE UNIQUE INDEX uq_pit_master_interval_identity
        ON pit_master_intervals(
            batch_id, domain, scope_id, security_code,
            effective_from, effective_to
        );
        DROP TRIGGER IF EXISTS pit_master_intervals_no_overlap;
        CREATE TRIGGER pit_master_intervals_no_overlap
        BEFORE INSERT ON pit_master_intervals
        WHEN EXISTS (
            SELECT 1 FROM pit_master_intervals existing
            WHERE existing.domain = NEW.domain
              AND existing.scope_id = NEW.scope_id
              AND existing.security_code = NEW.security_code
              AND existing.effective_from <= NEW.effective_to
              AND NEW.effective_from <= existing.effective_to
              AND (
                  existing.batch_id = NEW.batch_id
                  OR NEW.revision IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM pit_master_batches incoming
                      WHERE incoming.batch_id = NEW.batch_id
                        AND incoming.supersedes_batch_id = existing.batch_id
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'point-in-time interval overlap');
        END;
        """
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('experiment-009-point-in-time-master')
        """
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('experiment-010-governed-pit-activation')
        """
    )
    from backend.data.price_ledger import PRICE_LEDGER_SCHEMA_SQL

    await conn.executescript(PRICE_LEDGER_SCHEMA_SQL)
    await _add_columns(
        conn,
        "price_ledger_batches",
        [
            ("available_at", "TEXT"),
            ("ingested_at", "TEXT"),
            ("revision", "INTEGER"),
            ("supersedes_batch_id", "TEXT"),
        ],
    )
    await _add_columns(
        conn,
        "price_ledger_prices",
        [
            ("effective_at", "TEXT"),
            ("available_at", "TEXT"),
            ("ingested_at", "TEXT"),
            ("revision", "INTEGER"),
        ],
    )
    await _add_columns(
        conn,
        "price_ledger_runtime_bindings",
        [
            ("as_known_at", "TEXT"),
            ("bitemporal_evidence_sha256", "TEXT"),
            ("price_role_usage_json", "TEXT"),
        ],
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('experiment-010-dual-price-ledger')
        """
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version)
        VALUES ('experiment-011-bitemporal-pit-ledgers')
        """
    )


async def migrate_trading(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA busy_timeout=5000")
    await _add_columns(
        conn,
        "deployments",
        [
            ("pool_preset", "TEXT"),
            ("pool_custom_codes", "TEXT"),
            ("pool_industries", "TEXT"),
            ("data_version", "TEXT"),
            ("current_model_sha256", "TEXT"),
            ("current_model_size", "INTEGER"),
            ("research_promotion_id", "INTEGER"),
            ("promotion_version", "INTEGER"),
            ("promotion_report_id", "INTEGER"),
            ("promotion_report_hash", "TEXT"),
            ("promotion_manifest_hash", "TEXT"),
            ("promotion_model_artifact_id", "INTEGER"),
            ("promotion_model_sha256", "TEXT"),
            ("promotion_evidence_hash", "TEXT"),
            ("promotion_binding_hash", "TEXT"),
            ("research_risk_snapshot", "TEXT"),
            ("research_risk_snapshot_hash", "TEXT"),
            ("research_generation_id", "TEXT"),
            ("research_source_id", "TEXT"),
            ("research_window_start", "TEXT"),
            ("research_window_end", "TEXT"),
        ],
    )
    await conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_deployment_research_promotion
            ON deployments(research_promotion_id);

        CREATE TRIGGER IF NOT EXISTS
            trg_deployment_promotion_binding_immutable
        BEFORE UPDATE OF
            research_promotion_id,
            promotion_version,
            promotion_report_id,
            promotion_report_hash,
            promotion_manifest_hash,
            promotion_model_artifact_id,
            promotion_model_sha256,
            promotion_evidence_hash,
            promotion_binding_hash
        ON deployments
        WHEN OLD.promotion_binding_hash IS NOT NULL
         AND (
            OLD.research_promotion_id IS NOT NEW.research_promotion_id
            OR OLD.promotion_version IS NOT NEW.promotion_version
            OR OLD.promotion_report_id IS NOT NEW.promotion_report_id
            OR OLD.promotion_report_hash IS NOT NEW.promotion_report_hash
            OR OLD.promotion_manifest_hash IS NOT NEW.promotion_manifest_hash
            OR OLD.promotion_model_artifact_id
                IS NOT NEW.promotion_model_artifact_id
            OR OLD.promotion_model_sha256 IS NOT NEW.promotion_model_sha256
            OR OLD.promotion_evidence_hash IS NOT NEW.promotion_evidence_hash
            OR OLD.promotion_binding_hash IS NOT NEW.promotion_binding_hash
         )
        BEGIN
            SELECT RAISE(
                ABORT,
                'deployment promotion binding is immutable'
            );
        END;

        CREATE TRIGGER IF NOT EXISTS
            trg_deployment_research_risk_binding_immutable
        BEFORE UPDATE OF
            research_risk_snapshot,
            research_risk_snapshot_hash,
            research_generation_id,
            research_source_id,
            research_window_start,
            research_window_end
        ON deployments
        WHEN OLD.research_risk_snapshot_hash IS NOT NULL
         AND (
            OLD.research_risk_snapshot IS NOT NEW.research_risk_snapshot
            OR OLD.research_risk_snapshot_hash
                IS NOT NEW.research_risk_snapshot_hash
            OR OLD.research_generation_id IS NOT NEW.research_generation_id
            OR OLD.research_source_id IS NOT NEW.research_source_id
            OR OLD.research_window_start IS NOT NEW.research_window_start
            OR OLD.research_window_end IS NOT NEW.research_window_end
         )
        BEGIN
            SELECT RAISE(
                ABORT,
                'deployment research risk binding is immutable'
            );
        END;
        """
    )
    if await _table_exists(conn, "model_version_history"):
        await _add_columns(
            conn,
            "model_version_history",
            [
                ("validation_window_start", "TEXT"),
                ("validation_window_end", "TEXT"),
                ("validation_metrics", "TEXT"),
                ("model_sha256", "TEXT"),
                ("model_size", "INTEGER"),
                ("strategy_id", "TEXT"),
                ("params_hash", "TEXT"),
                ("status", "TEXT NOT NULL DEFAULT 'promoted'"),
                ("error", "TEXT"),
                ("retrain_manifest_json", "TEXT"),
                ("retrain_manifest_hash", "TEXT"),
            ],
        )
        await conn.execute(
            """
            UPDATE model_version_history
            SET status='unverified_legacy'
            WHERE model_sha256 IS NULL
               OR model_size IS NULL
               OR retrain_manifest_hash IS NULL
            """
        )
        await conn.execute(
            """
            UPDATE model_version_history
            SET is_latest=0
            WHERE is_latest=1
              AND id NOT IN (
                  SELECT MAX(id)
                  FROM model_version_history
                  WHERE is_latest=1
                  GROUP BY deployment_id
              )
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_model_version_history_deployment_version
            ON model_version_history(deployment_id, model_version)
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_model_version_history_latest
            ON model_version_history(deployment_id)
            WHERE is_latest=1
            """
        )
    await _add_columns(
        conn,
        "portfolios",
        [
            ("cash_balance", "REAL"),
            ("current_revision", "INTEGER DEFAULT 0"),
        ],
    )
    await _add_columns(
        conn,
        "daily_signals",
        [
            ("simulation_run_id", "TEXT"),
            ("target_weight_bps", "INTEGER DEFAULT 0"),
        ],
    )
    await _add_columns(
        conn,
        "position_snapshots",
        [
            ("simulation_run_id", "TEXT"),
            ("weight_in_portfolio", "REAL DEFAULT 0"),
        ],
    )
    await _add_columns(
        conn,
        "orders",
        [
            ("simulation_run_id", "TEXT"),
            ("filled_at", "TEXT"),
            ("order_intent_id", "TEXT"),
        ],
    )
    await _add_columns(
        conn,
        "nav_history",
        [
            ("simulation_run_id", "TEXT"),
            ("cash_balance", "REAL"),
            ("total_equity", "REAL"),
        ],
    )
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS simulation_runs (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            portfolio_id INTEGER,
            trade_date TEXT NOT NULL,
            idempotency_key TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            summary TEXT,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS portfolio_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            allocations TEXT NOT NULL,
            validation_result TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            effective_date TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            published_at TEXT,
            UNIQUE(portfolio_id, revision),
            FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS portfolio_allocations (
            portfolio_id INTEGER NOT NULL,
            deployment_id INTEGER NOT NULL,
            target_weight_bps INTEGER NOT NULL,
            min_weight_bps INTEGER NOT NULL DEFAULT 0,
            max_weight_bps INTEGER NOT NULL DEFAULT 10000,
            locked INTEGER NOT NULL DEFAULT 0,
            risk_budget_bps INTEGER,
            revision INTEGER NOT NULL,
            PRIMARY KEY (portfolio_id, deployment_id),
            FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE,
            FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS strategy_nav_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER NOT NULL,
            deployment_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            opening_equity REAL NOT NULL DEFAULT 0,
            net_flow REAL NOT NULL DEFAULT 0,
            cash_balance REAL NOT NULL DEFAULT 0,
            market_value REAL NOT NULL DEFAULT 0,
            total_equity REAL NOT NULL DEFAULT 0,
            daily_pnl REAL NOT NULL DEFAULT 0,
            daily_return REAL,
            cumulative_return REAL,
            realized_pnl REAL NOT NULL DEFAULT 0,
            unrealized_pnl REAL NOT NULL DEFAULT 0,
            transaction_cost REAL NOT NULL DEFAULT 0,
            turnover REAL NOT NULL DEFAULT 0,
            turnover_rate REAL,
            contribution_pnl REAL NOT NULL DEFAULT 0,
            contribution_return REAL,
            simulation_run_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(portfolio_id, deployment_id, date),
            FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE,
            FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS model_retrain_attempts (
            attempt_id TEXT PRIMARY KEY,
            deployment_id INTEGER NOT NULL,
            expected_model_version INTEGER NOT NULL,
            candidate_model_version INTEGER NOT NULL,
            status TEXT NOT NULL,
            train_window_start TEXT,
            train_window_end TEXT,
            validation_window_start TEXT,
            validation_window_end TEXT,
            validation_metrics TEXT,
            model_sha256 TEXT,
            model_size INTEGER,
            retrain_manifest_json TEXT,
            retrain_manifest_hash TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (deployment_id)
                REFERENCES deployments(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_model_retrain_attempt_deployment
            ON model_retrain_attempts(deployment_id, created_at DESC);

        CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_deployment_date_code
            ON daily_signals(deployment_id, date, code);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_position_portfolio_deployment_date_code
            ON position_snapshots(portfolio_id, deployment_id, date, code);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_nav_portfolio_date
            ON nav_history(portfolio_id, date);
        CREATE INDEX IF NOT EXISTS idx_simulation_runs_user_date
            ON simulation_runs(user_id, trade_date);
        CREATE INDEX IF NOT EXISTS idx_portfolio_versions_portfolio
            ON portfolio_versions(portfolio_id, revision);
        CREATE INDEX IF NOT EXISTS idx_strategy_nav_portfolio_date
            ON strategy_nav_history(portfolio_id, date);
        CREATE INDEX IF NOT EXISTS idx_strategy_nav_deployment_date
            ON strategy_nav_history(deployment_id, date);

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO schema_migrations(version) VALUES ('trading-003');
        INSERT OR IGNORE INTO schema_migrations(version) VALUES ('trading-004-strategy-nav');
        INSERT OR IGNORE INTO schema_migrations(version)
            VALUES ('trading-005-model-artifact-integrity');
        INSERT OR IGNORE INTO schema_migrations(version)
            VALUES ('trading-006-promotion-binding');
        """
    )
    await _add_columns(
        conn,
        "simulation_runs",
        [
            ("portfolio_id", "INTEGER"),
            ("claim_token", "TEXT"),
            ("claim_expires_at", "TEXT"),
            ("heartbeat_at", "TEXT"),
        ],
    )
    await _add_columns(
        conn,
        "model_retrain_attempts",
        [
            ("retrain_manifest_json", "TEXT"),
            ("retrain_manifest_hash", "TEXT"),
        ],
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_simulation_runs_portfolio_date
            ON simulation_runs(portfolio_id, trade_date)
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_intent
        ON orders(order_intent_id)
        WHERE order_intent_id IS NOT NULL
        """
    )
