# 策略开发指南

本文档介绍如何在量化验证平台中创建、注册和测试一个新策略。

---

## 核心概念

所有策略必须继承 `StrategyProtocol` 抽象基类，实现统一的接口。框架通过**注册中心自动扫描**发现策略，无需手动注册。

### 策略分类

| 类别 | 枚举值 | 说明 |
|------|--------|------|
| 技术分析 | `StrategyCategory.TECHNICAL` | 基于技术指标的规则型策略，无需训练 |
| 机器学习 | `StrategyCategory.ML` | 需要训练的 ML/DL 模型策略 |
| 因子模型 | `StrategyCategory.FACTOR` | 基于多因子的量化策略 |
| 组合优化 | `StrategyCategory.PORTFOLIO` | 仓位管理 / 配置策略 |
| 复合策略 | `StrategyCategory.COMPOSITE` | 组合多个子策略 |

### 策略模式

| 模式 | 枚举值 | 必须实现的方法 |
|------|--------|---------------|
| 批量 | `StrategyMode.BATCH` | `generate_batch_signals()` |
| 实时 | `StrategyMode.REALTIME` | `generate_realtime_signal()` |

---

## 快速上手：创建一个技术指标策略

### 1. 创建策略文件

在 `backend/strategies/technical/` 下新建 `my_strategy.py`：

```python
"""我的自定义策略 —— 简要说明."""

from __future__ import annotations

import pandas as pd

from backend.core.types import SignalDict, SignalItem
from backend.strategies.base import (
    ParamField,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    StrategyProtocol,
)

# ── 参数定义 ──

PARAM_SCHEMA: list[ParamField] = [
    ParamField(
        name="lookback",
        type="int",
        default=20,
        description="回看窗口（日）",
        min=5,
        max=120,
        step=1,
    ),
    ParamField(
        name="threshold",
        type="float",
        default=0.02,
        description="信号触发阈值",
        min=0.0,
        max=0.1,
        step=0.005,
    ),
]

# ── 策略类 ──

class MyStrategy(StrategyProtocol):
    """我的策略详细说明。

    策略原理:
        描述策略的核心逻辑——在什么条件下买入、什么条件下卖出。

    适用范围:
        描述该策略最适合的市场环境。
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        """返回策略元数据。"""
        return StrategyMetadata(
            # ⭐ 必须：唯一标识符，遵循 {name}_v{version} 命名
            strategy_id="my_strategy_v1",

            # ⭐ 必须：前端展示名称
            display_name="我的自定义策略",

            # ⭐ 必须：语义化版本号
            version="1.0.0",

            # ⭐ 必须：策略分类
            category=StrategyCategory.TECHNICAL,

            # ⭐ 必须：描述策略原理（AI 分析时使用）
            description=(
                "基于回看窗口内的价格变动，当涨跌幅超过阈值时产生交易信号。"
                "适合趋势明显的市场环境。"
            ),

            # 支持的模式（默认仅 batch）
            supported_modes=[StrategyMode.BATCH],

            # 是否需要训练（技术指标类策略为 False）
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,

            # 参数 Schema（必填，用于前端生成表单）
            params=PARAM_SCHEMA,

            # 最大持仓比例
            max_position_pct=0.10,

            # 支持的仓位管理模式
            supported_position_modes=["equal_weight"],

            # 标签（用于筛选和搜索）
            tags=["趋势跟踪", "自定义"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        """自定义参数校验。

        Returns:
            (is_valid, error_message)
        """
        lookback = params.get("lookback", 20)
        if not isinstance(lookback, int) or lookback < 5:
            return False, "lookback 必须为 ≥5 的整数"
        return True, ""

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        """批量生成交易信号。

        Args:
            pivot: 日线宽表。Index 为日期，Columns 为 (code, field) MultiIndex。
            params: 策略参数字典。
            start_date: 信号起始日期 "YYYY-MM-DD"。
            end_date: 信号结束日期 "YYYY-MM-DD"。

        Returns:
            SignalDict: { "2024-01-05": [SignalItem(...), ...], ... }
        """
        lookback = params.get("lookback", 20)
        threshold = params.get("threshold", 0.02)

        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)

        # 提取股票代码列表
        codes = self._get_codes(pivot)

        signals: SignalDict = {}

        for code in codes:
            close = self._get_close(pivot, code)
            if close is None or len(close) < lookback:
                continue

            close = close.loc[start:end].dropna()

            # 计算回看窗口收益率
            returns = close.pct_change(periods=lookback)

            for date in returns.index:
                r = returns.loc[date]
                if pd.isna(r):
                    continue

                date_str = date.strftime("%Y-%m-%d")
                signals.setdefault(date_str, [])

                if r > threshold:
                    signals[date_str].append(
                        SignalItem(
                            code=code,
                            action="BUY",
                            score=min(r / threshold, 1.0),
                            weight=1.0,
                        )
                    )
                elif r < -threshold:
                    signals[date_str].append(
                        SignalItem(
                            code=code,
                            action="SELL",
                            score=min(abs(r) / threshold, 1.0),
                            weight=1.0,
                        )
                    )

        return signals

    # ── 辅助方法 ──

    @staticmethod
    def _get_codes(pivot: pd.DataFrame) -> list[str]:
        if isinstance(pivot.columns, pd.MultiIndex):
            return list({c[0] for c in pivot.columns if isinstance(c, tuple)})
        return []

    @staticmethod
    def _get_close(pivot: pd.DataFrame, code: str) -> pd.Series | None:
        if isinstance(pivot.columns, pd.MultiIndex):
            for field in ("close", "Close", "收盘"):
                if (code, field) in pivot.columns:
                    return pivot[(code, field)]
        return None
```

### 2. 注册策略（自动）

策略放在 `backend/strategies/` 目录下即可。启动时注册中心会自动扫描所有 `*.py` 文件，发现 `StrategyProtocol` 的子类。

也可以手动触发扫描：

```
POST /api/strategies/scan
```

**自动扫描的规则：**

- 递归扫描 `backend/strategies/` 下所有 `.py` 文件
- 跳过以下划线开头（`_*.py`）的文件
- 跳过 `base.py`、`registry.py`
- 跳过抽象类（`inspect.isabstract()` 检查）
- 实例化策略类并调用 `metadata()` 获取元数据
- 调用 `on_register()` 生命周期钩子

### 3. 验证策略已注册

```
GET /api/strategies/my_strategy_v1
```

返回策略详情即表示注册成功。

---

## metadata() 编写规范

### strategy_id 命名

格式：`{name}_v{version}`

- 使用小写 + 下划线
- 不能包含特殊字符
- 示例：`ma_cross_v1`、`alpha158_xgb_v1`

### description 字段

**必须写清楚策略原理**。AI 分析、前端策略详情页都会使用此字段。

好的示例：
```
"经典双均线趋势跟踪策略。计算快速均线和慢速均线，"
"金叉（快线上穿慢线）产生买入信号，死叉（快线下穿慢线）产生卖出信号。"
"适用于趋势明显的市场，震荡市需配合过滤条件使用。"
```

差的示例：
```
"一个策略"
```

### 参数定义注意事项

- `min`/`max`/`step` 用于前端滑块控件
- `choices` 用于下拉选择组件
- `required=True` 的参数必须由用户提供（无默认值时）

---

## generate_batch_signals() 规范

### 输入：pivot 数据结构

`pivot` 是一个 pandas DataFrame，格式为：

- **Index**: `pd.DatetimeIndex`，日期
- **Columns**: `pd.MultiIndex`，层级为 `(code, field)`
  - 例如：`('000001.SZ', 'close')`, `('000001.SZ', 'volume')`
- 必须包含 `close` 字段

### 输出：SignalDict

```python
# {日期 → [SignalItem, ...]}
{
    "2024-01-05": [
        SignalItem(code="000001.SZ", action="BUY", score=0.85, weight=1.0),
        SignalItem(code="000002.SZ", action="SELL", score=0.60, weight=1.0),
    ],
    "2024-01-08": [...],
}
```

### 关键约束

1. **避免前视偏差**：计算 T 日的信号时，只能用 T-1 日及之前的数据
2. **日期过滤**：信号日期必须在 `start_date` ~ `end_date` 范围内
3. **score 范围**：建议在 `[0, 1]` 之间，引擎会按 score 排序
4. **空信号**：某日无信号时，`SignalDict` 中不包含该日的 key 即可

### 辅助方法参考

查看 `backend/strategies/technical/ma_cross.py` 中的 `_extract_codes()` 和 `_get_close_series()` 了解如何从 pivot 提取数据。

---

## 创建 ML 策略

ML 策略需要额外覆写 `train()` 方法：

```python
from backend.strategies.base import TrainedModel

class MyMLStrategy(StrategyProtocol):

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            # ...
            requires_training=True,
            retrain_frequency=RetrainFrequency.MONTHLY,
            estimated_training_seconds=300,
        )

    def train(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
        progress_callback: Callable[[float, str], None] | None = None,
        existing_model: Any = None,
    ) -> TrainedModel:
        """训练 ML 模型。

        进度回调示例：
            progress_callback(0.5, "训练中... 50%")

        Returns:
            TrainedModel(model=..., feature_importance=..., train_metrics=...)
        """
        # 1. 准备特征和标签
        X, y = self._prepare_features(pivot, params, train_start, train_end)

        # 2. 训练
        model = SomeModel()
        model.fit(X, y)
        if progress_callback:
            progress_callback(1.0, "训练完成")

        # 3. 计算训练指标
        train_pred = model.predict(X)
        metrics = {"accuracy": float((train_pred == y).mean())}

        return TrainedModel(
            model=model,
            feature_importance=self._get_importance(model),
            train_metrics=metrics,
        )
```

参考示例：
- `backend/strategies/ml/alpha158_xgb.py` — XGBoost 多因子训练
- `backend/strategies/ml/lstm_rank.py` — LSTM 深度学习训练

---

## 创建组合策略

组合策略继承 `CompositeStrategy`，在 `metadata()` 中声明子策略：

```python
from backend.strategies.base import CompositeStrategy, SubStrategyRef

class MyComposite(CompositeStrategy):

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="my_composite_v1",
            display_name="我的组合策略",
            version="1.0.0",
            category=StrategyCategory.COMPOSITE,
            description="组合 MA Cross + RSI Reversal，加权融合信号。",
            sub_strategies=[
                SubStrategyRef(
                    strategy_id="ma_cross_v1",
                    role="趋势跟踪",
                    params_override={"fast_period": 20},
                ),
                SubStrategyRef(
                    strategy_id="rsi_reversal_v1",
                    role="均值回归",
                    params_override={"period": 14},
                ),
            ],
            integration_method="weighted_sum",
        )

    def generate_batch_signals(self, pivot, params, start_date, end_date):
        # 获取子策略实例
        ma = self._get_sub_strategy("ma_cross_v1")
        rsi = self._get_sub_strategy("rsi_reversal_v1")

        # 获取子策略信号
        sig1 = ma.generate_batch_signals(pivot, {"fast_period": 20, "slow_period": 60, "min_score": 0.5}, start_date, end_date)
        sig2 = rsi.generate_batch_signals(pivot, {"period": 14, "oversold": 30, "overbought": 70, "min_score": 0.3}, start_date, end_date)

        # 加权合并
        return self._merge_signals([sig1, sig2], weights=[0.6, 0.4])
```

### 从单策略研究结果生成静态组合

相关性页面可以从至少三个已完成的非 ML 单策略实验生成五个组合草案。后端会先复核
每个实验的 PIT-only 运行清单、规范价格绑定、所有权和参数，再按照收益、波动、普通/
尾部相关和持仓重叠构造候选。候选使用 `composite_research_weighted_v1`：

- `component_specs` 是 JSON 数组，每项只允许 `strategy_id`、`params` 以及成对出现的
  `source_experiment_id` / `source_manifest_hash`；未知字段、嵌套组合、ML 策略和无效参数
  全部拒绝；
- `static_weights` 是同顺序的有限非负 JSON 数组，运行时归一化；
- 来源实验参数和运行清单哈希进入父实验参数哈希，因此同名但不同证据的组合不会混淆；
- 来源内容只作为数据校验，运行时不会加载来源模型、pickle、Python 表达式或动态代码；
- 生成结果始终是草稿，不会自动注册、提交、晋级或部署。

人工决定创建候选实验后，仍需走普通实验的 PIT 数据、锁定样本、成本与晋级门禁。

---

## 测试策略

### 单元测试

```python
import pandas as pd
import numpy as np
from my_strategy import MyStrategy

def test_basic():
    strat = MyStrategy()

    # 构造测试数据
    dates = pd.date_range("2024-01-01", "2024-03-01", freq="B")
    pivot = pd.DataFrame(
        {("000001.SZ", "close"): np.random.randn(len(dates)).cumsum() + 100},
        index=dates,
    )
    pivot.columns = pd.MultiIndex.from_tuples(pivot.columns)

    # 生成信号
    signals = strat.generate_batch_signals(
        pivot,
        params={"lookback": 10, "threshold": 0.05},
        start_date="2024-02-01",
        end_date="2024-02-29",
    )

    assert isinstance(signals, dict)
    for items in signals.values():
        for item in items:
            assert item.action in ("BUY", "SELL")
```

### 参数校验测试

```python
def test_validation():
    strat = MyStrategy()
    valid, _ = strat.validate_params({"lookback": 20, "threshold": 0.05})
    assert valid

    invalid, msg = strat.validate_params({"lookback": 3, "threshold": 0.05})
    assert not invalid
    assert "lookback" in msg
```

### 通过 API 测试

```bash
# 校验参数
curl -X POST http://localhost:8000/api/strategies/my_strategy_v1/validate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"params": {"lookback": 20, "threshold": 0.05}}'

# 创建实验
curl -X POST http://localhost:8000/api/experiments \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "name": "My Strategy Test",
    "strategy_id": "my_strategy_v1",
    "pool_preset": "csi800",
    "test_start": "2024-01-01",
    "test_end": "2025-12-31",
    "params": {"lookback": 20, "threshold": 0.05}
  }'
```

---

## 注意事项

1. **避免前视偏差**：信号计算必须使用 `shift(1)` 确保只用历史数据
2. **空数据处理**：`pivot` 可能为空，需要做防御性检查
3. **性能**：策略被批量回测引擎调用，遍历数百只股票数千个交易日，需注意性能
4. **幂等性**：相同输入必须产生相同输出
5. **文件命名**：放在对应分类的子目录下（`technical/`、`ml/`、`factor/`、`portfolio/`、`composite/`）
6. **不要修改代码文件**之外的系统文件
