from __future__ import annotations

import asyncio

import aiosqlite

from backend.db import migrate


def test_add_columns_tolerates_concurrent_duplicate_column(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "migration.db"
        async with aiosqlite.connect(db_path) as setup:
            await setup.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
            await setup.commit()

        original_columns = migrate._columns
        arrived = 0
        ready = asyncio.Event()

        async def synchronized_columns(conn, table):
            nonlocal arrived
            columns = await original_columns(conn, table)
            arrived += 1
            if arrived == 2:
                ready.set()
            await ready.wait()
            return columns

        migrate._columns = synchronized_columns
        try:
            async with (
                aiosqlite.connect(
                    db_path,
                    isolation_level=None,
                ) as first,
                aiosqlite.connect(
                    db_path,
                    isolation_level=None,
                ) as second,
            ):
                await asyncio.gather(
                    migrate._add_columns(
                        first,
                        "sample",
                        [("new_value", "TEXT")],
                    ),
                    migrate._add_columns(
                        second,
                        "sample",
                        [("new_value", "TEXT")],
                    ),
                )
                columns = await original_columns(first, "sample")
        finally:
            migrate._columns = original_columns

        assert "new_value" in columns

    asyncio.run(scenario())


def test_research_manifest_migration_is_concurrent_and_idempotent(
    tmp_path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "experiment.db"
        async with aiosqlite.connect(db_path) as setup:
            await setup.executescript(
                """
                CREATE TABLE experiments (id INTEGER PRIMARY KEY);
                CREATE TABLE trade_log (id INTEGER PRIMARY KEY);
                CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY);
                CREATE TABLE model_artifacts (id INTEGER PRIMARY KEY);
                """
            )
            await setup.commit()

        async with (
            aiosqlite.connect(db_path, isolation_level=None) as first,
            aiosqlite.connect(db_path, isolation_level=None) as second,
        ):
            await first.execute("PRAGMA busy_timeout=5000")
            await second.execute("PRAGMA busy_timeout=5000")
            await asyncio.gather(
                migrate.migrate_experiment(first),
                migrate.migrate_experiment(second),
            )
            await migrate.migrate_experiment(first)
            tables = {
                row[0]
                for row in await (
                    await first.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table'
                        """
                    )
                ).fetchall()
            }
            migration_count = (
                await (
                    await first.execute(
                        """
                        SELECT COUNT(*) FROM schema_migrations
                        WHERE version='experiment-007-research-manifest'
                        """
                    )
                ).fetchone()
            )[0]
            artifact_columns = await migrate._columns(
                first,
                "model_artifacts",
            )

        assert {
            "research_run_manifests",
            "research_artifact_manifests",
            "research_rerun_requests",
        } <= tables
        assert migration_count == 1
        assert {
            "artifact_sha256",
            "artifact_size",
            "run_manifest_hash",
        } <= artifact_columns

    asyncio.run(scenario())
