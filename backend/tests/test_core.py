"""核心模块单元测试 —— 量化验证平台."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import pandas as pd
import numpy as np


def test_cost_model():
    from backend.core.cost_model import CostModel

    cm = CostModel(commission_rate=0.001, slippage_rate=0.001, stamp_duty_rate=0.001)
    # 买入 1000 股 × 10元
    buy_cost = cm.calc_buy_cost(10, 1000)
    print(f"Buy cost(10, 1000): {buy_cost:.2f}")
    # 卖出 1000 股 × 10元
    sell_cost = cm.calc_sell_cost(10, 1000)
    print(f"Sell cost(10, 1000): {sell_cost:.2f}")
    # 整手取整
    shares = cm.calc_shares(10000, 10)
    print(f"Shares from 10000 capital at 10: {shares}")
    assert shares % 100 == 0
    assert shares >= 0
    print("PASS: cost_model")


def test_round_lot():
    from backend.core.rules import round_lot

    assert round_lot(150) == 100
    assert round_lot(99) == 0
    assert round_lot(200) == 200
    assert round_lot(0) == 0
    assert round_lot(-5) == 0
    print("PASS: round_lot")


def test_ma_cross_metadata():
    from backend.strategies.technical.ma_cross import MACrossStrategy

    strat = MACrossStrategy()
    meta = strat.metadata()
    assert meta.strategy_id == "ma_cross_v1"
    print(f"Strategy: {meta.display_name}")
    print(f"Description: {meta.description[:100]}...")
    assert meta.category.value == "technical"
    print("PASS: ma_cross metadata")


def _make_multiindex_pivot(dates, codes, prices_dict):
    """Helper: create MultiIndex pivot DataFrame (code, 'close') format."""
    dfs = {}
    for code in codes:
        series = prices_dict.get(code)
        if series is not None:
            dfs[(code, "close")] = series
    df = pd.DataFrame(dfs, index=dates)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_ma_cross_signals():
    """用 MultiIndex 数据测试 MA 交叉信号生成."""
    from backend.strategies.technical.ma_cross import MACrossStrategy

    strat = MACrossStrategy()
    # 制造一个上升趋势 + 金叉
    dates = pd.date_range("2024-01-01", periods=120, freq="B")
    np.random.seed(42)
    # 股票价格：前60天震荡，后60天持续上涨
    prices = np.concatenate(
        [
            100 + np.random.randn(60).cumsum() * 0.5,
            100 + np.arange(60) * 0.5 + np.random.randn(60) * 0.5,
        ]
    )
    pivot = _make_multiindex_pivot(dates, ["000001.SZ"], {"000001.SZ": pd.Series(prices, index=dates)})

    # 用宽松参数获取更多信号（min_score=0 确保即使弱信号也能捕获）
    signals = strat.generate_batch_signals(
        pivot,
        {"fast_period": 5, "slow_period": 25, "min_score": 0.0},
        "2024-01-15",
        "2024-06-30",
    )
    buy_count = sum(1 for sigs in signals.values() for s in sigs if s.action == "BUY")
    sell_count = sum(1 for sigs in signals.values() for s in sigs if s.action == "SELL")
    total = buy_count + sell_count
    print(f"Signals: {len(signals)} dates, {buy_count} BUY, {sell_count} SELL, {total} total")
    assert total > 0, f"Expected some signals, got {total}"
    print("PASS: ma_cross signals")


def test_backtest_engine():
    from backend.core.engine import BacktestEngine
    from backend.core.cost_model import CostModel
    from backend.strategies.technical.ma_cross import MACrossStrategy

    # 创建模拟 MultiIndex 数据
    dates = pd.date_range("2024-01-01", periods=80, freq="B")
    np.random.seed(42)
    prices = {
        "000001.SZ": pd.Series(100 + np.random.randn(80).cumsum() * 0.5, index=dates),
        "000002.SZ": pd.Series(50 + np.random.randn(80).cumsum() * 0.3, index=dates),
        "000003.SZ": pd.Series(200 + np.random.randn(80).cumsum() * 1.0, index=dates),
    }
    pivot = _make_multiindex_pivot(dates, list(prices.keys()), prices)

    # 生成信号（用 min_score=0 确保有信号）
    strat = MACrossStrategy()
    signals = strat.generate_batch_signals(
        pivot,
        {"fast_period": 5, "slow_period": 20, "min_score": 0.0},
        "2024-01-15",
        "2024-04-15",
    )
    print(f"Signals generated: {len(signals)} date-keys")
    # 如果无信号，用空字典也能跑
    if not signals:
        signals = {"2024-01-20": []}

    cm = CostModel()
    engine = BacktestEngine(
        initial_capital=1000000,
        cost_model=cm,
        start_date="2024-01-15",
        end_date="2024-04-15",
        max_positions=20,
    )
    result = engine.run(signals, pivot, strategy_id="ma_cross_v1")

    print(f"Final equity: {result.final_equity:,.0f}")
    print(f"Total return: {(result.final_equity / 1000000 - 1) * 100:.2f}%")
    print(f"Total trades: {len(result.trade_log)}")
    print(f"Equity points: {len(result.equity_curve)}")
    assert result.final_equity > 0
    print("PASS: backtest engine")


def test_backtest_executes_t_plus_one_at_open():
    from backend.core.cost_model import CostModel
    from backend.core.engine import BacktestEngine
    from backend.core.types import SignalItem

    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    pivot = pd.DataFrame(
        {
            ("000001.SZ", "open"): [10.0, 11.0, 12.0],
            ("000001.SZ", "close"): [10.5, 11.5, 12.5],
        },
        index=dates,
    )
    pivot.columns = pd.MultiIndex.from_tuples(pivot.columns)
    signals = {
        "2024-01-02": [
            SignalItem("000001.SZ", "BUY", score=1.0, weight=0.5)
        ],
        "2024-01-03": [
            SignalItem("000001.SZ", "SELL", score=1.0, weight=0.0)
        ],
    }
    engine = BacktestEngine(
        initial_capital=100_000,
        cost_model=CostModel(),
        start_date="2024-01-02",
        end_date="2024-01-04",
    )

    result = engine.run(signals, pivot, strategy_id="contract_test")

    assert [trade.date for trade in result.trade_log] == [
        "2024-01-03",
        "2024-01-04",
    ]
    assert [trade.signal_date for trade in result.trade_log] == [
        "2024-01-02",
        "2024-01-03",
    ]
    assert [trade.price for trade in result.trade_log] == [11.0, 12.0]


def test_backtest_never_uses_close_as_open_fallback():
    from backend.core.cost_model import CostModel
    from backend.core.engine import BacktestEngine
    from backend.core.types import SignalItem

    dates = pd.date_range("2024-01-02", periods=2, freq="B")
    legacy_close_only = pd.DataFrame(
        {"000001.SZ": [10.0, 99.0]},
        index=dates,
    )
    engine = BacktestEngine(
        initial_capital=100_000,
        cost_model=CostModel(),
        start_date="2024-01-02",
        end_date="2024-01-03",
    )

    result = engine.run(
        {"2024-01-02": [SignalItem("000001.SZ", "BUY", 1.0, 1.0)]},
        legacy_close_only,
    )

    assert result.trade_log == []


def test_missing_close_does_not_poison_portfolio_equity():
    from backend.core.cost_model import CostModel
    from backend.core.engine import BacktestEngine
    from backend.core.types import SignalItem

    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    pivot = pd.DataFrame(
        {
            ("000001.SZ", "open"): [10.0, 10.0, 10.0],
            ("000001.SZ", "close"): [10.0, 10.0, np.nan],
        },
        index=dates,
    )
    pivot.columns = pd.MultiIndex.from_tuples(pivot.columns)
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=CostModel(),
        start_date="2024-01-02",
        end_date="2024-01-04",
    ).run(
        {"2024-01-02": [SignalItem("000001.SZ", "BUY", 1.0, 0.5)]},
        pivot,
    )

    assert np.isfinite(result.final_equity)
    assert result.final_equity > 99_000


def test_metrics():
    from backend.core.metrics import compute_all_metrics

    # 创建模拟净值曲线
    dates = pd.date_range("2024-01-01", periods=252, freq="B")
    np.random.seed(42)
    returns = np.random.randn(252) * 0.01 + 0.0005
    equity = 1000000 * (1 + returns).cumprod()
    benchmark = 1000000 * (1 + np.random.randn(252) * 0.008 + 0.0003).cumprod()

    equity_curve = pd.DataFrame({"equity": equity}, index=dates)
    bench_curve = pd.Series(benchmark, index=dates)

    metrics = compute_all_metrics(equity_curve, bench_curve, [])
    print(f"Sharpe: {metrics.get('sharpe_ratio', 'N/A')}")
    print(f"MaxDD: {metrics.get('max_drawdown', 'N/A')}")
    print(f"Annualized Return: {metrics.get('annualized_return', 'N/A')}")
    assert "sharpe_ratio" in metrics
    assert "max_drawdown" in metrics
    assert "annualized_return" in metrics
    print("PASS: metrics computation")


def test_partial_benchmark_does_not_change_absolute_strategy_metrics():
    from backend.core.metrics import compute_all_metrics

    dates = pd.bdate_range("2024-01-01", periods=12)
    equity = pd.DataFrame(
        {"equity": 1_000_000 * (1.01 ** np.arange(len(dates)))},
        index=dates,
    )
    partial_benchmark = pd.Series(
        [1_000_000, 1_002_000, 1_001_000],
        index=dates[-3:],
    )

    without_benchmark = compute_all_metrics(equity)
    with_partial_benchmark = compute_all_metrics(equity, partial_benchmark)

    for key in (
        "annualized_return",
        "sharpe_ratio",
        "annualized_volatility",
        "n_days",
        "n_years",
    ):
        assert with_partial_benchmark[key] == without_benchmark[key]
    assert with_partial_benchmark["information_ratio"] is None
    assert with_partial_benchmark["alpha"] is None
    assert with_partial_benchmark["beta"] is None


def test_jwt():
    from backend.auth.jwt_handler import create_access_token, decode_token
    from backend.config import settings

    original_secret = settings.JWT_SECRET
    settings.JWT_SECRET = "test-jwt-secret-" + ("s" * 48)
    try:
        token = create_access_token(1, "testuser", ["experiments:read"])
        print(f"Token: {token[:50]}...")
        payload = decode_token(token)
        # JWT stores user_id in 'sub' field (standard claim)
        assert payload["sub"] == "1"
        assert payload["username"] == "testuser"
        assert payload["permissions"] == ["experiments:read"]
        # Test invalid token
        assert decode_token("invalid-token") is None
        print("PASS: JWT create/decode")
    finally:
        settings.JWT_SECRET = original_secret


def test_rbac():
    from backend.auth.permissions import Permission, ROLE_PERMISSIONS, has_permission

    user = {"permissions": ["experiments:read", "strategies:read"]}
    assert has_permission(user, Permission.EXP_READ) == True
    assert has_permission(user, Permission.EXP_CREATE) == False
    assert has_permission(user, Permission.EXP_DELETE) == False
    # Test admin
    admin = {"is_admin": True, "permissions": []}
    assert has_permission(admin, Permission.EXP_DELETE) == True
    assert has_permission(admin, Permission.ADMIN_USERS) == True
    # Test role permissions
    assert "experiments:read" in ROLE_PERMISSIONS["viewer"]
    assert "experiments:delete" not in ROLE_PERMISSIONS["viewer"]
    print("PASS: RBAC permissions")


def test_strategy_registry():
    from backend.strategies.registry import StrategyRegistry

    registry = StrategyRegistry()
    result = registry.scan_directory("backend/strategies")
    print(f"Scan result: {result}")
    all_strategies = registry.list_all()
    print(f"Registered strategies: {len(all_strategies)}")
    for s in all_strategies:
        print(f"  - {s.strategy_id} ({s.category})")
    assert len(all_strategies) >= 10, f"Expected >=10, got {len(all_strategies)}"
    # Test get by ID
    s = registry.get_strategy("ma_cross_v1")
    assert s is not None
    # Test parameter validation
    valid, msg = registry.validate_params("ma_cross_v1", {"fast_period": 5, "slow_period": 20})
    assert valid, f"Expected valid, got: {msg}"
    valid2, msg2 = registry.validate_params("ma_cross_v1", {"fast_period": 30, "slow_period": 10})
    assert not valid2, "Expected invalid (fast>=slow)"
    print("PASS: strategy registry")


def test_signal_types():
    from backend.core.types import SignalItem, TradeRecord, BacktestResult, PositionSnapshot

    s = SignalItem(code="000001.SZ", action="BUY", score=0.85, weight=0.5)
    assert s.code == "000001.SZ"
    assert s.action == "BUY"
    assert s.score == 0.85
    assert s.weight == 0.5

    t = TradeRecord(
        date="2024-01-15",
        code="000001.SZ",
        action="BUY",
        price=10.0,
        shares=1000,
        amount=10000.0,
        cost=20.0,
        signal_strategy="ma_cross_v1",
        signal_score=0.7,
    )
    assert t.shares == 1000

    ps = PositionSnapshot(
        date="2024-01-15",
        code="000001.SZ",
        shares=1000,
        avg_cost=9.8,
        close_price=10.0,
        market_value=10000.0,
        unrealized_pnl=200.0,
    )
    assert ps.unrealized_pnl == 200.0

    # BacktestResult construction
    br = BacktestResult(
        equity_curve=pd.DataFrame(),
        trade_log=[],
        position_snapshots=[],
        final_equity=1000000.0,
        signals_generated=5,
        trades_executed=0,
    )
    assert br.final_equity == 1000000.0
    print("PASS: signal types")


def test_rules_helpers():
    from backend.core.rules import round_lot, is_trading_day, can_buy, next_trading_day
    from backend.core.cost_model import CostModel
    from datetime import date

    # round_lot
    assert round_lot(250) == 200
    assert round_lot(0) == 0
    assert round_lot(50) == 0

    # is_trading_day (without calendar)
    assert is_trading_day(date(2024, 1, 2)) == True  # Tuesday
    assert is_trading_day(date(2024, 1, 6)) == False  # Saturday

    # next_trading_day
    nxt = next_trading_day(date(2024, 1, 5))  # Friday → Monday
    assert nxt == date(2024, 1, 8), f"Expected 2024-01-08, got {nxt}"

    # can_buy
    cm = CostModel()
    assert can_buy(20000, 10, 1000, cm) == True
    assert can_buy(1000, 10, 1000, cm) == False
    assert can_buy(0, 10, 100, cm) == False
    assert can_buy(10000, 0, 100, cm) == False
    print("PASS: rules helpers")


# ── 运行 ──
if __name__ == "__main__":
    tests = [
        test_cost_model,
        test_round_lot,
        test_ma_cross_metadata,
        test_ma_cross_signals,
        test_backtest_engine,
        test_metrics,
        test_jwt,
        test_rbac,
        test_strategy_registry,
        test_signal_types,
        test_rules_helpers,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL: {test.__name__}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1
    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
