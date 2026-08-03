"""Regression tests for parameter-sweep terminal accounting."""

from __future__ import annotations

import asyncio

import aiosqlite

from backend.execution.backtest_runner import _refresh_related_sweep_status


def test_failed_and_cancelled_members_complete_the_sweep(tmp_path) -> None:
    async def exercise() -> tuple[str, int]:
        database = tmp_path / "sweep.db"
        async with aiosqlite.connect(database) as connection:
            await connection.executescript(
                """
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE param_sweeps (
                    id INTEGER PRIMARY KEY,
                    total_experiments INTEGER NOT NULL,
                    completed_experiments INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL
                );
                CREATE TABLE sweep_experiments (
                    sweep_id INTEGER NOT NULL,
                    experiment_id INTEGER NOT NULL
                );
                INSERT INTO param_sweeps
                    (id, total_experiments, completed_experiments, status)
                VALUES (7, 3, 0, 'running');
                INSERT INTO experiments (id, status)
                VALUES (1, 'completed'), (2, 'failed'), (3, 'cancelled');
                INSERT INTO sweep_experiments (sweep_id, experiment_id)
                VALUES (7, 1), (7, 2), (7, 3);
                """
            )
            await _refresh_related_sweep_status(connection, 2)
            await connection.commit()
            cursor = await connection.execute(
                """
                SELECT status, completed_experiments
                FROM param_sweeps
                WHERE id=7
                """
            )
            row = await cursor.fetchone()
            assert row is not None
            return str(row[0]), int(row[1])

    assert asyncio.run(exercise()) == ("completed", 3)


def test_nonterminal_member_keeps_the_sweep_running(tmp_path) -> None:
    async def exercise() -> tuple[str, int]:
        database = tmp_path / "sweep.db"
        async with aiosqlite.connect(database) as connection:
            await connection.executescript(
                """
                CREATE TABLE experiments (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE param_sweeps (
                    id INTEGER PRIMARY KEY,
                    total_experiments INTEGER NOT NULL,
                    completed_experiments INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL
                );
                CREATE TABLE sweep_experiments (
                    sweep_id INTEGER NOT NULL,
                    experiment_id INTEGER NOT NULL
                );
                INSERT INTO param_sweeps
                    (id, total_experiments, completed_experiments, status)
                VALUES (8, 2, 0, 'pending');
                INSERT INTO experiments (id, status)
                VALUES (11, 'completed'), (12, 'running');
                INSERT INTO sweep_experiments (sweep_id, experiment_id)
                VALUES (8, 11), (8, 12);
                """
            )
            await _refresh_related_sweep_status(connection, 11)
            await connection.commit()
            cursor = await connection.execute(
                """
                SELECT status, completed_experiments
                FROM param_sweeps
                WHERE id=8
                """
            )
            row = await cursor.fetchone()
            assert row is not None
            return str(row[0]), int(row[1])

    assert asyncio.run(exercise()) == ("running", 1)
