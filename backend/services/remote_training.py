"""Durable, token-scoped remote model training tasks.

Remote tasks deliberately do not use the in-process job broker.  The server
freezes an immutable Parquet input and only accepts opaque model bytes back;
it never imports or deserializes a worker-provided artifact.
"""

from __future__ import annotations
from backend.core.timeutils import utc_now
from backend.core.hashing import file_sha256

import hashlib
import hmac
import inspect
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import aiosqlite
import pandas as pd

from backend.config import settings
from backend.data.universe import POOL_NAME_ALIASES
from backend.data.versioning import compute_data_version
from backend.strategies.base import RetrainFrequency, TrainableStrategy

TOKEN_TTL_HOURS = 24
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_REPORT_JSON_BYTES = 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
DEFAULT_DATA_START = "2015-01-01"
MANIFEST_SCHEMA_VERSION = "remote-training-bundle/v1"
RESULT_SCHEMA_VERSION = "remote-training-result/v1"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class RemoteTrainingError(RuntimeError):
    """An expected API-safe remote-training error."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AsyncUpload(Protocol):
    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    sha256: str
    data_version: str
    rows: int
    columns: int
    fields: list[str]
    data_start: str
    data_end: str
    train_start: str
    train_end: str
    lookback_rows: int
    label_tail_rows: int


SnapshotBuilder = Callable[
    [dict[str, Any], TrainableStrategy, dict[str, Any], str, str, Path],
    Awaitable[SnapshotInfo],
]


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_json_object(value: Any, *, field: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RemoteTrainingError(422, f"{field} 不是有效 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise RemoteTrainingError(422, f"{field} 必须是 JSON 对象")
    return parsed


def _parse_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
            items = decoded if isinstance(decoded, list) else value.split(",")
        except json.JSONDecodeError:
            items = value.split(",")
    else:
        return []
    return sorted({str(item).strip() for item in items if str(item).strip()})


def _params_hash(params: dict[str, Any]) -> str:
    try:
        payload = json.dumps(
            params,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RemoteTrainingError(
            422,
            "params 必须是仅含有限数值的规范 JSON 对象",
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _revoked_token_hash() -> str:
    """Return an unguessable tombstone that cannot match the issued token."""
    return hashlib.sha256(
        f"revoked:{secrets.token_urlsafe(32)}".encode("utf-8")
    ).hexdigest()


def _strategy_source_sha256(strategy: TrainableStrategy) -> str:
    try:
        source_path = Path(inspect.getfile(type(strategy))).resolve()
        return file_sha256(source_path)
    except (OSError, TypeError):
        source = inspect.getsource(type(strategy)).encode("utf-8")
        return hashlib.sha256(source).hexdigest()


def _safe_task_dir(root: Path, task_uuid: str) -> Path:
    if not task_uuid or any(char not in "0123456789abcdef" for char in task_uuid):
        raise RemoteTrainingError(400, "任务 ID 格式无效")
    task_dir = (root / task_uuid).resolve()
    root_resolved = root.resolve()
    if task_dir.parent != root_resolved:
        raise RemoteTrainingError(400, "任务存储路径无效")
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def _filter_codes(pivot: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pivot
    allowed = set(codes)
    if isinstance(pivot.columns, pd.MultiIndex):
        columns = [column for column in pivot.columns if str(column[0]) in allowed]
    else:
        columns = [column for column in pivot.columns if str(column) in allowed]
    return pivot.loc[:, columns]


async def _load_experiment_pivot(experiment: dict[str, Any]) -> pd.DataFrame:
    pool_id = POOL_NAME_ALIASES.get(
        str(experiment.get("pool_preset") or "csi300"),
        str(experiment.get("pool_preset") or "csi300"),
    )
    codes = _parse_list(experiment.get("pool_custom_codes"))
    industries = _parse_list(experiment.get("pool_industries"))
    try:
        from backend.data.pit_runtime import require_pit_runtime_input

        pit_input = await require_pit_runtime_input(
            pool_id=pool_id,
            required_start=DEFAULT_DATA_START,
            required_end=str(experiment["test_end"]),
            purpose="tuning",
            requested_codes=codes,
            require_benchmark=False,
        )
        pivot = pit_input.market.frame
    except Exception as exc:
        from backend.data.pit_runtime import PitRuntimeDataError

        if isinstance(exc, PitRuntimeDataError):
            raise RemoteTrainingError(409, exc.message) from exc
        raise

    if pivot is None or pivot.empty:
        raise RemoteTrainingError(422, f"股票池 {pool_id} 没有可用行情")
    if not isinstance(pivot.index, pd.DatetimeIndex):
        pivot.index = pd.to_datetime(pivot.index)
    pivot = pivot.sort_index()

    if industries:
        raise RemoteTrainingError(
            409,
            (
                "远程训练的行业筛选尚未绑定不可变 PIT 行业时间线，"
                "禁止退回当前行业快照"
            ),
        )
    pivot = _filter_codes(pivot, codes)
    if pivot.empty or len(pivot.columns) == 0:
        raise RemoteTrainingError(422, "股票池或行业筛选后没有可训练标的")
    return pivot


def _persist_snapshot(
    pivot: pd.DataFrame,
    *,
    strategy: TrainableStrategy,
    params: dict[str, Any],
    train_start: str,
    train_end: str,
    task_dir: Path,
) -> SnapshotInfo:
    """Slice lookback + label tail and atomically persist immutable Parquet."""
    start = pd.Timestamp(train_start)
    end = pd.Timestamp(train_end)
    if start >= end:
        raise RemoteTrainingError(422, "train_start 必须早于 train_end")

    dates = pd.DatetimeIndex(pd.to_datetime(pivot.index)).sort_values().unique()
    train_positions = [
        index for index, date in enumerate(dates) if start <= date <= end
    ]
    if not train_positions:
        raise RemoteTrainingError(422, "训练窗口内没有可用行情")

    lookback_rows = max(252, int(params.get("seq_len", 0) or 0))
    label_tail_rows = int(strategy.label_horizon_days(params))
    if label_tail_rows < 0:
        raise RemoteTrainingError(422, "label_horizon_days 必须大于等于 0")

    first_position = train_positions[0]
    last_position = train_positions[-1]
    data_start_position = max(0, first_position - lookback_rows)
    data_end_position = min(len(dates) - 1, last_position + label_tail_rows)
    actual_tail = data_end_position - last_position
    if actual_tail < label_tail_rows:
        raise RemoteTrainingError(
            422,
            f"训练窗口后仅有 {actual_tail} 个标签尾部交易日，"
            f"需要 {label_tail_rows} 个",
        )

    snapshot_dates = dates[data_start_position : data_end_position + 1]
    snapshot = pivot.loc[pivot.index.isin(snapshot_dates)].copy()
    if snapshot.empty:
        raise RemoteTrainingError(422, "远程训练数据快照为空")

    snapshot_path = task_dir / "training-data.parquet"
    temp_path = task_dir / f".training-data-{secrets.token_hex(8)}.tmp"
    try:
        snapshot.to_parquet(temp_path, compression="snappy", index=True)
        os.replace(temp_path, snapshot_path)
    finally:
        temp_path.unlink(missing_ok=True)

    fields = (
        sorted({str(column[1]) for column in snapshot.columns})
        if isinstance(snapshot.columns, pd.MultiIndex)
        else ["close"]
    )
    effective_train_dates = dates[first_position : last_position + 1]
    return SnapshotInfo(
        path=snapshot_path,
        sha256=file_sha256(snapshot_path),
        data_version=compute_data_version(snapshot),
        rows=len(snapshot),
        columns=len(snapshot.columns),
        fields=fields,
        data_start=snapshot_dates[0].strftime("%Y-%m-%d"),
        data_end=snapshot_dates[-1].strftime("%Y-%m-%d"),
        train_start=effective_train_dates[0].strftime("%Y-%m-%d"),
        train_end=effective_train_dates[-1].strftime("%Y-%m-%d"),
        lookback_rows=first_position - data_start_position,
        label_tail_rows=actual_tail,
    )


async def build_training_snapshot(
    experiment: dict[str, Any],
    strategy: TrainableStrategy,
    params: dict[str, Any],
    train_start: str,
    train_end: str,
    task_dir: Path,
) -> SnapshotInfo:
    pivot = await _load_experiment_pivot(experiment)
    return _persist_snapshot(
        pivot,
        strategy=strategy,
        params=params,
        train_start=train_start,
        train_end=train_end,
        task_dir=task_dir,
    )


class RemoteTrainingService:
    """Database and storage boundary for remote training tasks."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        storage_root: str | Path | None = None,
        snapshot_builder: SnapshotBuilder | None = None,
        now: Callable[[], datetime] = utc_now,
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        self.db_path = Path(
            db_path or settings.abs_path(settings.EXPERIMENT_DB)
        ).resolve()
        self.storage_root = Path(
            storage_root
            or settings.abs_path(f"{settings.MODEL_STORE_DIR}/remote-training")
        ).resolve()
        self.snapshot_builder = snapshot_builder or build_training_snapshot
        self.now = now
        self.max_artifact_bytes = max_artifact_bytes

    async def _connect(self) -> aiosqlite.Connection:
        connection = await aiosqlite.connect(str(self.db_path))
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def public_task(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for field in ("params", "manifest", "report_json"):
            if result.get(field):
                try:
                    result[field] = json.loads(result[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        for private in (
            "token_hash",
            "snapshot_path",
            "artifact_path",
        ):
            result.pop(private, None)
        return result

    async def _owned_task(
        self,
        task_uuid: str,
        user_id: int,
    ) -> aiosqlite.Row:
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                SELECT * FROM remote_training_tasks
                WHERE task_uuid=? AND user_id=?
                """,
                (task_uuid, user_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise RemoteTrainingError(404, "远程训练任务不存在")
        return row

    async def _token_task(
        self,
        task_uuid: str,
        token: str | None,
    ) -> aiosqlite.Row:
        if not token:
            raise RemoteTrainingError(401, "缺少 X-Training-Token")
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                "SELECT * FROM remote_training_tasks WHERE task_uuid=?",
                (task_uuid,),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise RemoteTrainingError(401, "训练令牌无效")
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(candidate, str(row["token_hash"])):
            raise RemoteTrainingError(401, "训练令牌无效")
        expires_at = datetime.fromisoformat(str(row["token_expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if self.now() >= expires_at:
            raise RemoteTrainingError(401, "训练令牌已过期")
        return row

    @staticmethod
    def _derive_window(
        experiment: dict[str, Any],
        metadata: Any,
        params: dict[str, Any],
        train_start: str | None,
        train_end: str | None,
    ) -> tuple[str, str]:
        if bool(train_start) != bool(train_end):
            raise RemoteTrainingError(422, "train_start 和 train_end 必须同时提供")
        start_value = train_start or experiment.get("train_start")
        end_value = train_end or experiment.get("train_end")
        test_start = pd.Timestamp(experiment["test_start"])

        periodic = metadata.retrain_frequency != RetrainFrequency.NEVER
        if end_value is None and periodic:
            end_value = (test_start - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if start_value is None and periodic:
            end_timestamp = pd.Timestamp(end_value)
            if str(params.get("window_mode", "expanding")) == "rolling":
                months = int(params.get("rolling_train_months", 36))
                start_value = (
                    end_timestamp - pd.DateOffset(months=months)
                ).strftime("%Y-%m-%d")
            else:
                start_value = DEFAULT_DATA_START
        if not start_value or not end_value:
            raise RemoteTrainingError(422, "远程训练必须具有确定的完整训练窗口")

        start_timestamp = pd.Timestamp(start_value)
        end_timestamp = pd.Timestamp(end_value)
        if start_timestamp >= end_timestamp:
            raise RemoteTrainingError(422, "train_start 必须早于 train_end")
        if end_timestamp >= test_start:
            raise RemoteTrainingError(422, "训练窗口必须在测试窗口之前结束")
        return start_timestamp.strftime("%Y-%m-%d"), end_timestamp.strftime(
            "%Y-%m-%d"
        )

    async def create_task(
        self,
        *,
        experiment_id: int,
        user_id: int,
        registry: Any,
        train_start: str | None = None,
        train_end: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                "SELECT * FROM experiments WHERE id=? AND user_id=?",
                (experiment_id, user_id),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise RemoteTrainingError(404, "实验不存在或不属于当前用户")
        experiment = dict(row)

        try:
            strategy = registry.create_strategy(experiment["strategy_id"])
            metadata = registry.get_metadata(experiment["strategy_id"])
        except KeyError as exc:
            raise RemoteTrainingError(422, "实验策略未注册") from exc
        if not isinstance(strategy, TrainableStrategy):
            raise RemoteTrainingError(422, "远程训练仅支持 TrainableStrategy")

        params = _parse_json_object(experiment.get("params"), field="params")
        params_digest = _params_hash(params)
        is_valid, validation_error = registry.validate_params(
            experiment["strategy_id"],
            params,
        )
        if not is_valid:
            raise RemoteTrainingError(422, f"策略参数无效: {validation_error}")
        resolved_start, resolved_end = self._derive_window(
            experiment,
            metadata,
            params,
            train_start,
            train_end,
        )

        task_uuid = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = self.now() + timedelta(hours=TOKEN_TTL_HOURS)
        task_dir = _safe_task_dir(self.storage_root, task_uuid)
        try:
            snapshot = await self.snapshot_builder(
                experiment,
                strategy,
                params,
                resolved_start,
                resolved_end,
                task_dir,
            )
            manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "task": {
                    "task_uuid": task_uuid,
                    "experiment_id": experiment_id,
                    "single_window_only": True,
                    "completes_walk_forward": False,
                },
                "strategy": {
                    "strategy_id": experiment["strategy_id"],
                    "version": str(metadata.version),
                    "retrain_frequency": (
                        metadata.retrain_frequency.value
                        if hasattr(metadata.retrain_frequency, "value")
                        else str(metadata.retrain_frequency)
                    ),
                    "source_sha256": _strategy_source_sha256(strategy),
                },
                "params": params,
                "params_sha256": params_digest,
                "windows": {
                    "requested_train_start": resolved_start,
                    "requested_train_end": resolved_end,
                    "effective_train_start": snapshot.train_start,
                    "effective_train_end": snapshot.train_end,
                    "data_start": snapshot.data_start,
                    "data_end": snapshot.data_end,
                    "lookback_rows": snapshot.lookback_rows,
                    "label_tail_rows": snapshot.label_tail_rows,
                },
                "data": {
                    "filename": "training-data.parquet",
                    "sha256": snapshot.sha256,
                    "data_version": snapshot.data_version,
                    "rows": snapshot.rows,
                    "columns": snapshot.columns,
                    "fields": snapshot.fields,
                },
                "token_expires_at": _iso(expires_at),
                "max_artifact_bytes": self.max_artifact_bytes,
            }
            connection = await self._connect()
            try:
                await connection.execute(
                    """
                    INSERT INTO remote_training_tasks
                        (task_uuid, user_id, experiment_id, strategy_id, status,
                         token_hash, token_expires_at, params, params_hash,
                         train_start, train_end, data_start, data_end,
                         data_version, data_sha256, data_rows, data_columns,
                         data_fields, manifest, snapshot_path, max_upload_bytes,
                         progress, progress_message, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, 0, '等待远程 Worker', ?, ?)
                    """,
                    (
                        task_uuid,
                        user_id,
                        experiment_id,
                        experiment["strategy_id"],
                        token_hash,
                        _iso(expires_at),
                        json.dumps(params, ensure_ascii=False, sort_keys=True),
                        params_digest,
                        snapshot.train_start,
                        snapshot.train_end,
                        snapshot.data_start,
                        snapshot.data_end,
                        snapshot.data_version,
                        snapshot.sha256,
                        snapshot.rows,
                        snapshot.columns,
                        json.dumps(snapshot.fields, ensure_ascii=False),
                        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                        str(snapshot.path),
                        self.max_artifact_bytes,
                        _iso(self.now()),
                        _iso(self.now()),
                    ),
                )
                await connection.commit()
                cursor = await connection.execute(
                    "SELECT * FROM remote_training_tasks WHERE task_uuid=?",
                    (task_uuid,),
                )
                created = await cursor.fetchone()
            finally:
                await connection.close()
        except Exception:
            snapshot_path = task_dir / "training-data.parquet"
            snapshot_path.unlink(missing_ok=True)
            try:
                task_dir.rmdir()
            except OSError:
                pass
            raise
        assert created is not None
        return self.public_task(created), token

    async def list_tasks(
        self,
        *,
        user_id: int,
        experiment_id: int | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["user_id=?"]
        values: list[Any] = [user_id]
        if experiment_id is not None:
            conditions.append("experiment_id=?")
            values.append(experiment_id)
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                f"""
                SELECT * FROM remote_training_tasks
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC
                """,
                values,
            )
            rows = await cursor.fetchall()
        finally:
            await connection.close()
        return [self.public_task(row) for row in rows]

    async def get_task(self, *, task_uuid: str, user_id: int) -> dict[str, Any]:
        return self.public_task(await self._owned_task(task_uuid, user_id))

    async def cancel_task(
        self,
        *,
        task_uuid: str,
        user_id: int,
    ) -> dict[str, Any]:
        row = await self._owned_task(task_uuid, user_id)
        if row["status"] in TERMINAL_STATUSES:
            raise RemoteTrainingError(409, "终态任务不能被覆盖")
        now = _iso(self.now())
        revoked_token_hash = _revoked_token_hash()
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                UPDATE remote_training_tasks
                SET status='cancelled', progress_message='用户已取消',
                    token_hash=?, completed_at=?, updated_at=?
                WHERE task_uuid=? AND user_id=?
                  AND status IN ('created', 'running')
                """,
                (revoked_token_hash, now, now, task_uuid, user_id),
            )
            if cursor.rowcount != 1:
                raise RemoteTrainingError(409, "任务状态已变化")
            await connection.commit()
        finally:
            await connection.close()
        return await self.get_task(task_uuid=task_uuid, user_id=user_id)

    async def worker_manifest(
        self,
        *,
        task_uuid: str,
        token: str | None,
    ) -> dict[str, Any]:
        row = await self._token_task(task_uuid, token)
        return _parse_json_object(row["manifest"], field="manifest")

    async def worker_data_path(
        self,
        *,
        task_uuid: str,
        token: str | None,
    ) -> Path:
        row = await self._token_task(task_uuid, token)
        path = Path(str(row["snapshot_path"])).resolve()
        expected_parent = _safe_task_dir(self.storage_root, task_uuid)
        if path.parent != expected_parent or not path.is_file():
            raise RemoteTrainingError(410, "训练数据快照不存在")
        if file_sha256(path) != row["data_sha256"]:
            raise RemoteTrainingError(409, "训练数据快照校验失败")
        return path

    async def worker_start(
        self,
        *,
        task_uuid: str,
        token: str | None,
    ) -> dict[str, Any]:
        row = await self._token_task(task_uuid, token)
        if row["status"] != "created":
            raise RemoteTrainingError(409, "只有 created 任务可以开始")
        now = _iso(self.now())
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                UPDATE remote_training_tasks
                SET status='running', progress=0,
                    progress_message='远程训练已开始',
                    started_at=?, updated_at=?
                WHERE task_uuid=? AND status='created'
                """,
                (now, now, task_uuid),
            )
            if cursor.rowcount != 1:
                raise RemoteTrainingError(409, "任务状态已变化")
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM remote_training_tasks WHERE task_uuid=?",
                (task_uuid,),
            )
            updated = await cursor.fetchone()
        finally:
            await connection.close()
        assert updated is not None
        return self.public_task(updated)

    async def worker_progress(
        self,
        *,
        task_uuid: str,
        token: str | None,
        progress: float,
        message: str | None,
    ) -> dict[str, Any]:
        row = await self._token_task(task_uuid, token)
        if row["status"] != "running":
            raise RemoteTrainingError(409, "只有 running 任务可以更新进度")
        if not 0 <= progress < 1:
            raise RemoteTrainingError(422, "progress 必须在 [0, 1) 范围内")
        now = _iso(self.now())
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                UPDATE remote_training_tasks
                SET progress=?, progress_message=?, updated_at=?
                WHERE task_uuid=? AND status='running'
                """,
                (progress, message or "远程训练中", now, task_uuid),
            )
            if cursor.rowcount != 1:
                raise RemoteTrainingError(409, "任务状态已变化")
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM remote_training_tasks WHERE task_uuid=?",
                (task_uuid,),
            )
            updated = await cursor.fetchone()
        finally:
            await connection.close()
        assert updated is not None
        return self.public_task(updated)

    @staticmethod
    def _validate_report(
        report: dict[str, Any],
        row: aiosqlite.Row,
    ) -> None:
        expected = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "task_uuid": row["task_uuid"],
            "experiment_id": row["experiment_id"],
            "strategy_id": row["strategy_id"],
            "params_sha256": row["params_hash"],
            "data_sha256": row["data_sha256"],
        }
        mismatches = [
            field for field, value in expected.items() if report.get(field) != value
        ]
        if mismatches:
            raise RemoteTrainingError(
                422,
                "训练报告与任务不匹配: " + ", ".join(mismatches),
            )

    async def worker_complete(
        self,
        *,
        task_uuid: str,
        token: str | None,
        report_json: str,
        artifact: AsyncUpload,
    ) -> dict[str, Any]:
        row = await self._token_task(task_uuid, token)
        if row["status"] != "running":
            raise RemoteTrainingError(409, "只有 running 任务可以完成")
        if len(report_json.encode("utf-8")) > MAX_REPORT_JSON_BYTES:
            raise RemoteTrainingError(413, "训练报告超过 1 MiB 上限")
        report = _parse_json_object(report_json, field="report_json")
        try:
            json.dumps(
                report,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RemoteTrainingError(
                422,
                "训练报告必须是仅含有限数值的规范 JSON 对象",
            ) from exc
        self._validate_report(report, row)
        if not artifact.filename:
            raise RemoteTrainingError(422, "artifact 文件名不能为空")

        task_dir = _safe_task_dir(self.storage_root, task_uuid)
        temp_path = task_dir / f".upload-{secrets.token_hex(8)}.tmp"
        size = 0
        digest = hashlib.sha256()
        try:
            with temp_path.open("xb") as handle:
                while True:
                    chunk = await artifact.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > int(row["max_upload_bytes"]):
                        raise RemoteTrainingError(413, "模型产物超过上传上限")
                    digest.update(chunk)
                    handle.write(chunk)
            if size == 0:
                raise RemoteTrainingError(422, "模型产物不能为空")
            artifact_sha256 = digest.hexdigest()
            # Each contender gets a distinct path.  A losing concurrent
            # completion may safely delete its own upload without removing or
            # replacing the artifact committed by the winner.
            artifact_path = task_dir / (
                f"artifact-{artifact_sha256[:24]}-{secrets.token_hex(8)}.bin"
            )
            os.replace(temp_path, artifact_path)
        finally:
            temp_path.unlink(missing_ok=True)

        now = _iso(self.now())
        revoked_token_hash = _revoked_token_hash()
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                UPDATE remote_training_tasks
                SET status='completed', progress=1,
                    progress_message='远程训练完成',
                    report_json=?, artifact_path=?, artifact_sha256=?,
                    artifact_size=?, token_hash=?, completed_at=?, updated_at=?
                WHERE task_uuid=? AND status='running'
                """,
                (
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                    str(artifact_path),
                    artifact_sha256,
                    size,
                    revoked_token_hash,
                    now,
                    now,
                    task_uuid,
                ),
            )
            if cursor.rowcount != 1:
                artifact_path.unlink(missing_ok=True)
                raise RemoteTrainingError(409, "任务状态已变化")
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM remote_training_tasks WHERE task_uuid=?",
                (task_uuid,),
            )
            updated = await cursor.fetchone()
        finally:
            await connection.close()
        assert updated is not None
        return self.public_task(updated)

    async def worker_fail(
        self,
        *,
        task_uuid: str,
        token: str | None,
        error: str,
    ) -> dict[str, Any]:
        row = await self._token_task(task_uuid, token)
        if row["status"] in TERMINAL_STATUSES:
            raise RemoteTrainingError(409, "终态任务不能被覆盖")
        now = _iso(self.now())
        revoked_token_hash = _revoked_token_hash()
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                """
                UPDATE remote_training_tasks
                SET status='failed', error=?, progress_message='远程训练失败',
                    token_hash=?, completed_at=?, updated_at=?
                WHERE task_uuid=? AND status IN ('created', 'running')
                """,
                (error[:8000], revoked_token_hash, now, now, task_uuid),
            )
            if cursor.rowcount != 1:
                raise RemoteTrainingError(409, "任务状态已变化")
            await connection.commit()
            cursor = await connection.execute(
                "SELECT * FROM remote_training_tasks WHERE task_uuid=?",
                (task_uuid,),
            )
            updated = await cursor.fetchone()
        finally:
            await connection.close()
        assert updated is not None
        return self.public_task(updated)
