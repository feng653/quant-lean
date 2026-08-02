from __future__ import annotations

from pathlib import Path


README = Path(__file__).resolve().parents[2] / "README.md"


def test_readme_keeps_data_update_and_market_ledger_layers_distinct() -> None:
    """Keep warning-first research separate from strict future-live data."""

    content = README.read_text(encoding="utf-8")
    assert "### 2. 数据管理：先确认实际研究代，再开始实验" in content
    assert "独立 ResearchDataStore" in content
    assert "不可变 SQLite generation" in content
    assert "不会写入\n或冒充 `certified_live` 数据" in content
    assert "严格生产治理与个人研究数据分开显示" in content
    assert "完整双价格账本仍保留给未来实盘" in content
    assert "不是个人研究更新的人工审批步骤" in content
    assert "研究/模拟可在有实际可计算数据时携带高风险告警继续" in content
    assert "真实下单路由不存在" in content
