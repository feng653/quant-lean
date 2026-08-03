"""Database bootstrap：建表 + 迁移（原 main._init_databases）。"""

from __future__ import annotations

import logging

import aiosqlite

from backend.auth.sessions import ensure_auth_session_schema
from backend.config import settings
from backend.db.migrate import migrate_experiment, migrate_trading
from backend.db.schema import (
    EXPERIMENT_SCHEMA,
    TRADING_SIM_SCHEMA,
    USERS_SCHEMA,
)

logger = logging.getLogger("quant_platform")


async def init_databases() -> None:
    """初始化所有数据库，执行迁移 SQL。"""
    db_files = [
        (settings.abs_path(settings.USERS_DB), USERS_SCHEMA),
        (settings.abs_path(settings.EXPERIMENT_DB), EXPERIMENT_SCHEMA),
        (settings.abs_path(settings.TRADING_SIM_DB), TRADING_SIM_SCHEMA),
    ]

    for db_path, schema_sql in db_files:
        db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.executescript(schema_sql)
            if db_path == settings.abs_path(settings.USERS_DB):
                await ensure_auth_session_schema(conn)
            elif db_path == settings.abs_path(settings.EXPERIMENT_DB):
                await migrate_experiment(conn)
            elif db_path == settings.abs_path(settings.TRADING_SIM_DB):
                await migrate_trading(conn)
            await conn.commit()

        logger.info("Database initialized: %s", db_path)
