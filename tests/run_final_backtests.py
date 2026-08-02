"""Final backtest run for 5 technical + 5 ML strategies with correct params."""
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
    assert r.status_code == 200
    HEADERS = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
    print("✅ Logged in as admin")

def create_exp(name, sid, params, pool="csi500", train_start=None, train_end=None):
    payload = {
        "name": name, "strategy_id": sid, "pool_preset": pool,
        "test_start": "2023-07-01", "test_end": "2026-06-30",
        "params": params, "mode": "batch",
    }
    if train_start: payload["train_start"] = train_start
    if train_end: payload["train_end"] = train_end

    r = httpx.post(f"{BASE}/api/experiments", json=payload,
                   headers=HEADERS, follow_redirects=True, timeout=30)
    if r.status_code == 200:
        d = r.json()["data"]
        print(f"  ✅ {sid}")
        return d["experiment_id"]
    else:
        print(f"  ❌ {sid}: {r.status_code} {r.text[:100]}")
        return None

STRATEGIES = [
    ("MA双均线交叉", "ma_cross_v1",
     {"fast_period": 5, "slow_period": 20, "min_score": 0.0}),
    ("MACD金叉", "macd_signal_v1",
     {"fast": 12, "slow": 26, "signal": 9, "min_score": 0.0}),
    ("布林带突破", "bollinger_breakout_v1",
     {"period": 20, "std_multiplier": 2.0, "min_score": 0.0}),
    ("RSI均值回归", "rsi_reversal_v1",
     {"period": 14, "oversold": 30, "overbought": 70, "min_score": 0.0}),
    ("风险平价", "risk_parity_v1",
     {"lookback": 63, "rebalance_frequency": "monthly", "min_score": 0.0}),
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
print(f"\nCreating 10 experiments (2023-07-01 ~ 2026-06-30)...\n")

ids = []
for name, sid, params, *train in STRATEGIES:
    eid = create_exp(name, sid, params,
                     train_start=train[0] if train else None,
                     train_end=train[1] if train else None)
    if eid: ids.append(eid)

print(f"\nCreated {len(ids)} experiments. Monitoring job worker...")

for i in range(30):
    time.sleep(5)
    r = httpx.get(f"{BASE}/api/experiments?limit=50", headers=HEADERS, follow_redirects=True, timeout=10)
    if r.status_code != 200:
        continue
    items = r.json()["data"]["items"]
    completed = sum(1 for e in items if e["status"] in ("completed","failed"))
    running = sum(1 for e in items if e["status"] == "running")
    pending = sum(1 for e in items if e["status"] == "pending")
    print(f"  [{i+1:2d}] done={completed:2d} run={running} pend={pending}")
    if pending == 0 and running == 0:
        break

# Final report - fetch metrics for each
print(f"\n{'='*80}")
print("📊 十大策略近3年回测表现 (2023-07-01 ~ 2026-06-30)")
print(f"{'='*80}")

results = []
r = httpx.get(f"{BASE}/api/experiments?limit=50", headers=HEADERS, follow_redirects=True, timeout=10)
if r.status_code == 200:
    items = r.json()["data"]["items"]
    for e in items:
        eid = e["id"]
        # Get detailed metrics
        mr = httpx.get(f"{BASE}/api/experiments/{eid}/metrics", headers=HEADERS, follow_redirects=True, timeout=10)
        metrics = {}
        if mr.status_code == 200:
            data = mr.json().get("data")
            if isinstance(data, dict):
                metrics = data
        results.append({
            "id": eid,
            "name": e["name"],
            "sid": e["strategy_id"],
            "status": e["status"],
            "error": (e.get("error_log") or "")[:80],
            **metrics
        })

# Sort by sharpe
results.sort(key=lambda x: x.get("sharpe_ratio") or -999, reverse=True)

print(f"{'Status':<9} {'Strategy':<20} {'Sharpe':<9} {'年化收益':<10} {'最大回撤':<10} {'胜率':<8} {'交易次数':<9}")
print("-" * 80)
for r in results:
    s = r.get("sharpe_ratio")
    ar = r.get("annual_return")
    mdd = r.get("max_drawdown")
    wr = r.get("win_rate")
    tt = r.get("total_trades")
    status = r["status"]

    sharpe_s = f"{s:.2f}" if isinstance(s, (int,float)) and s != 0 else "-"
    ret_s = f"{ar*100:.1f}%" if isinstance(ar, (int,float)) and ar != 0 else "-"
    mdd_s = f"{mdd*100:.1f}%" if isinstance(mdd, (int,float)) and mdd != 0 else "-"
    wr_s = f"{wr*100:.0f}%" if isinstance(wr, (int,float)) and wr != 0 else "-"
    tt_s = str(int(tt)) if isinstance(tt, (int,float)) and tt != 0 else "-"

    print(f"{status:<9} {r['sid']:<20} {sharpe_s:<9} {ret_s:<10} {mdd_s:<10} {wr_s:<8} {tt_s:<9}")
    if r["error"]:
        print(f"  └─ Error: {r['error']}")

print(f"{'='*80}")
print("备注: min_score=0.0 确保所有信号都被捕获。ML策略需要MultiIndex数据格式。")
