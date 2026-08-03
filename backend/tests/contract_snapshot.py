"""Contract snapshot helpers — shared by the snapshot generator and CI gate.

v0.3.0 契约锁定层：为全部端点生成可复现的响应 golden 快照。

设计约定
--------
- 探测顺序：GET（只读）优先 → 写操作 → 最后注销，保证同一进程内多次探测结果一致。
- 隔离：每次探测使用全新临时数据库 + 离线数据服务 + 独立 JobBroker，
  永不触网、永不写真实 data/ 目录。
- 规范化：时间戳/日期/UUID/JWT/哈希/id/分页/系统指标统一替换为占位符，
  使快照只锁"响应结构"，不锁易变值。
- 易变端点（见 _VOLATILE_ENDPOINTS）：机器实时负载/健康/执行适配器状态类端点
  只锁状态码与可达性，不锁内部值（其 schema 仍由 OpenAPI golden 锁定）。
- 本模块不含任何真实数据库写入，仅用于生成与校验快照。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api import data as data_api
from backend.config import settings
from backend.data.sources import validated as validated_sources
from backend.jobs import broker as broker_module
from backend.jobs.broker import JobBroker
from backend.main import app as test_app

# ── 路径 ──────────────────────────────────────────────────────────────────

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
OPENAPI_GOLDEN = SNAPSHOT_DIR / "openapi_v1.json"
ENDPOINTS_GOLDEN = SNAPSHOT_DIR / "endpoints_v1.json"
ZERO_COVERAGE_GOLDEN = SNAPSHOT_DIR / "zero_coverage_v1.json"


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 离线数据服务（与 tests/test_api_alignment.py 相同意图的紧凑实现） ──


class _OfflineSource:
    """Offline market source that never touches the network."""

    _TRADING_DAYS = ("2024-01-02", "2024-01-03")

    async def fetch_daily(self, codes, start, end):
        import numpy as np
        import pandas as pd

        days = [d for d in self._TRADING_DAYS if start <= d <= end]
        index = pd.DatetimeIndex(days, name="date")
        fields = ("open", "close", "high", "low", "volume", "amount")
        columns = pd.MultiIndex.from_product([codes, fields], names=["code", "field"])
        vals = np.zeros((len(index), len(codes) * len(fields)))
        return pd.DataFrame(vals, index=index, columns=columns)

    async def fetch_index_components(self, index_code, date=None):
        return ["000001", "600000"]

    async def fetch_trading_calendar(self, start, end):
        return [d for d in self._TRADING_DAYS if start <= d <= end]

    async def fetch_industry_list(self):
        return [{"code": "BK0475", "name": "Banking"}]


class _OfflineCalendar:
    _DAYS = ("2024-01-02", "2024-01-03")

    async def load(self, source, start, end):
        return [d for d in self._DAYS if start <= d <= end]

    async def ensure_loaded(self, source, date):
        pass

    def is_trading_day(self, date):
        return date in self._DAYS

    def next_trading_day(self, date):
        for d in self._DAYS:
            if d > date:
                return d
        return None

    def prev_trading_day(self, date):
        for d in reversed(self._DAYS):
            if d < date:
                return d
        return None


class _OfflineCache:
    async def get_cache_info(self, pool_id):
        return {"pool_id": pool_id, "exists": False, "source": "offline:contract-snapshot"}

    async def load_pivot(self, pool_id):
        return None

    async def invalidate(self, pool_id):
        return True


class _OfflineUniverse:
    _CODES = ("000001", "600000")
    _INDUSTRIES = {"000001": "Banking", "600000": "Banking"}

    async def get_pool_snapshot(self, pool_id, date=None, *, include_industry_quality=True):
        from backend.data.lineage import build_universe_snapshot

        return build_universe_snapshot(
            pool_id,
            self._CODES,
            requested_as_of=date,
            source_as_of="2024-01-02",
            point_in_time=False,
            source_requested_count=len(self._CODES),
            expected_count=None,
            industry_map=self._INDUSTRIES,
            risk_warnings=("offline_fixture",),
        )

    async def get_pool_codes(self, pool_id, date=None):
        return list(self._CODES)

    async def get_pool_info(self, pool_id, date=None):
        snap = await self.get_pool_snapshot(pool_id, date)
        return {
            "pool_id": pool_id,
            "name": "Offline",
            "description": "offline",
            "index_code": "000905",
            "n_stocks": len(snap.codes),
            "industries": [{"industry": "Banking", "count": len(snap.codes), "pct": 100.0}],
            "lineage": {
                "schema_version": snap.schema_version,
                "requested_as_of": snap.requested_as_of,
                "source_as_of": snap.source_as_of,
                "point_in_time": snap.point_in_time,
                "snapshot_hash": snap.snapshot_hash,
            },
            "quality": snap.quality.to_dict(),
            "risk_warnings": list(snap.risk_warnings),
        }

    async def get_industry_map(self, *, strict=False):
        return dict(self._INDUSTRIES)

    async def get_industry_readiness(self, codes=None, *, refresh_missing=False):
        requested = sorted(set(codes or []))
        mapped = sum(c in self._INDUSTRIES for c in requested)
        return {
            "filterable": bool(requested) and mapped == len(requested),
            "reason": None if requested else "coverage_not_evaluated",
            "source": "offline:contract-snapshot",
            "classification": "cninfo_008001",
            "mapped_stocks": len(self._INDUSTRIES),
            "requested_stocks": len(requested),
            "requested_mapped_stocks": mapped,
            "map_coverage": (mapped / len(requested)) if requested else None,
            "coverage_scope": "requested_codes" if requested else "not_evaluated",
            "minimum_coverage": 0.95,
        }

    async def filter_by_industry(self, codes, industries):
        return [c for c in codes if self._INDUSTRIES.get(c) in industries]


class _OfflineDataServices:
    def __init__(self):
        self.source = _OfflineSource()
        self.cache = _OfflineCache()
        self.calendar = _OfflineCalendar()
        self.universe = _OfflineUniverse()

    @property
    def s(self):
        return self.source

    @property
    def c(self):
        return self.cache

    @property
    def cal(self):
        return self.calendar

    @property
    def u(self):
        return self.universe


# ── 隔离客户端构建 ────────────────────────────────────────────────────────


def build_isolated_client(tmp_path: Path) -> tuple[TestClient, pytest.MonkeyPatch]:
    """Build a TestClient against fully isolated temporary state.

    所有可写路径均指向 tmp_path，禁用一切后台调度器，数据层替换为离线实现，
    broker 换成独立实例。返回 (client, patcher)；使用方必须 finally patcher.undo()。
    """
    patcher = pytest.MonkeyPatch()
    path_settings = {
        "DATABASE_DIR": tmp_path,
        "USERS_DB": tmp_path / "users.db",
        "EXPERIMENT_DB": tmp_path / "experiment.db",
        "TRADING_SIM_DB": tmp_path / "trading_sim.db",
        "TRADING_LIVE_DB": tmp_path / "trading_live.db",
        "DATA_CACHE_DIR": tmp_path / "cache",
        "DATA_STAGING_DIR": tmp_path / "staging",
        "PIT_EVIDENCE_DIR": tmp_path / "pit_evidence",
        "PIT_EVIDENCE_DB": tmp_path / "pit_evidence" / "governance.db",
        "PIT_LICENCE_EVIDENCE_DB": tmp_path / "pit_evidence" / "licence_evidence.db",
        "MODEL_STORE_DIR": tmp_path / "models",
        "RESEARCH_SNAPSHOT_DIR": tmp_path / "research_snapshots",
        "RESEARCH_DATA_DIR": tmp_path / "research_data",
    }
    for name, path in path_settings.items():
        patcher.setattr(settings, name, str(path))
    patcher.setattr(settings, "ENVIRONMENT", "test")
    patcher.setattr(settings, "JWT_SECRET", "contract-lock-test-secret-" + ("s" * 48))
    patcher.setattr(settings, "PAPER_SIMULATION_AUTO_RUN", False)
    patcher.setattr(settings, "PAPER_SIMULATION_REFRESH_DATA", False)
    patcher.setattr(settings, "MODEL_RETRAIN_AUTO_RUN", False)
    patcher.setattr(settings, "PIT_AUTOMATION_AUTO_RUN", False)
    patcher.setattr(settings, "PIT_CANDIDATE_PREFLIGHT_AUTO_RUN", False)
    patcher.setattr(settings, "RESEARCH_DATA_REFRESH_AUTO_RUN", False)
    patcher.setattr(settings, "JOB_SCHEDULER_ENABLED", False)

    def reject_live_market_source():
        raise AssertionError("Contract lock tests must not init live market source")

    patcher.setattr(validated_sources, "build_public_research_source", reject_live_market_source)
    patcher.setattr(data_api, "_data_svc", _OfflineDataServices())
    patcher.setattr(broker_module, "_broker_instance", JobBroker(str(tmp_path / "jobs.db")))
    # 清掉进程级 memo 缓存，避免其他测试在同一进程先调用造成非确定性。
    from backend.api.price_ledger import _legacy_audit_cache

    _legacy_audit_cache.clear()
    from backend.auth.rate_limit import reset_auth_rate_limits_for_tests

    reset_auth_rate_limits_for_tests()
    client = TestClient(test_app)
    return client, patcher


def register_admin(client: TestClient) -> dict[str, str]:
    """Register the bootstrap admin and return auth headers."""
    resp = client.post(
        "/api/auth/register",
        json={
            "username": "contract_admin",
            "password": "contract-pass-123",
            "display_name": "Contract Admin",
        },
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ── Path/query/body 生成（从 OpenAPI schema 驱动） ────────────────────────

_SPECIAL_PATH_VALUES = {
    "experiment_id": "1", "sweep_id": "1", "deployment_id": "1", "portfolio_id": "1",
    "preset_id": "1", "run_id": "1", "job_id": "1", "delivery_id": "1", "target_user_id": "2",
    "user_id": "2", "session_id": "1", "package_id": "1", "binding_id": "1", "group_id": "1",
    "hypothesis_id": "1", "promotion_id": "1", "protocol_id": "1", "version": "1",
    "revision": "1", "strategy_id": "ma_cross_v1", "pool_id": "csi500", "date": "2025-01-02",
    "code": "000001.SZ", "record_sha256": "a" * 64, "factor_id": "momentum_20",
    "task_uuid": "00000000-0000-0000-0000-000000000000",
}

_SPECIAL_QUERY_VALUES = {
    "pool_id": "csi500", "start": "2024-01-01", "end": "2024-01-31", "date": "2025-01-02",
    "domain": "price", "scope_id": "csi500", "role": "public", "codes": "000001.SZ",
    "experiment_ids": "1,2", "name": "probe", "strategy_id": "ma_cross_v1",
    "entity_type": "experiment", "entity_id": "1", "page": "1", "page_size": "10",
    "limit": "10", "status": "pending", "category": "technical", "q": "probe",
    "security_code": "000001.SZ", "effective_start": "2024-01-01", "effective_end": "2024-01-31",
    "as_known_at": "2025-01-02",
}


def _resolve_schema(schema: dict | None) -> dict:
    while schema and "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = test_app.openapi()["components"]["schemas"].get(name, {})
    return schema or {}


def _query_value(param: dict) -> str:
    schema = param.get("schema") or {}
    if schema.get("default") is not None:
        return str(schema["default"])
    name = param["name"]
    if name in _SPECIAL_QUERY_VALUES:
        return _SPECIAL_QUERY_VALUES[name]
    t = schema.get("type")
    if t == "integer":
        return "1"
    if t == "number":
        return "1.0"
    if t == "boolean":
        return "true"
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return str(schema["enum"][0])
    return "probe"


def _body_value(schema: dict | None) -> Any:
    schema = _resolve_schema(schema or {})
    if not schema:
        return None
    if schema.get("default") is not None:
        return schema["default"]
    t = schema.get("type")
    if t == "string":
        if isinstance(schema.get("enum"), list) and schema["enum"]:
            return schema["enum"][0]
        fmt = schema.get("format")
        if fmt == "date":
            return "2025-01-02"
        if fmt == "date-time":
            return "2025-01-02T00:00:00"
        if schema.get("pattern"):
            return "probe-1"
        return "probe"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return False
    if t == "array":
        item = schema.get("items")
        return [_body_value(item)] if item else []
    if t == "object":
        props = schema.get("properties") or {}
        return {k: _body_value(v) for k, v in props.items()}
    return None


def _body_for_operation(op: dict) -> Any:
    rb = op.get("requestBody")
    if not rb:
        return None
    content = rb.get("content") or {}
    schema = _resolve_schema((content.get("application/json") or {}).get("schema"))
    if not schema:
        return None
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    chosen = set(required) | set(list(props)[:3])
    return {k: _body_value(v) for k, v in props.items() if k in chosen}


def _path_value(name: str) -> str:
    return _SPECIAL_PATH_VALUES.get(name, "1")


_PUBLIC_PATHS = {"/api/auth/register", "/api/auth/login", "/api/auth/refresh"}
_LOGOUT_PATHS = {("/api/auth/logout", "post")}


def probe_all(client: TestClient, headers: dict[str, str]) -> dict[str, dict]:
    """Probe every OpenAPI operation once: GET first, writes next, logout last."""
    schema = test_app.openapi()
    operations = []
    for path in sorted(schema["paths"]):
        for method in sorted(schema["paths"][path]):
            if method in ("head", "options"):
                continue
            operations.append((method.upper(), path, schema["paths"][path][method]))

    def sort_key(item):
        method, path, _op = item
        if (path, method.lower()) in _LOGOUT_PATHS:
            return (2, path, method)
        if method == "GET":
            return (0, path, method)
        return (1, path, method)

    operations.sort(key=sort_key)

    results: dict[str, dict] = {}
    for method, path, op in operations:
        full_path = path
        for p in op.get("parameters", []):
            if p.get("in") == "path":
                full_path = full_path.replace("{" + p["name"] + "}", _path_value(p["name"]))
        params = {}
        for p in op.get("parameters", []):
            if p.get("in") == "query" and (p.get("required") or p["name"] in ("page", "page_size")):
                params[p["name"]] = _query_value(p)
        body = _body_for_operation(op)
        req_headers = headers if path not in _PUBLIC_PATHS else {}
        try:
            r = client.request(method, full_path, params=params or None, json=body, headers=req_headers)
            try:
                body_out = r.json()
            except Exception:
                body_out = {"__raw__": r.text[:500]}
            results[f"{method} {path}"] = {"status": r.status_code, "body": body_out}
        except Exception as e:
            results[f"{method} {path}"] = {"error": repr(e)}
    return results


# ── 规范化（只锁结构，不锁易变值） ───────────────────────────────────────

_RE_JWT = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_RE_HEX = re.compile(r"^[0-9a-f]{32,64}$", re.I)
_RE_DT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_KEYS = (
    "id", "job_uuid", "user_id", "experiment_id", "deployment_id", "portfolio_id",
    "preset_id", "sweep_id", "run_id", "delivery_id", "group_id", "hypothesis_id",
    "promotion_id", "protocol_id", "package_id", "binding_id", "session_id",
    "target_user_id", "revision", "model_version", "attempt_id",
)
_PAGINATION_KEYS = (
    "total", "total_count", "count", "page_count", "has_more", "page", "page_size", "limit", "offset",
)
_METRIC_KEYS = (
    "memory_available_mb", "memory_used_ratio", "disk_free_mb", "cpu_load",
    "io_pressure", "memory_used_mb", "disk_used_mb", "cpu_count", "load_avg_1m",
    "swap_used_mb", "swap_total_mb", "memory_total_mb", "disk_total_mb", "metrics",
    "normalized_load", "load_1m",
)

# 机器实时状态类端点：只锁状态码与可达性，不锁内部易变值。
_VOLATILE_ENDPOINTS = frozenset({
    "GET /api/health",
    "GET /api/jobs/summary",
    "GET /api/jobs/observability",
    "GET /api/execution/adapters/readiness",
    "GET /api/execution/live-readiness",
})


_IDENTITY_KEYS = (
    "strategy_id", "adapter_id", "pool_id", "code", "key", "id",
    "experiment_id", "job_id", "deployment_id", "name", "username",
)


def _identity_key(value: dict) -> str | None:
    for k in _IDENTITY_KEYS:
        if k in value:
            return k
    return None


def _normalize_value(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _normalize_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and _identity_key(v) for v in value):
            ik = _identity_key(value[0])
            value = sorted(value, key=lambda v: str(v[ik]))
        return [_normalize_value(v, key) for v in value]
    if isinstance(value, str):
        if _RE_JWT.match(value):
            return "<jwt>"
        if _RE_UUID.match(value):
            return "<uuid>"
        if _RE_HEX.match(value):
            return "<hash>"
        if _RE_DT.match(value):
            return "<datetime>"
        if _RE_DATE.match(value):
            return "<date>"
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if key in _ID_KEYS:
            return "<id>"
        if key in _PAGINATION_KEYS:
            return "<count>"
        if key in _METRIC_KEYS:
            return "<metric>"
        return value
    return value


def normalize_result(key: str, result: dict) -> dict:
    """Normalize one probe result so snapshots lock structure, not volatile values.

    易变端点只锁状态码；其余端点锁"状态码 + 规范化后的响应体"。
    """
    if key in _VOLATILE_ENDPOINTS:
        return {"status": result.get("status")}
    return {
        "status": result.get("status"),
        "body": _normalize_value(result.get("body")),
    }


# ── 零覆盖清单（AST 静态分析测试引用） ────────────────────────────────────

_TEST_ROOTS = (
    Path(__file__).resolve().parents[2] / "backend" / "tests",
    Path(__file__).resolve().parents[2] / "tests" / "integration",
    Path(__file__).resolve().parents[2] / "tests" / "test_api_alignment.py",
)


def compute_zero_coverage() -> list[str]:
    """端点路径中被测试代码引用的集合之差。

    通过 AST 提取测试文件中的字符串字面量与 f-string 模板，与 OpenAPI 路径
    做正则匹配（{param} -> [^/]+），未命中任何测试引用的端点列入零覆盖清单。
    """
    schema = test_app.openapi()
    referenced: set[str] = set()
    root = Path(__file__).resolve().parents[2]
    files = list((root / "backend" / "tests").glob("*.py"))
    files += list((root / "tests" / "integration").glob("*.py"))
    files += [root / "tests" / "test_api_alignment.py"]
    for tf in files:
        try:
            tree = ast.parse(tf.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                referenced.add(node.value)
            elif isinstance(node, ast.JoinedStr):
                parts = []
                for v in node.values:
                    if isinstance(v, ast.Constant):
                        parts.append(v.value)
                    else:
                        parts.append("{x}")
                referenced.add("".join(parts))

    covered: set[str] = set()
    for path in schema["paths"]:
        regex = "^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"
        for lit in referenced:
            if lit.startswith("/api") and re.fullmatch(regex, lit.rstrip("/")):
                covered.add(path)
                break

    zero = sorted(set(schema["paths"]) - covered)
    result = []
    for path in zero:
        for method in sorted(schema["paths"][path]):
            if method in ("head", "options"):
                continue
            result.append(f"{method.upper()} {path}")
    return result
