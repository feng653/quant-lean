"""Run backtest experiments for 10 strategies - v2 with redirect handling."""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import httpx

BASE = "http://localhost:8000"
HEADERS = {}

def login():
    global HEADERS
    r = httpx.post(f"{BASE}/api/auth/login", json={
        "username": "admin", "password": "admin123",
    }, follow_redirects=True, timeout=10)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    token = r.json()["data"]["access_token"]
    HEADERS = {"Authorization": f"Bearer {token}"}
    print("✅ Logged in as admin")

def create_exp(name, sid, test_start, test_end, params, pool="csi500",
               train_start=None, train_end=None):
    payload = {
        "name": name, "strategy_id": sid, "pool_preset": pool,
        "test_start": test_start, "test_end": test_end,
        "params": params, "mode": "batch",
    }
    if train_start: payload["train_start"] = train_start
    if train_end: payload["train_end"] = train_end

    r = httpx.post(f"{BASE}/api/experiments", json=payload,
                   headers=HEADERS, follow_redirects=True, timeout=30)
    if r.status_code == 200:
        d = r.json()["data"]
        print(f"  ✅ {sid:25s} id={d['experiment_id']}")
        return d["experiment_id"]
    else:
        print(f"  ❌ {sid:25s} {r.status_code}: {r.text[:120]}")
        return None

STRATEGIES = [
    ("MA双均线交叉", "ma_cross_v1",
     {"fast_period": 5, "slow_period": 20, "min_score": 0.2}),
    ("MACD金叉", "macd_signal_v1",
     {"fast": 12, "slow": 26, "signal": 9, "min_score": 0.1}),
    ("布林带突破", "bollinger_breakout_v1",
     {"period": 20, "std_multiplier": 2.0, "min_score": 0.1}),
    ("RSI均值回归", "rsi_reversal_v1",
     {"period": 14, "oversold": 30, "overbought": 70, "min_score": 0.1}),
    ("风险平价", "risk_parity_v1",
     {"lookback": 63, "rebalance_frequency": "monthly", "min_score": 0.1}),
    # Training strategies need MultiIndex data - may fail
    ("AlphaMaster GBR", "alphamaster_gbr_v1",
     {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05, "top_k": 30},
     "2022-01-01", "2023-06-30"),
    ("Alpha158+LightGBM", "alpha158_lgb_v1",
     {"n_estimators": 200, "max_depth": 5, "learning_rate": 0.05, "top_k_pct": 0.1},
     "2022-01-01", "2023-06-30"),
    ("Alpha158+XGBoost", "alpha158_xgb_v1",
     {"n_estimators": 200, "max_depth": 5, "learning_rate": 0.05, "top_k_pct": 0.1},
     "2022-01-01", "2023-06-30"),
    ("LSTM排序", "lstm_rank_v1",
     {"seq_len": 60, "hidden_size": 64, "num_layers": 2, "epochs": 20, "top_k_pct": 0.1},
     "2022-01-01", "2023-06-30"),
    ("Transformer排序", "transformer_rank_v1",
     {"seq_len": 60, "hidden_size": 64, "num_layers": 2, "epochs": 20, "top_k_pct": 0.1},
     "2022-01-01", "2023-06-30"),
]

login()
print(f"\nCreating experiments (test: 2023-07-01 ~ 2026-06-30)...\n")

ids = []
for name, sid, params, *train in STRATEGIES:
    ts = train[0] if train else None
    te = train[1] if train else None
    eid = create_exp(name, sid, "2023-07-01", "2026-06-30", params, train_start=ts, train_end=te)
    if eid: ids.append(eid)

print(f"\nCreated {len(ids)} experiments. Monitoring...")

for i in range(20):
    time.sleep(5)
    r = httpx.get(f"{BASE}/api/experiments", headers=HEADERS, follow_redirects=True, timeout=10)
    if r.status_code != 200:
        print(f"  [{i+1}] API error: {r.status_code}")
        continue
    items = r.json()["data"]["items"]
    completed = sum(1 for e in items if e["status"] in ("completed","failed"))
    running = sum(1 for e in items if e["status"] == "running")
    pending = sum(1 for e in items if e["status"] == "pending")
    print(f"  [{i+1}/20] done={completed} run={running} pend={pending}")
    if pending == 0 and running == 0:
        break

# Final report
r = httpx.get(f"{BASE}/api/experiments?limit=50", headers=HEADERS, follow_redirects=True, timeout=10)
results = r.json()["data"]["items"] if r.status_code == 200 else []
print(f"\n{'='*70}")
print("FINAL RESULTS - 十大策略近3年回测表现")
print(f"{'='*70}")
print(f"{'Status':<10} {'Strategy':<28} {'Sharpe':<8} {'Return':<10} {'MDD':<10} {'WinRate':<8}")
print("-" * 70)
for e in sorted(results, key=lambda x: x.get("sharpe_ratio") or -999, reverse=True):
    status = e["status"]
    sid = e["strategy_id"]
    s = e.get("sharpe_ratio")
    sharpe = f"{s:.2f}" if s else "N/A"
    ar = e.get("annual_return")
    ret = f"{ar*100:.1f}%" if ar else "N/A"
    mdd = e.get("max_drawdown")
    draw = f"{mdd*100:.1f}%" if mdd else "N/A"
    wr = e.get("win_rate")
    win = f"{wr*100:.0f}%" if wr else "N/A"
    print(f"{status:<10} {sid:<28} {sharpe:<8} {ret:<10} {draw:<10} {win:<8}")
print(f"{'='*70}")
