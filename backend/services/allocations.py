"""Portfolio allocation validation and rebalance preview."""

from __future__ import annotations

from typing import Any


def _to_bps(item: dict[str, Any], key: str, legacy_key: str | None = None) -> int:
    value = item.get(key)
    if value is None and legacy_key:
        value = item.get(legacy_key)
    if value is None:
        return 0
    numeric = float(value)
    if key == "target_weight_bps" and item.get(key) is not None:
        return int(round(numeric))
    if abs(numeric) <= 1:
        return int(round(numeric * 10_000))
    if abs(numeric) <= 100:
        return int(round(numeric * 100))
    return int(round(numeric))


def canonicalize_allocations(
    allocations: list[dict[str, Any]],
    total_capital: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize allocation units and validate all portfolio constraints.

    The API accepts legacy fractional ``weight`` values for compatibility but
    persists integer basis points.  A sum below 10,000 represents explicit
    cash; a sum above 10,000 is rejected.
    """
    errors: list[str] = []
    warnings: list[str] = []
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()

    if total_capital <= 0:
        errors.append("组合总资金必须大于 0")

    for index, raw in enumerate(allocations):
        try:
            deployment_id = int(raw["deployment_id"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"第 {index + 1} 行缺少有效 deployment_id")
            continue
        if deployment_id in seen:
            errors.append(f"部署 {deployment_id} 重复")
            continue
        seen.add(deployment_id)

        target = _to_bps(raw, "target_weight_bps", "weight")
        minimum = _to_bps(raw, "min_weight_bps", "min_weight")
        maximum = _to_bps(raw, "max_weight_bps", "max_weight")
        if "max_weight_bps" not in raw and "max_weight" not in raw:
            maximum = 10_000
        risk_budget = (
            _to_bps(raw, "risk_budget_bps", "risk_budget")
            if raw.get("risk_budget_bps") is not None or raw.get("risk_budget") is not None
            else None
        )

        if not 0 <= minimum <= maximum <= 10_000:
            errors.append(f"部署 {deployment_id} 的最小/最大权重无效")
        if not minimum <= target <= maximum:
            errors.append(
                f"部署 {deployment_id} 的目标权重 {target}bp 超出 "
                f"[{minimum}, {maximum}]bp"
            )
        if risk_budget is not None and not 0 <= risk_budget <= 10_000:
            errors.append(f"部署 {deployment_id} 的风险预算无效")

        normalized.append(
            {
                "deployment_id": deployment_id,
                "target_weight_bps": target,
                "min_weight_bps": minimum,
                "max_weight_bps": maximum,
                "locked": bool(raw.get("locked", False)),
                "risk_budget_bps": risk_budget,
                "capital": round(total_capital * target / 10_000, 2),
            }
        )

    total = sum(item["target_weight_bps"] for item in normalized)
    if total > 10_000:
        errors.append(f"策略权重合计 {total}bp，不能超过 10000bp")
    cash_weight = max(10_000 - total, 0)
    if cash_weight > 5_000:
        warnings.append("现金权重超过 50%，请确认是否符合预期")
    if allocations and total == 0:
        warnings.append("所有策略目标权重均为 0")

    return normalized, {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "strategy_weight_bps": total,
        "cash_weight_bps": cash_weight,
        "total_weight_bps": total + cash_weight,
        "cash_capital": round(total_capital * cash_weight / 10_000, 2),
    }


def build_rebalance_preview(
    allocations: list[dict[str, Any]],
    total_capital: float,
    current_market_values: dict[int, float],
    estimated_cost_rate: float = 0.0013,
) -> dict[str, Any]:
    """Create a current-to-target capital and cost preview."""
    rows: list[dict[str, Any]] = []
    turnover_amount = 0.0
    for item in allocations:
        deployment_id = item["deployment_id"]
        current = float(current_market_values.get(deployment_id, 0.0))
        target = total_capital * item["target_weight_bps"] / 10_000
        delta = target - current
        turnover_amount += abs(delta)
        rows.append(
            {
                **item,
                "current_capital": round(current, 2),
                "target_capital": round(target, 2),
                "capital_delta": round(delta, 2),
                "direction": "BUY" if delta > 0 else "SELL" if delta < 0 else "HOLD",
                "estimated_cost": round(abs(delta) * estimated_cost_rate, 2),
            }
        )
    return {
        "rows": rows,
        "one_way_turnover": round(turnover_amount / 2, 2),
        "turnover_rate": round(turnover_amount / max(total_capital * 2, 1), 6),
        "estimated_cost": round(turnover_amount * estimated_cost_rate, 2),
    }
