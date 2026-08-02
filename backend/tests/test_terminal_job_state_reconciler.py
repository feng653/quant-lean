"""Regression tests for terminal-job/experiment state reconciliation."""

from __future__ import annotations

import asyncio

from backend.jobs.broker import JobBroker
from backend.jobs.state_reconciler import RECONCILIATION_EVENT_STAGE


def _create_research_tables(broker: JobBroker) -> None:
    with broker._get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                progress_pct REAL DEFAULT 0,
                progress_message TEXT,
                error_log TEXT,
                completed_at TEXT
            );
            CREATE TABLE param_sweeps (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                total_experiments INTEGER NOT NULL,
                completed_experiments INTEGER DEFAULT 0,
                status TEXT
            );
            CREATE TABLE sweep_experiments (
                sweep_id INTEGER NOT NULL,
                experiment_id INTEGER NOT NULL
            );
            """
        )
        conn.commit()


async def _submit_backtest(
    broker: JobBroker,
    *,
    experiment_id: int,
    user_id: int,
) -> str:
    return await broker.submit_job(
        "backtest",
        {"experiment_id": experiment_id},
        user_id=user_id,
        resource_type="experiment",
        resource_id=experiment_id,
    )


def test_startup_repairs_latest_cancelled_job_and_sweep_once(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        _create_research_tables(broker)
        with broker._get_conn() as conn:
            conn.executescript(
                """
                INSERT INTO experiments
                    (id, user_id, status, progress_pct, progress_message)
                VALUES (1, 7, 'running', 5, '加载数据');
                INSERT INTO param_sweeps
                    (id, user_id, total_experiments,
                     completed_experiments, status)
                VALUES (3, 7, 1, 0, 'running');
                INSERT INTO sweep_experiments (sweep_id, experiment_id)
                VALUES (3, 1);
                """
            )
            conn.commit()
        job_uuid = await _submit_backtest(
            broker,
            experiment_id=1,
            user_id=7,
        )
        with broker._get_conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status='cancelled', progress=1, completed_at=datetime('now')
                WHERE job_uuid=?
                """,
                (job_uuid,),
            )
            conn.commit()

        assert await broker.recover_pending_jobs() == 0
        with broker._get_conn() as conn:
            experiment = conn.execute(
                """
                SELECT status, progress_pct, progress_message, error_log,
                       completed_at
                FROM experiments WHERE id=1
                """
            ).fetchone()
            sweep = conn.execute(
                """
                SELECT status, completed_experiments
                FROM param_sweeps WHERE id=3
                """
            ).fetchone()
            events = conn.execute(
                """
                SELECT COUNT(*) FROM job_events
                WHERE job_uuid=? AND stage=?
                """,
                (job_uuid, RECONCILIATION_EVENT_STAGE),
            ).fetchone()[0]
        assert tuple(experiment)[:4] == (
            "cancelled",
            100,
            "后台任务已取消，状态已自动协调",
            None,
        )
        assert experiment["completed_at"] is not None
        assert tuple(sweep) == ("completed", 1)
        assert events == 1

        assert await broker.reconcile_terminal_backtest_jobs() == 0
        with broker._get_conn() as conn:
            repeated_events = conn.execute(
                """
                SELECT COUNT(*) FROM job_events
                WHERE job_uuid=? AND stage=?
                """,
                (job_uuid, RECONCILIATION_EVENT_STAGE),
            ).fetchone()[0]
        assert repeated_events == 1

    asyncio.run(scenario())


def test_newer_active_job_blocks_historical_terminal_job(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        _create_research_tables(broker)
        with broker._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO experiments (id, user_id, status, progress_pct)
                VALUES (9, 4, 'running', 15)
                """
            )
            conn.commit()
        historical = await _submit_backtest(
            broker,
            experiment_id=9,
            user_id=4,
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed' WHERE job_uuid=?",
                (historical,),
            )
            conn.commit()
        newer = await _submit_backtest(
            broker,
            experiment_id=9,
            user_id=4,
        )

        assert await broker.reconcile_terminal_backtest_jobs() == 0
        with broker._get_conn() as conn:
            experiment = conn.execute(
                "SELECT status, progress_pct FROM experiments WHERE id=9"
            ).fetchone()
            newer_status = conn.execute(
                "SELECT status FROM jobs WHERE job_uuid=?",
                (newer,),
            ).fetchone()[0]
        assert tuple(experiment) == ("running", 15)
        assert newer_status == "pending"

    asyncio.run(scenario())


def test_reconciliation_is_user_scoped_and_fail_closed(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        _create_research_tables(broker)
        with broker._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO experiments (id, user_id, status)
                VALUES (5, 1, 'running')
                """
            )
            conn.commit()
        foreign_job = await _submit_backtest(
            broker,
            experiment_id=5,
            user_id=2,
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='cancelled' WHERE job_uuid=?",
                (foreign_job,),
            )
            conn.commit()

        assert await broker.reconcile_terminal_backtest_jobs() == 0
        with broker._get_conn() as conn:
            status = conn.execute(
                "SELECT status FROM experiments WHERE id=5"
            ).fetchone()[0]
        assert status == "running"

    asyncio.run(scenario())


def test_completed_job_evidence_is_never_downgraded(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        _create_research_tables(broker)
        with broker._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO experiments (id, user_id, status, progress_pct)
                VALUES (12, 3, 'running', 80)
                """
            )
            conn.commit()
        completed = await _submit_backtest(
            broker,
            experiment_id=12,
            user_id=3,
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='completed' WHERE job_uuid=?",
                (completed,),
            )
            conn.commit()
        failed_retry = await _submit_backtest(
            broker,
            experiment_id=12,
            user_id=3,
        )
        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed' WHERE job_uuid=?",
                (failed_retry,),
            )
            conn.commit()

        assert await broker.reconcile_terminal_backtest_jobs() == 0
        with broker._get_conn() as conn:
            experiment = conn.execute(
                "SELECT status, progress_pct FROM experiments WHERE id=12"
            ).fetchone()
        assert tuple(experiment) == ("running", 80)

        with broker._get_conn() as conn:
            conn.execute(
                "UPDATE experiments SET status='completed' WHERE id=12"
            )
            conn.commit()
        pending = await _submit_backtest(
            broker,
            experiment_id=12,
            user_id=3,
        )
        assert await broker.request_cancel(pending) == "cancelled"
        with broker._get_conn() as conn:
            protected_status = conn.execute(
                "SELECT status FROM experiments WHERE id=12"
            ).fetchone()[0]
            misleading_audits = conn.execute(
                """
                SELECT COUNT(*) FROM job_events
                WHERE job_uuid=? AND stage=?
                """,
                (pending, RECONCILIATION_EVENT_STAGE),
            ).fetchone()[0]
        assert protected_status == "completed"
        assert misleading_audits == 0

    asyncio.run(scenario())


def test_failed_job_repair_redacts_error_and_respects_pass_limit(tmp_path) -> None:
    async def scenario() -> None:
        broker = JobBroker(str(tmp_path / "jobs.db"))
        _create_research_tables(broker)
        with broker._get_conn() as conn:
            conn.executemany(
                """
                INSERT INTO experiments
                    (id, user_id, status, progress_pct, error_log)
                VALUES (?, 6, 'running', 5, 'old error')
                """,
                [(21,), (22,)],
            )
            conn.commit()
        first = await _submit_backtest(
            broker,
            experiment_id=21,
            user_id=6,
        )
        second = await _submit_backtest(
            broker,
            experiment_id=22,
            user_id=6,
        )
        secret_error = "token=raw-secret /Users/private/research.parquet"
        with broker._get_conn() as conn:
            conn.execute(
                """
                UPDATE jobs SET status='failed', error=?
                WHERE job_uuid IN (?, ?)
                """,
                (secret_error, first, second),
            )
            conn.commit()

        assert await broker.reconcile_terminal_backtest_jobs(limit=1) == 1
        with broker._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT status, error_log FROM experiments
                WHERE id IN (21, 22) ORDER BY id
                """
            ).fetchall()
            audit_messages = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT message FROM job_events
                    WHERE stage=? ORDER BY id
                    """,
                    (RECONCILIATION_EVENT_STAGE,),
                ).fetchall()
            ]
        assert [row["status"] for row in rows].count("failed") == 1
        repaired_error = next(
            row["error_log"] for row in rows if row["status"] == "failed"
        )
        assert repaired_error is not None
        assert "raw-secret" not in repaired_error
        assert "/Users/" not in repaired_error
        assert all("raw-secret" not in message for message in audit_messages)

        assert await broker.reconcile_terminal_backtest_jobs(limit=1) == 1
        assert await broker.reconcile_terminal_backtest_jobs(limit=1) == 0

    asyncio.run(scenario())
