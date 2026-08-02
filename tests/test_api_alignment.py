"""
API Alignment Integration Test Suite

Tests every backend API endpoint for:
1. Route registration (no 404)
2. Return format consistency ({data: ...} wrapping)
3. Auth behavior (401 without token)
4. Key functional paths (register -> login -> list -> query)
5. Backend core module correctness

Run: python -m pytest tests/test_api_alignment.py -v
"""
import sys
import os
import re
import uuid
import pytest
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.config import settings
from backend.data.lineage import build_universe_snapshot
from backend.version import APP_VERSION

app = None
client = None
_test_runtime_root = None


class _OfflineMarketSource:
    """Small deterministic market-data source for API contract tests."""

    _TRADING_DAYS = (
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-06-28",
    )
    _FIELDS = ("open", "close", "high", "low", "volume", "amount")

    async def fetch_daily(
        self,
        codes: list[str],
        start: str,
        end: str,
    ) -> pd.DataFrame:
        days = [
            day for day in self._TRADING_DAYS
            if start <= day <= end
        ]
        index = pd.DatetimeIndex(days, name="date")
        columns = pd.MultiIndex.from_product(
            [codes, self._FIELDS],
            names=["code", "field"],
        )
        values: dict[tuple[str, str], list[float]] = {}
        for code_position, code in enumerate(codes):
            base = 10.0 + code_position
            for field in self._FIELDS:
                if field == "volume":
                    values[(code, field)] = [
                        1_000_000.0 + day_position
                        for day_position in range(len(index))
                    ]
                elif field == "amount":
                    values[(code, field)] = [
                        10_000_000.0 + day_position
                        for day_position in range(len(index))
                    ]
                else:
                    offset = {
                        "open": 0.0,
                        "close": 0.2,
                        "high": 0.4,
                        "low": -0.2,
                    }[field]
                    values[(code, field)] = [
                        base + offset + day_position
                        for day_position in range(len(index))
                    ]
        return pd.DataFrame(values, index=index, columns=columns)

    async def fetch_index_components(
        self,
        index_code: str,
        date: str | None = None,
    ) -> list[str]:
        del index_code, date
        return ["000001", "600000"]

    async def fetch_trading_calendar(
        self,
        start: str,
        end: str,
    ) -> list[str]:
        return [
            day for day in self._TRADING_DAYS
            if start <= day <= end
        ]

    async def fetch_industry_list(self) -> list[dict[str, str]]:
        return [
            {"code": "BK0475", "name": "Banking"},
            {"code": "BK0737", "name": "Software"},
        ]


class _OfflineUniverse:
    """Universe facade with stable lineage and complete industry coverage."""

    _CODES = ("000001", "600000")
    _INDUSTRIES = {
        "000001": "Banking",
        "600000": "Banking",
    }

    @classmethod
    def _snapshot(cls, pool_id: str, date: str | None = None):
        return build_universe_snapshot(
            pool_id,
            cls._CODES,
            requested_as_of=date,
            source_as_of="2024-01-02",
            point_in_time=False,
            source_requested_count=len(cls._CODES),
            expected_count=None,
            industry_map=cls._INDUSTRIES,
            risk_warnings=("offline_fixture",),
        )

    async def get_pool_snapshot(
        self,
        pool_id: str,
        date: str | None = None,
        *,
        include_industry_quality: bool = True,
    ):
        del include_industry_quality
        return self._snapshot(pool_id, date)

    async def get_pool_codes(
        self,
        pool_id: str,
        date: str | None = None,
    ) -> list[str]:
        return list(self._snapshot(pool_id, date).codes)

    async def get_pool_info(
        self,
        pool_id: str,
        date: str | None = None,
    ) -> dict:
        snapshot = self._snapshot(pool_id, date)
        return {
            "pool_id": pool_id,
            "name": "Offline CSI 500",
            "description": "Deterministic integration-test universe",
            "index_code": "000905",
            "n_stocks": len(snapshot.codes),
            "industries": [
                {
                    "industry": "Banking",
                    "count": len(snapshot.codes),
                    "pct": 100.0,
                }
            ],
            "lineage": {
                "schema_version": snapshot.schema_version,
                "requested_as_of": snapshot.requested_as_of,
                "source_as_of": snapshot.source_as_of,
                "point_in_time": snapshot.point_in_time,
                "snapshot_hash": snapshot.snapshot_hash,
            },
            "quality": snapshot.quality.to_dict(),
            "risk_warnings": list(snapshot.risk_warnings),
        }

    async def get_industry_map(
        self,
        *,
        strict: bool = False,
    ) -> dict[str, str]:
        del strict
        return dict(self._INDUSTRIES)

    async def get_industry_readiness(
        self,
        codes: list[str] | None = None,
        *,
        refresh_missing: bool = False,
    ) -> dict:
        del refresh_missing
        requested = sorted(set(codes or []))
        mapped = sum(code in self._INDUSTRIES for code in requested)
        coverage = mapped / len(requested) if requested else None
        return {
            "filterable": bool(requested) and coverage == 1.0,
            "reason": None if requested else "coverage_not_evaluated",
            "source": "akshare:cninfo",
            "classification": "cninfo_008001",
            "mapped_stocks": len(self._INDUSTRIES),
            "requested_stocks": len(requested),
            "requested_mapped_stocks": mapped,
            "map_coverage": coverage,
            "coverage_scope": (
                "requested_codes" if requested else "not_evaluated"
            ),
            "minimum_coverage": 0.95,
        }

    async def filter_by_industry(
        self,
        codes: list[str],
        industries: list[str],
    ) -> list[str]:
        requested = set(industries)
        return [
            code for code in codes
            if self._INDUSTRIES.get(code) in requested
        ]


class _OfflineTradingCalendar:
    """In-memory calendar matching the methods exercised by the routes."""

    def __init__(self, source: _OfflineMarketSource) -> None:
        self._source = source
        self._loaded_days = list(source._TRADING_DAYS)

    async def load(
        self,
        source: _OfflineMarketSource,
        start: str,
        end: str,
    ) -> list[str]:
        assert source is self._source
        return await source.fetch_trading_calendar(start, end)

    async def ensure_loaded(
        self,
        source: _OfflineMarketSource,
        date: str,
    ) -> None:
        assert source is self._source
        assert date

    def is_trading_day(self, date: str) -> bool:
        return date in self._loaded_days

    def next_trading_day(self, date: str) -> str | None:
        return next((day for day in self._loaded_days if day > date), None)

    def prev_trading_day(self, date: str) -> str | None:
        return next(
            (day for day in reversed(self._loaded_days) if day < date),
            None,
        )


class _OfflineDataCache:
    async def get_cache_info(self, pool_id: str) -> dict:
        return {
            "pool_id": pool_id,
            "exists": False,
            "source": "offline:test-fixture",
        }


class _OfflineDataServices:
    """Pre-wired data services that cannot initialize a network adapter."""

    def __init__(self) -> None:
        source = _OfflineMarketSource()
        self.source = source
        self.cache = _OfflineDataCache()
        self.calendar = _OfflineTradingCalendar(source)
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


@pytest.fixture(scope="module", autouse=True)
def app_lifespan(tmp_path_factory):
    """Run the API exclusively against module-scoped temporary state.

    The application resolves most storage paths through the process-wide
    ``settings`` object during startup.  Patch every writable path before
    importing the FastAPI app so this integration suite can never initialize,
    migrate, or enqueue work in the developer's real databases.
    """
    global app, client, _test_runtime_root

    runtime_root = tmp_path_factory.mktemp("api-alignment")
    patcher = pytest.MonkeyPatch()
    path_settings = {
        "DATABASE_DIR": runtime_root,
        "USERS_DB": runtime_root / "users.db",
        "EXPERIMENT_DB": runtime_root / "experiment.db",
        "TRADING_SIM_DB": runtime_root / "trading_sim.db",
        "TRADING_LIVE_DB": runtime_root / "trading_live.db",
        "DATA_CACHE_DIR": runtime_root / "cache",
        "MODEL_STORE_DIR": runtime_root / "models",
    }
    optional_path_settings = {
        "RESEARCH_SNAPSHOT_DIR": runtime_root / "research_snapshots",
    }
    path_settings.update(
        {
            name: path
            for name, path in optional_path_settings.items()
            if hasattr(settings, name)
        }
    )
    for name, path in path_settings.items():
        patcher.setattr(settings, name, str(path))
    patcher.setattr(settings, "ENVIRONMENT", "test")
    patcher.setattr(
        settings,
        "JWT_SECRET",
        "api-alignment-test-secret-" + ("s" * 48),
    )
    patcher.setattr(settings, "PAPER_SIMULATION_AUTO_RUN", False)
    patcher.setattr(settings, "PAPER_SIMULATION_REFRESH_DATA", False)

    from backend.api import data as data_api
    from backend.data.sources import validated as validated_sources
    from backend.jobs import broker as broker_module
    from backend.jobs.broker import JobBroker
    from backend.main import app as test_app

    def reject_live_market_source():
        raise AssertionError(
            "API alignment tests must not initialize a live market-data source"
        )

    patcher.setattr(
        validated_sources,
        "build_public_research_source",
        reject_live_market_source,
    )
    patcher.setattr(data_api, "_data_svc", _OfflineDataServices())
    patcher.setattr(
        broker_module,
        "_broker_instance",
        JobBroker(str(runtime_root / "jobs.db")),
    )

    app = test_app
    client = TestClient(test_app)
    _test_runtime_root = runtime_root
    try:
        with client:
            # The first registered account is the bootstrap administrator.
            _register_user(ADMIN_USER)
            yield
    finally:
        client = None
        app = None
        _test_runtime_root = None
        patcher.undo()


# ── Test data ────────────────────────────────────────────────────────────────

REGULAR_USER = {"username": "reg_test_user", "password": "test123456", "display_name": "Regular"}
ADMIN_USER = {"username": "adm_test_user", "password": "admin123456", "display_name": "Admin"}

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

def _register_user(user_data: dict) -> tuple[str, dict]:
    """Register a user and return (token, data)."""
    resp = client.post("/api/auth/register", json=user_data)
    if resp.status_code == 409:
        resp = client.post("/api/auth/login", json={
            "username": user_data["username"],
            "password": user_data["password"],
        })
    assert resp.status_code in (200, 201), f"Register/login failed: {resp.text}"
    data = resp.json().get("data", {})
    token = data.get("access_token") or data.get("token", "")
    assert token, f"No token in response: {data}"
    return token, data

# ══════════════════════════════════════════════════════════════════════════════
# Module-level fixtures (ordered: admin -> scan -> regular user)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def admin_token():
    """Admin user token (must be registered FIRST to become admin)."""
    token, data = _register_user(ADMIN_USER)
    return token

@pytest.fixture(scope="module")
def setup_strategies(admin_token):
    """Ensure strategies are scanned using admin token."""
    resp = client.post("/api/strategies/scan", headers=_auth_headers(admin_token))
    # May fail if already scanned (strategies exist), that's OK
    if resp.status_code == 200:
        print(f"  Strategies scanned: {resp.json()['data']}")
    elif resp.status_code == 403:
        print("  WARNING: Admin user lacks strategies:scan permission")
    return True

@pytest.fixture(scope="module")
def auth_token():
    """Regular user token with basic read permissions."""
    token, data = _register_user(REGULAR_USER)
    return token

# ══════════════════════════════════════════════════════════════════════════════
# 1. Health Check
# ══════════════════════════════════════════════════════════════════════════════

class TestHealth:
    def test_health_endpoint(self):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == APP_VERSION
        assert data["commit"] == "unknown" or re.fullmatch(
            r"[0-9a-f]{40}",
            data["commit"],
        )
        assert data["started_at"].endswith("+00:00")

    def test_all_writable_storage_is_temporary(self):
        runtime_root = Path(_test_runtime_root).resolve()
        for name in (
            "DATABASE_DIR",
            "USERS_DB",
            "EXPERIMENT_DB",
            "TRADING_SIM_DB",
            "TRADING_LIVE_DB",
            "DATA_CACHE_DIR",
            "MODEL_STORE_DIR",
            "RESEARCH_SNAPSHOT_DIR",
        ):
            if not hasattr(settings, name):
                continue
            resolved = settings.abs_path(getattr(settings, name)).resolve()
            assert resolved.is_relative_to(runtime_root), (name, resolved, runtime_root)

# ══════════════════════════════════════════════════════════════════════════════
# 2. Auth API
# ══════════════════════════════════════════════════════════════════════════════

class TestAuthAPI:
    def test_register_validation(self):
        resp = client.post("/api/auth/register", json={"username": "ab", "password": "short"})
        # FastAPI/Pydantic request-body validation uses the standard 422 response.
        assert resp.status_code == 422

    def test_login_success(self, auth_token):
        resp = client.post("/api/auth/login", json={
            "username": REGULAR_USER["username"],
            "password": REGULAR_USER["password"],
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["username"] == REGULAR_USER["username"]

    def test_login_wrong_password(self):
        resp = client.post("/api/auth/login", json={
            "username": REGULAR_USER["username"],
            "password": "wrong_password_123",
        })
        assert resp.status_code == 401

    def test_get_me(self, auth_token):
        resp = client.get("/api/auth/me", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["username"] == REGULAR_USER["username"]
        assert "id" in data
        assert "permissions" in data

    def test_get_me_unauthorized(self):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_refresh_token(self, auth_token):
        resp = client.post("/api/auth/login", json={
            "username": REGULAR_USER["username"],
            "password": REGULAR_USER["password"],
        })
        refresh_token = resp.json()["data"]["refresh_token"]
        resp = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "access_token" in data
        assert "refresh_token" in data

# ══════════════════════════════════════════════════════════════════════════════
# 3. Strategies API
# ══════════════════════════════════════════════════════════════════════════════

class TestStrategiesAPI:
    def test_list_strategies(self, setup_strategies, auth_token):
        resp = client.get("/api/strategies", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json().get("data", [])
        assert isinstance(data, list)
        if data:
            ids = [s["strategy_id"] for s in data]
            print(f"  Registered strategies: {ids}")

    def test_list_strategies_by_category(self, setup_strategies, auth_token):
        resp = client.get("/api/strategies?category=technical", headers=_auth_headers(auth_token))
        assert resp.status_code in (200, 422)

    def test_list_strategies_unauthenticated(self):
        resp = client.get("/api/strategies")
        assert resp.status_code == 401

    def test_get_strategy_detail(self, setup_strategies, auth_token):
        """Try known strategy IDs."""
        for sid in ["ma_cross_v1", "macd_v1", "bollinger_v1", "rsi_v1"]:
            resp = client.get(f"/api/strategies/{sid}", headers=_auth_headers(auth_token))
            if resp.status_code == 200:
                data = resp.json()["data"]
                assert data["strategy_id"] == sid
                return
        # If none found, skip gracefully
        pytest.skip("No known strategy found (strategies not registered)")

    def test_get_strategy_detail_not_found(self, auth_token):
        resp = client.get("/api/strategies/nonexistent_strat", headers=_auth_headers(auth_token))
        assert resp.status_code == 404

    def test_validate_params(self, setup_strategies, auth_token):
        for sid in ["ma_cross_v1", "macd_v1"]:
            resp = client.post(
                f"/api/strategies/{sid}/validate",
                json={"params": {"fast_period": 5, "slow_period": 20}},
                headers=_auth_headers(auth_token),
            )
            if resp.status_code == 200:
                return
        pytest.skip("No strategy found for validation test")

    def test_scan_strategies(self, setup_strategies, admin_token):
        resp = client.post("/api/strategies/scan", headers=_auth_headers(admin_token))
        if resp.status_code == 403:
            pytest.skip("Admin user lacks strategies:scan permission")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "before" in data
        assert "after" in data

    def test_sub_strategies(self, setup_strategies, auth_token):
        for sid in ["ma_cross_v1", "macd_v1"]:
            resp = client.get(f"/api/strategies/{sid}/sub-strategies", headers=_auth_headers(auth_token))
            if resp.status_code == 200:
                return
        pytest.skip("No strategy found for sub-strategies test")

    def test_best_experiments(self, setup_strategies, auth_token):
        for sid in ["ma_cross_v1", "macd_v1"]:
            resp = client.get(f"/api/strategies/{sid}/best-experiments?limit=5", headers=_auth_headers(auth_token))
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                assert isinstance(data, list)
                return
        pytest.skip("No strategy found for best-experiments test")

# ══════════════════════════════════════════════════════════════════════════════
# 4. Experiments API
# ══════════════════════════════════════════════════════════════════════════════

class TestExperimentsAPI:
    def test_list_experiments(self, auth_token):
        resp = client.get("/api/experiments", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "items" in data
        assert "total" in data
        assert "page" in data
        assert "limit" in data

    def test_create_experiment_forbidden(self, auth_token):
        resp = client.post(
            "/api/experiments",
            json={
                "name": "Test", "strategy_id": "ma_cross_v1",
                "pool_preset": "csi500", "test_start": "2024-01-01",
                "test_end": "2024-06-30", "params": {}, "mode": "batch",
            },
            headers=_auth_headers(auth_token),
        )
        assert resp.status_code == 403

    def test_create_experiment_fails_closed_without_pit_runtime(
        self,
        setup_strategies,
        admin_token,
    ):
        # Find a strategy to use
        strat_id = "ma_cross_v1"
        resp = client.get(f"/api/strategies/{strat_id}", headers=_auth_headers(admin_token))
        if resp.status_code != 200:
            # Try finding one from the list
            resp2 = client.get("/api/strategies", headers=_auth_headers(admin_token))
            strategies = resp2.json().get("data", [])
            if not strategies:
                pytest.skip("No strategies registered")
            strat_id = strategies[0]["strategy_id"]

        resp = client.post(
            "/api/experiments",
            json={
                "name": "Test API Experiment",
                "strategy_id": strat_id,
                "pool_preset": "csi500",
                "test_start": "2024-01-01",
                "test_end": "2024-06-30",
                "params": {},
                "mode": "batch",
            },
            headers=_auth_headers(admin_token),
        )
        if resp.status_code == 403:
            pytest.skip("Admin user lacks experiments:create permission")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "price_cache_unavailable"
        assert detail["data_policy"] == "pit_cache_only"
        assert detail["retryable_after_governance_activation"] is True

    def test_get_experiment_detail_not_found(self, auth_token):
        resp = client.get("/api/experiments/999999", headers=_auth_headers(auth_token))
        assert resp.status_code == 404

    def test_get_experiment_metrics(self, admin_token):
        exp_id = getattr(getattr(pytest, 'state', None), 'experiment_id', None)
        if not exp_id:
            pytest.skip("No experiment created yet")
        resp = client.get(f"/api/experiments/{exp_id}/metrics", headers=_auth_headers(admin_token))
        assert resp.status_code in (200,)

    def test_get_equity_curve(self, admin_token):
        exp_id = getattr(getattr(pytest, 'state', None), 'experiment_id', None)
        if not exp_id:
            pytest.skip("No experiment created yet")
        resp = client.get(f"/api/experiments/{exp_id}/equity", headers=_auth_headers(admin_token))
        assert resp.status_code == 200
        data = resp.json().get("data", [])
        assert isinstance(data, list)

    def test_get_trades(self, admin_token):
        exp_id = getattr(getattr(pytest, 'state', None), 'experiment_id', None)
        if not exp_id:
            pytest.skip("No experiment created yet")
        resp = client.get(f"/api/experiments/{exp_id}/trades?page=1&limit=10", headers=_auth_headers(admin_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "items" in data

    def test_experiment_picker(self, auth_token):
        resp = client.get("/api/experiments/picker?limit=5", headers=_auth_headers(auth_token))
        # Even if no completed experiments, should return 200 with empty list
        assert resp.status_code in (200,)

# ══════════════════════════════════════════════════════════════════════════════
# 5. Data API
# ══════════════════════════════════════════════════════════════════════════════

class TestDataAPI:
    _READINESS_BODY = {
        "data_access_policy": "cache_only",
        "pool_preset": "custom",
        "pool_custom_codes": ["000001", "600000"],
        "test_start": "2024-01-01",
        "test_end": "2024-06-30",
    }

    def test_experiment_readiness_requires_data_read(self):
        resp = client.post(
            "/api/data/experiment-readiness",
            json=self._READINESS_BODY,
        )
        assert resp.status_code == 401

    def test_experiment_readiness_is_read_only_and_strict(
        self,
        auth_token,
    ):
        experiment_db = Path(settings.EXPERIMENT_DB)
        before = (
            experiment_db.stat().st_size,
            experiment_db.stat().st_mtime_ns,
        )
        resp = client.post(
            "/api/data/experiment-readiness",
            json=self._READINESS_BODY,
            headers=_auth_headers(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_version"] == "experiment-readiness/v4"
        assert data["data_access_policy"] == "pit_cache_only"
        assert data["network_accessed"] is False
        assert data["writes_performed"] is False
        assert data["legacy_or_static_fallback_allowed"] is False
        assert isinstance(data["checks"], list)
        assert isinstance(data["blockers"], list)
        assert data["evidence"]["eligible_for_live_trading"] is False
        assert data["ready"] is False
        assert data["blockers"]
        assert (
            experiment_db.stat().st_size,
            experiment_db.stat().st_mtime_ns,
        ) == before
        assert set(data) == {
            "schema_version",
            "ready",
            "data_access_policy",
            "network_accessed",
            "writes_performed",
            "legacy_or_static_fallback_allowed",
            "price_purpose",
            "requested_purpose",
            "effective_gate",
            "checks",
            "blockers",
            "evidence",
            "market_data",
            "benchmark",
        }
        assert "path" not in str(data).lower()

        invalid = client.post(
            "/api/data/experiment-readiness",
            json={**self._READINESS_BODY, "allow_fetch": True},
            headers=_auth_headers(auth_token),
        )
        assert invalid.status_code == 422

    def test_refresh_industries_requires_update_permission(self, auth_token):
        resp = client.post(
            "/api/data/industries/refresh?pool_id=csi500",
            headers=_auth_headers(auth_token),
        )
        assert resp.status_code == 403

    def test_list_pools(self, auth_token):
        resp = client.get("/api/data/pools", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert isinstance(data, list)
        assert {pool["id"] for pool in data} == {
            "csi300",
            "csi500",
            "csi800",
            "csi1000",
        }
        assert all(pool["availability"]["ready"] is False for pool in data)
        assert all(pool["lineage"]["point_in_time"] is False for pool in data)
        assert all(pool["availability"]["network_accessed"] is False for pool in data)

    def test_get_pool_detail(self, auth_token):
        resp = client.get("/api/data/pools/csi500", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["pool_id"] == "csi500"
        assert data["count"] == 0
        assert data["declared_count"] == 500
        assert data["industries"] == []
        assert data["availability"]["ready"] is False
        assert data["availability"]["network_accessed"] is False
        assert data["lineage"]["point_in_time"] is False
        assert data["quality"] == {
            "ready": False,
            "expected_count": 500,
            "unique_count": 0,
        }
        assert data["risk_warnings"]

    def test_get_pool_stocks(self, auth_token):
        resp = client.get("/api/data/pools/csi500/stocks", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["pool_id"] == "csi500"
        assert data["stocks"] == []
        assert data["count"] == 0
        assert data["availability"]["ready"] is False
        assert data["availability"]["network_accessed"] is False
        assert data["lineage"]["point_in_time"] is False

    def test_get_industries(self, admin_token):
        refresh = client.post(
            "/api/data/industries/refresh?classification=cninfo_008001"
            "&pool_id=csi500",
            headers=_auth_headers(admin_token),
        )
        assert refresh.status_code == 409
        detail = refresh.json()["detail"]
        assert detail["code"] == "point_in_time_pool_unavailable"
        assert detail["availability"]["ready"] is False
        resp = client.get(
            "/api/data/industries?classification=cninfo_008001&pool_id=csi500",
            headers=_auth_headers(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_version"] == "industry-catalog/v2"
        assert data["classification"] == "cninfo_008001"
        assert data["industries"] == []
        assert data["count"] == 0
        assert data["filterable"] is False
        assert data["reason"] == "industry_cache_missing_stale_or_invalid"
        assert data["map_coverage"] == 0.0

    def test_custom_code_industry_readiness_is_exactly_scoped(self, admin_token):
        resp = client.post(
            "/api/data/industries/readiness",
            json={"codes": ["000001.SZ", "600000.SH"]},
            headers=_auth_headers(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["filterable"] is False
        assert data["reason"] == "industry_cache_missing_stale_or_invalid"
        assert data["industries"] == []
        assert data["count"] == 0

    def test_custom_code_industry_readiness_rejects_ambiguous_codes(
        self,
        admin_token,
    ):
        resp = client.post(
            "/api/data/industries/readiness",
            json={"codes": ["SZ000001"]},
            headers=_auth_headers(admin_token),
        )
        assert resp.status_code == 422
        assert "无效股票代码" in resp.text

    def test_get_stock_data(self, auth_token):
        resp = client.get("/api/data/stocks/000001?start=2024-01-01&end=2024-06-30", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["code"] == "000001"
        assert data["source"] == "live_fetch"
        assert [record["date"] for record in data["records"]] == [
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-06-28",
        ]
        assert data["records"][0] == {
            "date": "2024-01-02",
            "open": 10.0,
            "close": 10.2,
            "high": 10.4,
            "low": 9.8,
            "volume": 1_000_000.0,
            "amount": 10_000_000.0,
        }

    def test_get_calendar(self, auth_token):
        resp = client.get("/api/data/calendar?start=2024-01-01&end=2024-06-30", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data == {
            "start": "2024-01-01",
            "end": "2024-06-30",
            "count": 4,
            "trading_days": [
                "2024-01-02",
                "2024-01-03",
                "2024-01-04",
                "2024-06-28",
            ],
        }

    def test_check_trading_day(self, auth_token):
        resp = client.get("/api/data/calendar/check/2024-01-02", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data == {
            "date": "2024-01-02",
            "is_trading_day": True,
            "next_trading_day": "2024-01-03",
            "prev_trading_day": None,
        }

    def test_get_update_status(self, auth_token):
        resp = client.get("/api/data/update/status", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["pools_cache"]) == 4
        assert {item["pool_id"] for item in data["pools_cache"]} == {
            "csi300",
            "csi500",
            "csi800",
            "csi1000",
        }
        assert all(
            item["source"] == "offline:test-fixture"
            for item in data["pools_cache"]
        )
        assert data["market_data_update_contract"]["available"] is False
        assert (
            data["market_data_update_contract"]["reason"]
            == "pit_dual_price_update_not_authorized"
        )

# ══════════════════════════════════════════════════════════════════════════════
# 6. Trading API
# ══════════════════════════════════════════════════════════════════════════════

class TestTradingAPI:
    def test_list_deployments(self, auth_token):
        resp = client.get("/api/trading/deployments", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json().get("data", [])
        assert isinstance(data, list)

    def test_create_deployment_forbidden(self, auth_token):
        resp = client.post(
            "/api/trading/deployments",
            json={"strategy_id": "ma_cross_v1", "display_name": "Test",
                  "params": {}, "mode": "batch", "status": "active"},
            headers=_auth_headers(auth_token),
        )
        assert resp.status_code == 403

    def test_create_deployment(self, setup_strategies, admin_token):
        resp = client.post(
            "/api/trading/deployments",
            json={"strategy_id": "ma_cross_v1", "display_name": "Test Deployment",
                  "params": {}, "mode": "batch", "status": "paused"},
            headers=_auth_headers(admin_token),
        )
        if resp.status_code == 403:
            pytest.skip("Admin user lacks trading:deploy permission")
        assert resp.status_code == 200, f"Create deployment failed: {resp.text}"
        data = resp.json()["data"]
        assert "deployment_id" in data

    def test_list_portfolios(self, auth_token):
        resp = client.get("/api/trading/portfolios", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json().get("data", [])
        assert isinstance(data, list)

    def test_create_portfolio(self, admin_token):
        resp = client.post(
            "/api/trading/portfolios",
            json={"name": "Test Portfolio", "total_capital": 1000000,
                  "rebalance_frequency": "monthly", "allocations": []},
            headers=_auth_headers(admin_token),
        )
        if resp.status_code == 403:
            pytest.skip("Admin user lacks trading:rebalance permission")
        assert resp.status_code == 200, f"Create portfolio failed: {resp.text}"
        data = resp.json()["data"]
        assert "portfolio_id" in data

    def test_get_positions(self, auth_token):
        resp = client.get("/api/trading/positions", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json().get("data", [])
        assert isinstance(data, list)

    def test_get_signals(self, auth_token):
        resp = client.get("/api/trading/signals?limit=10", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json().get("data", [])
        assert isinstance(data, list)

    def test_get_orders(self, auth_token):
        resp = client.get("/api/trading/orders?page=1&limit=10", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        body = resp.json()
        data = body.get("data", {})
        if isinstance(data, dict):
            assert "items" in data

    def test_simulate_run_forbidden(self, auth_token):
        resp = client.post("/api/trading/simulate/run", headers=_auth_headers(auth_token))
        assert resp.status_code == 403

    def test_simulate_run(self, admin_token):
        resp = client.post("/api/trading/simulate/run", headers=_auth_headers(admin_token))
        if resp.status_code == 403:
            pytest.skip("Admin user lacks trading:execute permission")
        assert resp.status_code == 409
        assert resp.json()["detail"] == {
            "code": "pit_simulation_date_required",
            "message": "PIT-only 模拟必须显式选择已完成交易日",
        }

# ══════════════════════════════════════════════════════════════════════════════
# 7. Jobs API
# ══════════════════════════════════════════════════════════════════════════════

class TestJobsAPI:
    def test_list_jobs(self, auth_token):
        resp = client.get("/api/jobs", headers=_auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json().get("data", {})
        assert isinstance(data.get("items"), list)
        assert isinstance(data.get("total"), int)

    def test_get_job_detail_not_found(self, auth_token):
        resp = client.get("/api/jobs/nonexistent-job-id", headers=_auth_headers(auth_token))
        assert resp.status_code == 404

# ══════════════════════════════════════════════════════════════════════════════
# 8. Admin API
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminAPI:
    def test_list_permissions(self, admin_token):
        resp = client.get("/api/admin/permissions", headers=_auth_headers(admin_token))
        if resp.status_code == 403:
            pytest.skip("Admin user lacks admin:users permission")
        assert resp.status_code == 200
        data = resp.json().get("data", [])
        assert isinstance(data, list)
        if data:
            assert "key" in data[0]

    def test_list_users(self, admin_token):
        resp = client.get("/api/admin/users", headers=_auth_headers(admin_token))
        if resp.status_code == 403:
            pytest.skip("Admin user lacks admin:users permission")
        assert resp.status_code == 200
        data = resp.json().get("data", [])
        assert isinstance(data, list)

    def test_list_permissions_forbidden(self, auth_token):
        resp = client.get("/api/admin/permissions", headers=_auth_headers(auth_token))
        assert resp.status_code == 403

    def test_list_users_forbidden(self, auth_token):
        resp = client.get("/api/admin/users", headers=_auth_headers(auth_token))
        assert resp.status_code == 403

# ══════════════════════════════════════════════════════════════════════════════
# 9. AI API
# ══════════════════════════════════════════════════════════════════════════════

class TestAIAPI:
    def test_analyze_backtest_forbidden(self, auth_token):
        resp = client.post("/api/ai/analyze-backtest", json={"experiment_id": 999999},
                          headers=_auth_headers(auth_token))
        assert resp.status_code == 403

    def test_analyze_backtest_no_experiment(self, admin_token):
        resp = client.post("/api/ai/analyze-backtest", json={"experiment_id": 999999},
                          headers=_auth_headers(admin_token))
        if resp.status_code == 403:
            pytest.skip("Admin user lacks ai:use permission")
        assert resp.status_code == 404

    def test_suggest_params(self, admin_token):
        resp = client.post("/api/ai/suggest-params",
                          json={"strategy_id": "ma_cross_v1", "current_params": {}},
                          headers=_auth_headers(admin_token))
        if resp.status_code == 403:
            pytest.skip("Admin user lacks ai:use permission")
        assert resp.status_code in (200, 404, 503)

    def test_diagnose_error(self, admin_token):
        resp = client.post("/api/ai/diagnose-error",
                          json={"experiment_id": 999999, "error_log": "test"},
                          headers=_auth_headers(admin_token))
        if resp.status_code == 403:
            pytest.skip("Admin user lacks ai:use permission")
        assert resp.status_code in (200, 404, 503)

# ══════════════════════════════════════════════════════════════════════════════
# 10. WebSocket Routes
# ══════════════════════════════════════════════════════════════════════════════

class TestWebSocket:
    def test_ws_routes_registered(self):
        routes = [
            path
            for route in app.routes
            if (path := getattr(route, "path", None)) is not None
        ]
        assert "/ws/notifications" in routes
        assert "/ws/training/{experiment_id}" in routes
        assert "/ws/realtime/{deployment_id}" in routes

# ══════════════════════════════════════════════════════════════════════════════
# 11. Response Format Consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestResponseFormat:
    def test_health_returns_status(self):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert "status" in resp.json()

    def test_auth_has_data(self, auth_token):
        resp = client.get("/api/auth/me", headers=_auth_headers(auth_token))
        assert "data" in resp.json()

    def test_strategies_has_data(self, setup_strategies, auth_token):
        resp = client.get("/api/strategies", headers=_auth_headers(auth_token))
        assert "data" in resp.json()

    def test_experiments_has_data(self, auth_token):
        resp = client.get("/api/experiments", headers=_auth_headers(auth_token))
        assert "data" in resp.json()

    def test_trading_has_data(self, auth_token):
        resp = client.get("/api/trading/deployments", headers=_auth_headers(auth_token))
        assert "data" in resp.json()

    def test_jobs_has_data(self, auth_token):
        resp = client.get("/api/jobs", headers=_auth_headers(auth_token))
        assert "data" in resp.json()

    def test_data_has_data(self, auth_token):
        resp = client.get("/api/data/pools", headers=_auth_headers(auth_token))
        assert "data" in resp.json()

# ══════════════════════════════════════════════════════════════════════════════
# 12. End-to-End Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestEndToEndFlow:
    def _ensure_strategies(self, token):
        """Helper to ensure strategies are available."""
        resp = client.get("/api/strategies", headers=_auth_headers(token))
        if resp.status_code == 200 and resp.json().get("data"):
            return resp.json()["data"]
        # Try scanning
        resp = client.post("/api/strategies/scan", headers=_auth_headers(token))
        if resp.status_code == 200:
            resp = client.get("/api/strategies", headers=_auth_headers(token))
            return resp.json().get("data", [])
        return []

    def test_full_user_flow(self):
        suffix = uuid.uuid4().hex[:8]
        username = f"e2e_{suffix}"
        password = "e2e_test_pass_123"

        resp = client.post("/api/auth/register", json={
            "username": username, "password": password, "display_name": "E2E",
        })
        if resp.status_code == 409:
            resp = client.post("/api/auth/login", json={
                "username": username, "password": password,
            })
        assert resp.status_code in (200, 201), f"Register/login failed: {resp.text}"
        data = resp.json().get("data", {})
        token = data.get("access_token", "")
        assert token, f"No token: {data}"
        headers = _auth_headers(token)

        # Get /me
        resp = client.get("/api/auth/me", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["username"] == username

        # List strategies (may be empty)
        resp = client.get("/api/strategies", headers=headers)
        assert resp.status_code == 200

        # List experiments
        resp = client.get("/api/experiments", headers=headers)
        assert resp.status_code == 200
        assert "items" in resp.json()["data"]

        # List data pools
        resp = client.get("/api/data/pools", headers=headers)
        assert resp.status_code == 200

        # List deployments
        resp = client.get("/api/trading/deployments", headers=headers)
        assert resp.status_code == 200

        # List portfolios
        resp = client.get("/api/trading/portfolios", headers=headers)
        assert resp.status_code == 200

        # List jobs
        resp = client.get("/api/jobs", headers=headers)
        assert resp.status_code == 200

        print(f"\n  E2E flow passed for user: {username}")

# ══════════════════════════════════════════════════════════════════════════════
# 13. Backend Core Module Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCoreModules:
    def test_cost_model(self):
        from backend.core.cost_model import CostModel
        cm = CostModel(commission_rate=0.001, slippage_rate=0.001, stamp_duty_rate=0.001)
        assert cm.calc_buy_cost(10, 1000) > 0
        assert cm.calc_sell_cost(10, 1000) > 0
        shares = cm.calc_shares(10000, 10)
        assert shares % 100 == 0

    def test_round_lot(self):
        from backend.core.rules import round_lot
        assert round_lot(150) == 100
        assert round_lot(99) == 0
        assert round_lot(200) == 200

    def test_jwt_handler(self):
        from backend.auth.jwt_handler import create_access_token, decode_token
        token = create_access_token(1, "testuser", ["experiments:read"])
        assert token
        payload = decode_token(token)
        assert payload["sub"] == "1"
        assert payload["username"] == "testuser"
        assert decode_token("invalid-token") is None

    def test_signal_types(self):
        from backend.core.types import SignalItem, TradeRecord
        s = SignalItem(code="000001.SZ", action="BUY", score=0.85, weight=0.5)
        assert s.code == "000001.SZ"
        t = TradeRecord(date="2024-01-15", code="000001.SZ", action="BUY",
                        price=10.0, shares=1000, amount=10000.0, cost=20.0)
        assert t.shares == 1000

    def test_ma_cross_metadata(self):
        from backend.strategies.technical.ma_cross import MACrossStrategy
        meta = MACrossStrategy().metadata()
        assert meta.strategy_id == "ma_cross_v1"
        assert meta.category.value == "technical"

    def test_metrics_computation(self):
        import numpy as np
        import pandas as pd
        from backend.core.metrics import compute_all_metrics
        dates = pd.date_range("2024-01-01", periods=252, freq="B")
        np.random.seed(42)
        returns = np.random.randn(252) * 0.01 + 0.0005
        equity = 1000000 * (1 + returns).cumprod()
        equity_curve = pd.DataFrame({"equity": equity}, index=dates)
        metrics = compute_all_metrics(equity_curve, None, [])
        assert "sharpe_ratio" in metrics
        assert "max_drawdown" in metrics

    def test_backtest_engine(self):
        import numpy as np
        import pandas as pd
        from backend.core.engine import BacktestEngine
        from backend.core.cost_model import CostModel
        from backend.strategies.technical.ma_cross import MACrossStrategy

        dates = pd.date_range("2024-01-01", periods=80, freq="B")
        np.random.seed(42)
        prices = {
            "000001.SZ": pd.Series(100 + np.random.randn(80).cumsum() * 0.5, index=dates),
            "000002.SZ": pd.Series(50 + np.random.randn(80).cumsum() * 0.3, index=dates),
        }
        dfs = {}
        for code, series in prices.items():
            dfs[(code, "close")] = series
        pivot = pd.DataFrame(dfs, index=dates)
        pivot.columns = pd.MultiIndex.from_tuples(pivot.columns)

        signals = MACrossStrategy().generate_batch_signals(
            pivot, {"fast_period": 5, "slow_period": 20, "min_score": 0.0},
            "2024-01-15", "2024-04-15",
        ) or {"2024-01-20": []}

        cm = CostModel()
        engine = BacktestEngine(initial_capital=1000000, cost_model=cm,
                                start_date="2024-01-15", end_date="2024-04-15", max_positions=20)
        result = engine.run(signals, pivot, strategy_id="ma_cross_v1")
        assert result.final_equity > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
