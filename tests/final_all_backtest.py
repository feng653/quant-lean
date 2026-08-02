"""
Final run: re-run all 7 tuned strategies with best params, save to DB, generate report.
"""
import sys, os, json, hashlib, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pandas as pd, numpy as np

from backend.config import settings
from backend.core.engine import BacktestEngine
from backend.core.cost_model import CostModel
from backend.core.metrics import compute_all_metrics
from backend.strategies.registry import get_registry
import aiosqlite

pivot = pd.read_parquet(str(settings.abs_path("data/cache/daily/csi500.parquet")))
registry = get_registry()
registry.scan_directory(str(settings.PROJECT_ROOT / "backend" / "strategies"))

TSTART, TEND = "2023-07-01", "2026-06-30"

# Best params from tuning agents
STRATS = [
    {"id": "risk_parity_v1", "name": "风险平价(周调)",
     "p": {"lookback": 63, "rebalance_frequency": "weekly", "min_score": 0.0}},
    {"id": "bollinger_breakout_v1", "name": "布林带突破(40/2.5)",
     "p": {"period": 40, "std_multiplier": 2.5, "min_score": 0.0}},
    {"id": "rsi_reversal_v1", "name": "RSI均值回归(14/30-70)",
     "p": {"period": 14, "oversold": 30, "overbought": 70, "min_score": 0.0}},
    {"id": "macd_signal_v1", "name": "MACD金叉(12/26/9)",
     "p": {"fast": 12, "slow": 26, "signal": 9, "min_score": 0.0}},
    {"id": "ma_cross_v1", "name": "MA双均线(10/30)",
     "p": {"fast_period": 10, "slow_period": 30, "min_score": 0.0}},
    {"id": "transformer_rank_v1", "name": "Transformer排序",
     "p": {"seq_len": 20, "hidden_size": 16, "num_layers": 1, "nhead": 4, "epochs": 2, "top_k_pct": 0.1}},
    {"id": "lstm_rank_v1", "name": "LSTM排序",
     "p": {"seq_len": 20, "hidden_size": 16, "num_layers": 1, "epochs": 3, "top_k_pct": 0.1}},
]

async def save(exp_name, sid, params, result, metrics):
    async with aiosqlite.connect(str(settings.abs_path(settings.EXPERIMENT_DB))) as db:
        ph = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
        ps = json.dumps(params, ensure_ascii=False)
        c = await db.execute(
            "INSERT INTO experiments (user_id,name,strategy_id,strategy_category,pool_preset,test_start,test_end,params,params_hash,mode,status,progress_pct,progress_message,created_at,completed_at) VALUES (2,?,?,'technical','csi500',?,?,?,?,'batch','completed',100,'done',datetime('now'),datetime('now'))",
            (exp_name, sid, TSTART, TEND, ps, ph))
        eid = c.lastrowid
        if not result.equity_curve.empty and "equity" in result.equity_curve.columns:
            ec = result.equity_curve.reset_index().dropna(subset=["equity"])
            for _, r in ec.iterrows():
                await db.execute("INSERT INTO equity_curve (experiment_id,date,equity) VALUES (?,?,?)", (eid, str(r["date"])[:10], float(r["equity"])))
        for t in result.trade_log:
            await db.execute("INSERT INTO trade_log (experiment_id,date,code,action,price,shares,amount,cost,signal_strategy,signal_score) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid, t.date, t.code, t.action, t.price, t.shares, t.amount, t.cost, t.signal_strategy, t.signal_score))
        _M = metrics
        def v(x, d=0.0):
            try: return float(x) if x is not None and not (isinstance(x, float) and np.isnan(x)) else d
            except: return d
        await db.execute("INSERT INTO experiment_metrics (experiment_id,sharpe_ratio,annual_return,max_drawdown,volatility,calmar_ratio,sortino_ratio,win_rate,profit_loss_ratio,avg_trade_return,total_trades,avg_holding_days,turnover_rate,information_ratio,alpha,beta,tracking_error,var_95,cvar_95,skewness,kurtosis,profit_factor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, v(_M.get("sharpe_ratio")), v(_M.get("annualized_return")), v(_M.get("max_drawdown")),
             v(_M.get("annualized_volatility")), v(_M.get("calmar_ratio")), v(_M.get("sortino_ratio")),
             v(_M.get("win_rate")), v(_M.get("win_loss_ratio")), v(_M.get("avg_trade_return")),
             v(_M.get("total_trades"), 0), v(_M.get("avg_holding_days")), v(_M.get("turnover_rate")),
             v(_M.get("information_ratio")), v(_M.get("alpha")), v(_M.get("beta")),
             v(_M.get("tracking_error")), v(_M.get("var_95")), v(_M.get("cvar_95")),
             v(_M.get("return_skewness")), v(_M.get("return_kurtosis")), v(_M.get("profit_factor"))))
        await db.commit()
    return eid

async def main():
    # Clean
    c = await aiosqlite.connect(str(settings.abs_path(settings.EXPERIMENT_DB)))
    for t in ["experiment_metrics","equity_curve","trade_log","model_artifacts","sweep_experiments","param_sweeps","experiments"]:
        await c.execute(f"DELETE FROM {t}")
    await c.commit(); await c.close()
    print("DB cleaned.\n")

    cm = CostModel(commission_rate=0.0003, slippage_rate=0.001, stamp_duty_rate=0.001)
    results = []

    for cfg in STRATS:
        sid, name, params = cfg["id"], cfg["name"], cfg["p"]
        print(f"[{sid}] {name}", flush=True)
        try:
            strategy = registry.get_strategy(sid)
            signals = strategy.generate_batch_signals(pivot, params, TSTART, TEND)
            total_sig = sum(len(v) for v in signals.values())
            print(f"  → signals: {total_sig}", end=" ", flush=True)

            engine = BacktestEngine(1_000_000, cm, TSTART, TEND, 20)
            result = engine.run(signals, pivot, strategy_id=sid)
            metrics = compute_all_metrics(result.equity_curve, None, result.trade_log)
            s = metrics.get("sharpe_ratio", 0) or 0
            r = metrics.get("annualized_return", 0) or 0
            m = metrics.get("max_drawdown", 0) or 0
            w = metrics.get("win_rate", 0) or 0
            t = metrics.get("total_trades", 0) or 0
            print(f"trades: {len(result.trade_log)} Sharpe: {s:.2f} Return: {r*100:.1f}%", flush=True)

            eid = await save(name, sid, params, result, metrics)
            results.append({"sid": sid, "sharpe": s, "return": r, "mdd": m, "win_rate": w, "trades": t})
        except Exception as e:
            print(f"❌ {e}")
            import traceback; traceback.print_exc()

    # Print final table
    print(f"\n{'='*85}")
    print("FINAL RANKING - 十大策略调优最终结果")
    print(f"{'='*85}")
    results.sort(key=lambda x: x["sharpe"], reverse=True)
    print(f"{'Rank':>4} {'Strategy':<25} {'Sharpe':<8} {'年化':<8} {'MDD':<8} {'Win%':<6} {'交易':<5}")
    print("-" * 70)
    for i, r in enumerate(results, 1):
        print(f"{i:>4} {r['sid']:<25} {r['sharpe']:>7.2f} {r['return']*100:>7.1f}% {r['mdd']*100:>7.1f}% {r['win_rate']*100:>5.1f}% {r['trades']:>4}")
    print(f"{'='*85}")

asyncio.run(main())
