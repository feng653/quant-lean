"""Compile final ranking from all tuning results."""
import json

# Load all results
sources = [
    ("tests/tuned_technical.json", {}),
    ("tests/tuned_macd_rsi.json", {}),
    ("tests/tuned_dl.json", {}),
]

results = {}
for path, _ in sources:
    try:
        with open(path) as f:
            data = json.load(f)
        results.update(data)
        print(f"✅ Loaded {path}: {list(data.keys())}")
    except Exception as e:
        print(f"❌ {path}: {e}")

print(f"\n{'='*90}")
print("📊  十大策略调优后最终排名 (2023-07-01 ~ 2026-06-30, CSI500)")
print(f"{'='*90}")

# Build ranking
ranking = []
for sid, info in results.items():
    if not isinstance(info, dict):
        continue
    best = info.get("best_params", info.get("params", {}))
    ranking.append({
        "sid": sid,
        "sharpe": float(info.get("sharpe", 0)),
        "return": float(info.get("return", 0)),
        "mdd": float(info.get("mdd", 0)),
        "trades": int(info.get("trades", 0)),
        "win_rate": info.get("win_rate", 0),
        "error": info.get("error"),
        "params": best,
    })

ranking.sort(key=lambda x: x["sharpe"], reverse=True)

print(f"{'Rank':>4} {'Strategy':<25} {'Sharpe':<9} {'年化收益':<10} {'最大回撤':<10} {'Win%':<7} {'交易':<6}")
print("-" * 90)

for i, r in enumerate(ranking, 1):
    if r["error"]:
        print(f"{'❌':>4} {r['sid']:<25} {'SKIP':<9} {'SKIP':<10} {'SKIP':<10} {'SKIP':<7} {'SKIP':<6}")
        print(f"     └─ {r['error'][:80]}")
        continue
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f" {i}.")
    print(f"{medal:>4} {r['sid']:<25} {r['sharpe']:>8.2f} {r['return']*100:>9.2f}%"
          f" {r['mdd']*100:>9.2f}% {r.get('win_rate',0)*100:>6.1f}% {r['trades']:>5}")

print(f"{'='*90}")
print()

# Summary by improvement
print("📈 调优改善幅度:")
print("-" * 60)

# Base results (pre-tuning)
base = {
    "ma_cross_v1": -0.43,
    "bollinger_breakout_v1": -0.08,
    "risk_parity_v1": -0.18,
    "macd_signal_v1": 0.24,
    "rsi_reversal_v1": 0.53,
}

for r in ranking:
    sid = r["sid"]
    if sid in base:
        old = base[sid]
        new = r["sharpe"]
        delta = new - old
        arrow = "↑" if delta > 0.05 else ("→" if abs(delta) < 0.05 else "↓")
        print(f"  {sid:<25s}: Sharpe {old:.2f} → {new:.2f}  {arrow} {delta:+.2f}")
    else:
        print(f"  {sid:<25s}: Sharpe = {r['sharpe']:.2f}")

print()
print("⏳ 未完成策略 (因子/ML): alphamaster_gbr_v1, alpha158_lgb_v1, alpha158_xgb_v1")
print("   原因: 训练耗时过长(>2min/策略)，需优化特征计算流水线")

# Save as JSON
with open("tests/final_ranking.json", "w") as f:
    json.dump(ranking, f, indent=2, ensure_ascii=False)
print("\n✅ Saved to tests/final_ranking.json")
