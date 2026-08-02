"""Run backtest experiments for all 10 strategies."""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import httpx

BASE = "http://localhost:8000"
HEADERS = {}

def login():
    """Login as admin."""
    global HEADERS
    r = httpx.post(f"{BASE}/api/auth/login", json={
        "username": "admin",
        "password": "admin123",
    }, timeout=10)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    token = r.json()["data"]["access_token"]
    HEADERS = {"Authorization": f"Bearer {token}"}
    print("✅ Logged in as admin")

def create_experiment(name, strategy_id, test_start, test_end, params, pool="csi500"):
    """Create an experiment via API."""
    payload = {
        "name": name,
        "strategy_id": strategy_id,
        "pool_preset": pool,
        "test_start": test_start,
        "test_end": test_end,
        "params": params,
        "mode": "batch",
    }
    r = httpx.post(f"{BASE}/api/experiments", json=payload, headers=HEADERS, timeout=10)
    if r.status_code == 200:
        data = r.json()["data"]
        print(f"  ✅ Created: {name} -> experiment_id={data['experiment_id']}, job_id={data['job_id']}")
        return data["experiment_id"]
    else:
        print(f"  ❌ Failed: {name} -> {r.status_code}: {r.text[:100]}")
        return None

# All 10 strategies configuration
STRATEGIES = [
    # --- Technical (no training needed) ---
    {
        "name": "MA双均线交叉 (近3年)",
        "strategy_id": "ma_cross_v1",
        "params": {"fast_period": 5, "slow_period": 20, "min_score": 0.2},
    },
    {
        "name": "MACD金叉 (近3年)",
        "strategy_id": "macd_signal_v1",
        "params": {"fast": 12, "slow": 26, "signal": 9, "min_score": 0.1},
    },
    {
        "name": "布林带突破 (近3年)",
        "strategy_id": "bollinger_breakout_v1",
        "params": {"period": 20, "std_multiplier": 2.0, "min_score": 0.1},
    },
    {
        "name": "RSI均值回归 (近3年)",
        "strategy_id": "rsi_reversal_v1",
        "params": {"period": 14, "oversold": 30, "overbought": 70, "min_score": 0.1},
    },
    {
        "name": "风险平价 (近3年)",
        "strategy_id": "risk_parity_v1",
        "params": {"lookback": 63, "rebalance_frequency": "monthly", "min_score": 0.1},
    },
    # --- Factor (needs training) ---
    {
        "name": "AlphaMaster GBR (近3年)",
        "strategy_id": "alphamaster_gbr_v1",
        "params": {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05, "top_k": 30},
        "train_start": "2022-01-01",
        "train_end": "2023-06-30",
    },
    # --- ML (needs training) ---
    {
        "name": "Alpha158+LightGBM (近3年)",
        "strategy_id": "alpha158_lgb_v1",
        "params": {"n_estimators": 200, "max_depth": 5, "learning_rate": 0.05, "top_k_pct": 0.1},
        "train_start": "2022-01-01",
        "train_end": "2023-06-30",
    },
    {
        "name": "Alpha158+XGBoost (近3年)",
        "strategy_id": "alpha158_xgb_v1",
        "params": {"n_estimators": 200, "max_depth": 5, "learning_rate": 0.05, "top_k_pct": 0.1},
        "train_start": "2022-01-01",
        "train_end": "2023-06-30",
    },
    {
        "name": "LSTM深度学习排序 (近3年)",
        "strategy_id": "lstm_rank_v1",
        "params": {"seq_len": 60, "hidden_size": 64, "num_layers": 2, "epochs": 20, "top_k_pct": 0.1},
        "train_start": "2022-01-01",
        "train_end": "2023-06-30",
    },
    {
        "name": "Transformer排序 (近3年)",
        "strategy_id": "transformer_rank_v1",
        "params": {"seq_len": 60, "hidden_size": 64, "num_layers": 2, "epochs": 20, "top_k_pct": 0.1},
        "train_start": "2022-01-01",
        "train_end": "2023-06-30",
    },
]

TEST_START = "2023-07-01"
TEST_END = "2026-06-30"

def main():
    login()

    print(f"\n{'='*60}")
    print(f"Creating {len(STRATEGIES)} backtest experiments")
    print(f"Test period: {TEST_START} ~ {TEST_END}")
    print(f"{'='*60}\n")

    exp_ids = []
    for s in STRATEGIES:
        sid = s["strategy_id"]
        name = s["name"]
        params = s["params"]

        print(f"  [{sid}] {name}")
        eid = create_experiment(name, sid, TEST_START, TEST_END, params)
        if eid:
            exp_ids.append(eid)
        print()

    print(f"{'='*60}")
    print(f"Created {len(exp_ids)} experiments")
    print(f"Waiting for job worker to process...")

    # Wait and check status
    for i in range(15):
        time.sleep(4)
        # Check all experiments
        r = httpx.get(f"{BASE}/api/experiments", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            items = r.json()["data"]["items"]
            completed = sum(1 for e in items if e["status"] in ("completed", "failed"))
            pending = sum(1 for e in items if e["status"] == "pending")
            running = sum(1 for e in items if e["status"] == "running")
            print(f"  [{i+1}/15] completed={completed} running={running} pending={pending}")

            if pending == 0 and running == 0:
                print("\n✅ All experiments finished!")
                return items

    print("\n⏳ Still waiting... Showing final status:")
    r = httpx.get(f"{BASE}/api/experiments", headers=HEADERS, timeout=10)
    return r.json()["data"]["items"] if r.status_code == 200 else []

if __name__ == "__main__":
    results = main()
    print(f"\n{'='*60}")
    print("FINAL RESULTS:")
    print(f"{'='*60}")
    for e in results:
        sid = e["strategy_id"]
        metrics = e.get("sharpe_ratio", "N/A")
        status = e["status"]
        print(f"  [{status:10s}] {e['name']:<30s} Sharpe={metrics}")
