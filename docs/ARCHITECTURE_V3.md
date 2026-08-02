# 量化验证平台 V3 — 完整架构设计

> 版本: v0.3 | 日期: 2026-07-27 | 状态: 设计阶段

---

## 目录

1. [核心变更（V2→V3）](#一核心变更)
2. [策略分类体系](#二策略分类体系)
3. [系统架构总览](#三系统架构总览)
4. [代码目录结构](#四代码目录结构)
5. [策略接口设计（含组合策略）](#五策略接口设计v3)
6. [多用户权限系统（RBAC）](#六多用户权限系统)
7. [数据库设计](#七数据库设计v3)
8. [模型生命周期管理](#八模型生命周期管理)
9. [实验标注与部署选择器](#九实验标注与部署选择器)
10. [API 接口设计](#十api-接口设计v3)
11. [前端页面设计](#十一前端页面设计v3)
12. [策略重训练调度](#十二策略重训练调度)

---

## 一、核心变更（V2→V3）

| 变更点 | V2 设计 | V3 设计（本版） |
|--------|---------|-----------------|
| **组合策略** | 协调层是独立模块，输出权重 | 组合策略 = 一种策略类型，与单策略平行，实现相同接口 |
| **多用户** | 每用户数据隔离 | 数据共享，基于RBAC权限控制操作 |
| **协调层** | 独立层，读DB输出权重 | 化为可扩展的组合策略代码，框架只提供子策略注册查询能力 |
| **模型生命周期** | 一次训练 | 支持周期性重训练 + 模型版本归档 |
| **实验管理** | 基础列表 | 加 Star/Label 标注 + 策略详情页"最佳实验"展示 |
| **部署方式** | 手动选实验ID | 弹出档案选择器（标注优先显示） |
| **行情契约** | 收盘价二维表 | `(code, field)` 多级列日线面板，至少包含 open/close |
| **执行时序** | 信号与成交日语义不明确 | T 日收盘信号，T+1 交易日开盘成交，T+1 收盘估值 |
| **组合权重** | 前端滑块直接写浮点权重 | 整数基点 + 显式现金 + 上下限/锁定/风险预算 + 版本发布 |

---

### 1.1 行情与执行契约

数据源和 Parquet 缓存统一返回以交易日为索引、以 `(股票代码, 字段)` 为列的日线面板。字段为 `open/high/low/close/volume/amount`；旧版只有收盘价的缓存会自动失效并重建。

回测和模拟盘使用同一条无未来数据约束：

```
T 日收盘后：策略只读取 <= T 的数据并产生信号
T+1 交易日 09:30：按 T+1 open 成交；缺失开盘价则拒单
T+1 收盘后：按 close 生成持仓快照和组合净值
```

收盘价只用于估值，禁止作为缺失开盘价的成交回退。模拟运行按“用户 + 交易日”幂等，订单、信号、持仓和净值由后端数据库作为唯一事实来源。

---

## 二、策略分类体系

```
                        StrategyProtocol (统一接口)
                                │
                ┌───────────────┼───────────────┐
                │               │               │
        ┌───────▼──────┐ ┌─────▼──────┐ ┌──────▼──────┐
        │ Atomic        │ │ Composite   │ │ Reserved    │
        │ 原子策略       │ │ 组合策略     │ │ (扩展预留)   │
        │               │ │             │ │             │
        │ 直接从市场数据  │ │ 内部组合N个  │ │             │
        │ 生成交易信号    │ │ 子策略的信号  │ │             │
        │               │ │ 产出统一信号  │ │             │
        └───┬───┬───┬───┘ └──────────────┘ └──────────────┘
            │   │   │
    ┌───────┘   │   └───────┐
    ▼           ▼           ▼
  technical    ml        factor/portfolio
 (均线/MACD) (LGB/XGB)  (RiskParity/GBR)
```

### 核心原则

**组合策略和原子策略暴露完全相同的接口**。执行引擎、实验系统、交易系统**不区分**两者。唯一的区别在于：
- `category = "composite"`
- 元数据中必须声明 `sub_strategies`（整合了哪些策略）和 `integration_method`（整合方式）
- 前端显示时加"组合"徽章，展开可看子策略

### 组合策略示例

```python
class MAWithRiskParity(CompositeStrategy):
    """
    60% MA Cross + 40% Risk Parity
    内部实例化两个子策略，分别生成信号，按权重合并
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="ma_risk_parity_combo_v1",
            display_name="均线趋势 + 风险平价组合",
            category=StrategyCategory.COMPOSITE,
            description="趋势跟踪主力(60%) + 低波动压舱石(40%)，月度再平衡",
            sub_strategies=[
                SubStrategyRef(strategy_id="ma_cross_v1", role="趋势跟踪主力"),
                SubStrategyRef(strategy_id="risk_parity_v1", role="低波动压舱石"),
            ],
            integration_method=(
                "1. 分别运行两个子策略生成各自信号\n"
                "2. MA Cross信号分配60%可用资金\n"
                "3. Risk Parity信号分配40%可用资金\n"
                "4. 月度统一再平衡"
            ),
            params=[
                ParamField("ma_weight", "float", 0.6,
                    description="MA Cross策略的资金占比", min=0.1, max=0.9, step=0.05),
                ParamField("rp_weight", "float", 0.4,
                    description="Risk Parity策略的资金占比", min=0.1, max=0.9, step=0.05),
            ],
            # ... 其余元数据
        )

    def generate_batch_signals(self, pivot, params, start_date, end_date):
        # 内部实例化子策略
        ma = MACrossStrategy()
        rp = RiskParityStrategy()

        signals_ma = ma.generate_batch_signals(pivot, params.get("ma_params", {}),
                                                start_date, end_date)
        signals_rp = rp.generate_batch_signals(pivot, params.get("rp_params", {}),
                                                start_date, end_date)

        # 合并信号：MA信号占60%资金，RP信号占40%资金
        return self._merge_signals(signals_ma, signals_rp,
                                    params["ma_weight"], params["rp_weight"])
```

### 组合策略的"可扩展因子"设计

组合策略可以调用框架提供的 `StrategyPerformanceProvider` 来获取子策略的近期表现，用于动态权重调整：

```python
class StrategyPerformanceProvider:
    """
    框架层提供，组合策略可以注入使用。
    在回测模式下返回空（使用固定权重），
    在生产模式下返回实际表现数据。
    """
    def get_recent_performance(self, strategy_id: str,
                                db_type: str,  # "sim" | "live"
                                lookback_days: int = 90
    ) -> Optional[StrategyPerformanceData]:
        """查询某策略在模拟盘/实盘的近期表现"""
        ...
```

组合策略可以选择性使用此接口，实现"自适应权重"。不使用此接口的组合策略就是静态权重。

---

## 三、系统架构总览

```
┌─────────────────────────────────────────────────────────────┐
│  前端 (React + Vite + Tailwind + ECharts)                    │
│  仪表盘 | 实验中心 | 交易工作台 | 数据中心 | 策略管理           │
│  RBAC: 每个页面/操作按钮根据用户权限显示/隐藏                    │
├─────────────────────────────────────────────────────────────┤
│  API网关 (FastAPI)                                           │
│  JWT认证 → RBAC中间件 → 路由分发                              │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────┐ ┌──────────────┐ ┌──────────┐ ┌───────────┐  │
│  │ 实验管理  │ │ 交易管理      │ │ 数据服务  │ │ AI 服务    │  │
│  │ 参数扫描  │ │ 部署/组合/模拟 │ │ AKShare   │ │ DeepSeek   │  │
│  │ 指标计算  │ │ 信号/持仓/订单 │ │ 缓存/日历  │ │ 分析/建议   │  │
│  └─────┬─────┘ └──────┬───────┘ └─────┬────┘ └─────┬─────┘  │
│        │              │               │            │         │
│  ┌─────▼──────────────▼───────────────▼────────────▼─────┐  │
│  │              策略注册中心 (Registry)                     │  │
│  │  扫描/发现 → 元数据索引 → 实例缓存 → 子策略查询          │  │
│  │  原子策略注册  |  组合策略注册  |  能力声明               │  │
│  └────────────────────────┬───────────────────────────────┘  │
│                           │                                  │
│  ┌────────────────────────▼───────────────────────────────┐  │
│  │           执行内核 (纯Python，零框架依赖)                 │  │
│  │  回测引擎 | 成本模型 | 信号→订单 | A股规则 | 36项指标    │  │
│  │  统一处理原子策略 & 组合策略（不区分）                     │  │
│  └────────────────────────────────────────────────────────┘  │
│                           │                                  │
│  ┌────────────────────────▼───────────────────────────────┐  │
│  │           策略算法层 (共享代码，只读引用)                  │  │
│  │  technical/  |  ml/  |  factor/  |  portfolio/          │  │
│  │  composite/  (组合策略，内部引用上述各类策略)             │  │
│  └────────────────────────────────────────────────────────┘  │
│                           │                                  │
│         ┌─────────────────┼─────────────────┐               │
│    ┌────▼─────┐     ┌─────▼──────┐    ┌─────▼──────┐        │
│    │exp.db    │     │ sim.db     │    │ live.db    │        │
│    │实验库    │     │ 模拟交易库  │    │ 实盘库(预留)│        │
│    │只追加    │     │ 可覆盖     │    │ 可覆盖     │        │
│    └──────────┘     └────────────┘    └────────────┘        │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  users.db (独立)  |  RBAC权限表  |  JWT会话             │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  后台任务: 异步实验执行 | 参数扫描 | 每日数据更新        │   │
│  │  定时调度: 每日收盘后重训练(按策略配置) | 每日模拟运行    │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## 四、代码目录结构

```
quant-platform/
│
├── backend/
│   ├── main.py
│   ├── config.py
│   ├── dependencies.py           # FastAPI Depends (auth/perm/db)
│   │
│   ├── api/                      # REST 路由
│   │   ├── auth.py
│   │   ├── strategies.py
│   │   ├── experiments.py
│   │   ├── trading.py
│   │   ├── data.py
│   │   ├── ai.py
│   │   ├── jobs.py
│   │   └── admin.py              # 用户管理/权限分配 (仅admin)
│   │
│   ├── ws/                       # WebSocket
│   │   ├── training.py
│   │   ├── realtime.py
│   │   └── notifications.py
│   │
│   ├── core/                     # 执行内核
│   │   ├── engine.py
│   │   ├── cost_model.py
│   │   ├── rules.py
│   │   ├── metrics.py
│   │   └── types.py
│   │
│   ├── strategies/               # ═══ 策略算法层 ═══
│   │   ├── __init__.py
│   │   ├── base.py               # StrategyProtocol + CompositeStrategy(基类)
│   │   ├── registry.py           # 注册中心 + 策略发现
│   │   ├── performance.py        # StrategyPerformanceProvider
│   │   │
│   │   ├── technical/            # 技术指标策略
│   │   │   ├── ma_cross.py
│   │   │   ├── rsi_reversal.py
│   │   │   ├── bollinger_breakout.py
│   │   │   └── macd_signal.py
│   │   │
│   │   ├── ml/                   # 机器学习策略 (需要训练)
│   │   │   ├── alpha158_lgb.py
│   │   │   ├── alpha158_xgb.py
│   │   │   ├── lstm_rank.py
│   │   │   └── transformer_rank.py
│   │   │
│   │   ├── factor/
│   │   │   └── alphamaster_gbr.py
│   │   │
│   │   ├── portfolio/
│   │   │   └── risk_parity.py
│   │   │
│   │   └── composite/            # ═══ 组合策略 ═══
│   │       ├── equal_weight_combo.py
│   │       ├── risk_parity_combo.py
│   │       ├── momentum_combo.py
│   │       └── ma_risk_parity.py
│   │
│   ├── models/                   # 模型持久化 + 生命周期
│   │   ├── store.py              # .pkl 保存/加载
│   │   ├── metadata.py           # 元数据JSON
│   │   ├── lifecycle.py          # 训练→部署→重训→归档
│   │   └── versioning.py         # 模型版本管理
│   │
│   ├── auth/                     # 认证 + 权限
│   │   ├── jwt_handler.py
│   │   ├── middleware.py         # RBAC中间件
│   │   └── permissions.py        # 权限定义 + 检查逻辑
│   │
│   ├── data/                     # 数据层
│   │   ├── sources/
│   │   │   ├── base.py           # DataSource ABC (支持多分辨率)
│   │   │   ├── akshare_source.py
│   │   │   └── tushare_source.py
│   │   ├── pipeline.py
│   │   ├── cache.py
│   │   ├── calendar.py
│   │   └── universe.py
│   │
│   ├── db/                       # 数据库操作
│   │   ├── base.py
│   │   ├── experiment.py
│   │   ├── trading.py
│   │   ├── user.py
│   │   ├── permissions.py
│   │   └── migrations/
│   │
│   ├── jobs/                     # 后台任务
│   │   ├── broker.py             # 内存任务队列
│   │   ├── tasks.py              # 任务定义
│   │   ├── scheduler.py          # APScheduler 定时任务
│   │   └── worker.py             # Worker循环
│   │
│   └── ai/
│       ├── client.py
│       ├── prompts.py
│       └── cache.py
│
├── frontend/
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   │
│   │   ├── pages/
│   │   │   ├── Dashboard/
│   │   │   ├── ExperimentCenter/
│   │   │   │   ├── ExperimentList.tsx
│   │   │   │   ├── ExperimentNew.tsx      # 含策略选择器（原子/组合分类）
│   │   │   │   ├── ExperimentDetail.tsx
│   │   │   │   ├── ParamSweep.tsx
│   │   │   │   └── Compare.tsx
│   │   │   ├── TradingWorkbench/
│   │   │   │   ├── PortfolioManager.tsx
│   │   │   │   ├── PositionMonitor.tsx
│   │   │   │   ├── SignalPanel.tsx
│   │   │   │   ├── OrderHistory.tsx
│   │   │   │   └── DeploymentPicker.tsx   # 部署选择器弹窗
│   │   │   ├── DataCenter/
│   │   │   ├── StrategyManager/
│   │   │   │   ├── StrategyList.tsx
│   │   │   │   └── StrategyDetail.tsx     # 含"最佳实验"展示区
│   │   │   ├── Admin/                     # 用户管理/权限分配
│   │   │   │   ├── UserManagement.tsx
│   │   │   │   └── PermissionEditor.tsx
│   │   │   └── Auth/
│   │   │
│   │   ├── components/
│   │   │   ├── layout/           # AppShell/Sidebar/Navbar (根据权限显示菜单)
│   │   │   ├── charts/           # ECharts封装
│   │   │   ├── ai/               # AI面板组件
│   │   │   ├── strategy/
│   │   │   │   ├── StrategySelector.tsx       # 策略选择卡片
│   │   │   │   ├── SubStrategyDisplay.tsx     # 组合策略子策略展示
│   │   │   │   └── ParamForm.tsx              # 参数表单
│   │   │   ├── experiment/
│   │   │   │   ├── StarLabel.tsx             # 星标/标签组件
│   │   │   │   └── DeploymentPickerModal.tsx  # 部署选择弹窗
│   │   │   ├── job/              # 任务进度组件
│   │   │   └── shared/           # 通用UI组件
│   │   │
│   │   ├── hooks/
│   │   ├── services/             # API客户端
│   │   ├── store/                # Zustand状态
│   │   └── types/                # TypeScript类型
│   │
│   └── package.json
│
├── data/                         # 运行时数据 (gitignore)
│   ├── cache/                    # Parquet缓存 (所有用户共享)
│   └── models/
│       ├── exp/{strategy}/{exp_id}/
│       │   ├── model_v1.pkl
│       │   └── metadata.json
│       ├── sim/{strategy}/{deploy_id}/
│       │   ├── model_v1.pkl      # 从exp复制
│       │   ├── model_v2.pkl      # 重训练产物
│       │   └── metadata.json
│       └── live/ (预留)
│
├── requirements.txt
└── README.md
```

---

## 五、策略接口设计（V3）

### 5.1 统一策略基类

```python
# backend/strategies/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
import pandas as pd


# ═══════════════════════════════
# 枚举与类型
# ═══════════════════════════════

class StrategyCategory(str, Enum):
    TECHNICAL = "technical"
    ML = "ml"
    FACTOR = "factor"
    PORTFOLIO = "portfolio"
    COMPOSITE = "composite"       # ⭐ V3新增


class StrategyMode(str, Enum):
    BATCH = "batch"
    REALTIME = "realtime"


class RetrainFrequency(str, Enum):
    NEVER = "never"               # 不需要重训练
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


@dataclass
class SubStrategyRef:
    """组合策略对子策略的引用"""
    strategy_id: str              # 子策略的注册ID
    role: str                     # 在组合中的角色（如"趋势跟踪主力"）
    params_override: dict = field(default_factory=dict)  # 参数覆写


@dataclass
class ParamField:
    name: str
    type: str                     # "int" | "float" | "str" | "bool" | "choice"
    default: Any
    description: str
    required: bool = True
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    choices: Optional[list] = None


@dataclass
class StrategyMetadata:
    """策略元数据"""
    strategy_id: str
    display_name: str
    version: str
    category: StrategyCategory
    description: str              # ⭐ 策略原理自述

    # 能力声明
    supported_modes: list[StrategyMode] = field(default_factory=lambda: [StrategyMode.BATCH])
    requires_training: bool = False
    retrain_frequency: RetrainFrequency = RetrainFrequency.NEVER  # ⭐ V3新增
    estimated_training_seconds: int = 60

    # 参数
    params: list[ParamField] = field(default_factory=list)

    # 仓位
    max_position_pct: float = 0.05
    supported_position_modes: list[str] = field(default_factory=lambda: ["equal_weight"])

    # ⭐ V3新增：组合策略专属
    sub_strategies: list[SubStrategyRef] = field(default_factory=list)
    integration_method: str = ""   # 整合方式描述

    # 标签
    tags: list[str] = field(default_factory=list)


# ═══════════════════════════════
# 核心类型
# ═══════════════════════════════

@dataclass
class SignalItem:
    code: str
    action: str                   # "BUY" | "SELL" | "HOLD"
    score: float
    weight: float = 0.0


SignalDict = dict[str, list[SignalItem]]  # {date: [SignalItem, ...]}

@dataclass
class RealtimeSignal:
    code: str
    action: str
    score: float
    confidence: float
    reasoning: str = ""

@dataclass
class TrainedModel:
    model: Any
    feature_importance: Optional[dict] = None
    train_metrics: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


# ═══════════════════════════════
# 策略基类
# ═══════════════════════════════

class StrategyProtocol(ABC):
    """所有策略（原子/组合）的统一接口"""

    @classmethod
    @abstractmethod
    def metadata(cls) -> StrategyMetadata:
        ...

    def on_register(self) -> None:
        pass

    def on_unregister(self) -> None:
        pass

    def validate_params(self, params: dict) -> tuple[bool, str]:
        return True, ""

    def prepare_data(self, pivot: pd.DataFrame, params: dict) -> pd.DataFrame:
        return pivot

    @abstractmethod
    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict,
        start_date: str, end_date: str
    ) -> SignalDict:
        ...

    def generate_realtime_signal(
        self, market_snapshot: pd.DataFrame, params: dict
    ) -> RealtimeSignal:
        raise NotImplementedError

    # ⭐ V3新增：训练+重训练支持
    def train(
        self, pivot: pd.DataFrame, params: dict,
        train_start: str, train_end: str,
        progress_callback: Optional[callable] = None,
        existing_model: Optional[Any] = None  # 增量训练（重训练时传入旧模型）
    ) -> TrainedModel:
        raise NotImplementedError

    def load_model(self, path: str) -> Any:
        import joblib
        return joblib.load(path)

    def save_model(self, model: Any, path: str) -> None:
        import joblib
        joblib.dump(model, path)
```

### 5.2 组合策略基类

```python
# backend/strategies/base.py (续)

class CompositeStrategy(StrategyProtocol, ABC):
    """
    组合策略基类。

    提供:
    - self._get_sub_strategy(strategy_id) → 获取子策略实例
    - self._merge_signals(signals_list, weights) → 信号合并工具

    子类只需实现 generate_batch_signals，内部调用子策略即可。
    """

    def __init__(self):
        self._sub_instances: dict[str, StrategyProtocol] = {}

    def _get_sub_strategy(self, strategy_id: str) -> StrategyProtocol:
        """获取子策略实例（从注册中心懒加载）"""
        if strategy_id not in self._sub_instances:
            from .registry import get_registry
            registry = get_registry()
            self._sub_instances[strategy_id] = registry.get_strategy(strategy_id)
        return self._sub_instances[strategy_id]

    def _merge_signals(
        self,
        signals_list: list[SignalDict],
        weights: list[float]
    ) -> SignalDict:
        """
        合并多个子策略的信号。

        逻辑:
        1. 收集所有日期
        2. 对每个日期，合并所有子策略的信号
        3. 按权重分配 score
        4. 同股票、同方向信号合并（取最大score）
        """
        # ...实现...
        pass
```

### 5.3 组合策略：动量加权示例

```python
# backend/strategies/composite/momentum_combo.py

class MomentumWeightedCombo(CompositeStrategy):
    """
    动量加权组合策略。

    根据子策略近期表现动态调整权重：
    - 近3月Sharpe最高的策略获得最高权重
    - 权重按Sharpe排名归一化

    使用框架的 StrategyPerformanceProvider 获取表现数据。
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="momentum_weighted_combo_v1",
            display_name="动量加权多策略组合",
            version="1.0.0",
            category=StrategyCategory.COMPOSITE,
            description=(
                "动态监测各子策略近期表现，按Sharpe比率排名分配权重。"
                "表现好的策略自动增加权重，表现差的策略自动降低权重。"
            ),
            sub_strategies=[
                SubStrategyRef("ma_cross_v1", "趋势跟踪"),
                SubStrategyRef("macd_signal_v1", "MACD金叉"),
                SubStrategyRef("risk_parity_v1", "风控底仓"),
            ],
            integration_method=(
                "1. 计算各子策略近3月Sharpe\n"
                "2. 按Sharpe排名，排名越高权重越大\n"
                "3. 权重归一化：w_i = sharpe_i / sum(sharpe)\n"
                "4. 负Sharpe的策略权重为0"
            ),
            params=[
                ParamField("lookback_days", "int", 90,
                    description="表现回溯天数", min=30, max=365),
                ParamField("min_weight", "float", 0.1,
                    description="最小权重（保底）", min=0.0, max=0.3, step=0.05),
                ParamField("sub_params", "json", {},
                    description="各子策略的参数覆写"),
            ],
            tags=["自适应", "动量", "多策略"],
        )

    def generate_batch_signals(self, pivot, params, start_date, end_date):
        from ..performance import StrategyPerformanceProvider

        provider = StrategyPerformanceProvider()
        lookback = params.get("lookback_days", 90)
        min_weight = params.get("min_weight", 0.1)

        # 获取子策略近期表现
        performances = {}
        for sub in self.metadata().sub_strategies:
            perf = provider.get_recent_performance(
                sub.strategy_id, db_type="sim", lookback_days=lookback
            )
            performances[sub.strategy_id] = perf

        # 计算权重
        weights = self._compute_momentum_weights(performances, min_weight)

        # 生成各子策略信号
        all_signals = []
        for sub in self.metadata().sub_strategies:
            strategy = self._get_sub_strategy(sub.strategy_id)
            sub_params = params.get("sub_params", {}).get(sub.strategy_id, {})
            signals = strategy.generate_batch_signals(pivot, sub_params, start_date, end_date)
            all_signals.append(signals)

        # 合并
        return self._merge_signals(all_signals, list(weights.values()))

    def _compute_momentum_weights(self, performances: dict, min_weight: float) -> dict:
        """根据Sharpe计算动量权重"""
        sharpes = {}
        for sid, perf in performances.items():
            if perf and perf.sharpe_3m and perf.sharpe_3m > 0:
                sharpes[sid] = perf.sharpe_3m
            else:
                sharpes[sid] = 0.0

        total = sum(sharpes.values())
        if total == 0:
            n = len(sharpes)
            return {sid: 1.0/n for sid in sharpes}

        weights = {sid: max(s/total, min_weight) for sid, s in sharpes.items()}
        # 重新归一化
        total_w = sum(weights.values())
        return {sid: w/total_w for sid, w in weights.items()}
```

### 5.4 注册中心（V3更新）

```python
# backend/strategies/registry.py

class StrategyRegistry:
    """
    V3 更新:
    - 支持按分类查询（含 composite）
    - 支持查询某策略的所有子策略 / 被哪些组合策略引用
    - 提供 get_strategy() 供组合策略内部调用
    """

    def list_by_category(self, category: StrategyCategory) -> list[StrategyMetadata]:
        """按分类列出策略"""
        ...

    def get_sub_strategies(self, strategy_id: str) -> list[SubStrategyRef]:
        """获取某组合策略的子策略列表"""
        ...

    def get_parent_strategies(self, strategy_id: str) -> list[str]:
        """获取引用了某策略的所有组合策略"""
        ...

    def get_performance_provider(self) -> 'StrategyPerformanceProvider':
        """获取表现数据提供者（框架注入）"""
        ...
```

---

## 六、多用户权限系统（RBAC）

### 6.1 设计原则

| 规则 | 说明 |
|------|------|
| 数据共享 | 所有用户共享同一套实验、交易、数据 |
| 权限控制 | 基于角色 + 模块级细粒度权限 |
| 默认只读 | 除首位管理员外，所有新用户默认只读 |
| 管理员分发 | 管理员逐用户、逐模块授予操作权限 |

### 6.2 权限定义

```python
# backend/auth/permissions.py

from enum import Enum

class Permission(str, Enum):
    """模块级权限"""

    # 实验
    EXP_READ = "experiments:read"         # 查看实验
    EXP_CREATE = "experiments:create"     # 创建实验
    EXP_DELETE = "experiments:delete"     # 删除实验
    EXP_SWEEP = "experiments:sweep"       # 参数扫描

    # 交易
    TRADE_READ = "trading:read"           # 查看持仓/信号/订单
    TRADE_DEPLOY = "trading:deploy"       # 部署/修改部署
    TRADE_EXECUTE = "trading:execute"     # 执行模拟交易
    TRADE_REBALANCE = "trading:rebalance" # 触发再平衡

    # 数据
    DATA_READ = "data:read"               # 查看数据
    DATA_UPDATE = "data:update"           # 触发数据更新

    # 策略
    STRATEGY_READ = "strategies:read"     # 查看策略
    STRATEGY_SCAN = "strategies:scan"     # 扫描/热加载策略

    # AI
    AI_USE = "ai:use"                     # 使用AI分析

    # 管理
    ADMIN_USERS = "admin:users"           # 管理用户和权限


# 预定义角色
ROLE_PERMISSIONS = {
    "admin": [p.value for p in Permission],  # 所有权限
    "operator": [                             # 操作员
        Permission.EXP_READ, Permission.EXP_CREATE, Permission.EXP_SWEEP,
        Permission.TRADE_READ, Permission.TRADE_DEPLOY, Permission.TRADE_EXECUTE,
        Permission.DATA_READ, Permission.DATA_UPDATE,
        Permission.STRATEGY_READ, Permission.STRATEGY_SCAN,
        Permission.AI_USE,
    ],
    "viewer": [                               # 只读（默认）
        Permission.EXP_READ,
        Permission.TRADE_READ,
        Permission.DATA_READ,
        Permission.STRATEGY_READ,
    ],
}
```

### 6.3 用户数据模型

```sql
-- users.db

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    email TEXT,
    is_admin INTEGER DEFAULT 0,         -- 首位注册用户 = 1，其余 = 0
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE TABLE user_permissions (
    user_id INTEGER NOT NULL,
    permission TEXT NOT NULL,            -- 如 "experiments:create"
    granted_by INTEGER,                 -- 授权人
    granted_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, permission),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_jti TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### 6.4 RBAC 中间件

```python
# backend/auth/middleware.py

from functools import wraps
from fastapi import HTTPException, Depends

def require_permission(permission: Permission):
    """装饰器：检查当前用户是否有指定权限"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, current_user = Depends(get_current_user), **kwargs):
            if not current_user.has_permission(permission):
                raise HTTPException(403, f"需要权限: {permission.value}")
            return await func(*args, **kwargs)
        return wrapper
    return decorator

# 使用示例
@router.post("/api/experiments")
@require_permission(Permission.EXP_CREATE)
async def create_experiment(...):
    ...
```

### 6.5 管理API（仅admin）

```
GET    /api/admin/users                    # 用户列表
POST   /api/admin/users                    # 创建用户
PUT    /api/admin/users/:id/permissions    # 设置权限
DELETE /api/admin/users/:id                # 删除用户
```

---

## 七、数据库设计（V3）

### 7.1 实验库 experiment.db（更新）

```sql
-- 实验表（新增字段）
CREATE TABLE experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,              -- 创建者（审计用，不影响可见性）

    name TEXT,
    strategy_id TEXT NOT NULL,
    strategy_category TEXT NOT NULL,        -- ⭐ V3新增: 原子/组合

    -- ⭐ V3新增: 实验标注
    is_starred INTEGER DEFAULT 0,           -- 星标
    labels TEXT,                            -- JSON数组: ["表现最佳", "低回撤"]

    pool_preset TEXT,
    pool_custom_codes TEXT,
    pool_industries TEXT,

    train_start TEXT,
    train_end TEXT,
    test_start TEXT NOT NULL,
    test_end TEXT NOT NULL,

    params TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    mode TEXT DEFAULT 'batch',

    -- ⭐ V3新增: 训练相关
    requires_training INTEGER DEFAULT 0,
    retrain_frequency TEXT,                 -- "never"|"daily"|"weekly"|"monthly"|"quarterly"

    status TEXT DEFAULT 'pending',
    error_log TEXT,
    ai_diagnosis TEXT,

    progress_pct REAL DEFAULT 0,
    progress_message TEXT,

    data_version TEXT,
    code_version TEXT,

    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

-- 其余表 (experiment_metrics, equity_curve, trade_log) 保持不变

-- 模型产物表（更新）
CREATE TABLE model_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    model_version INTEGER DEFAULT 1,        -- ⭐ V3新增: 模型版本号
    model_file_path TEXT NOT NULL,
    metadata_file_path TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    train_window_start TEXT,
    train_window_end TEXT,
    feature_count INTEGER,
    train_samples INTEGER,
    train_metrics TEXT,
    feature_importance TEXT,
    is_latest INTEGER DEFAULT 1,            -- ⭐ V3新增: 是否最新版本
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

-- 可复用参数方案：独立于来源实验保存完整参数和评价快照
CREATE TABLE parameter_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    params TEXT NOT NULL,
    mode TEXT NOT NULL,
    pool_preset TEXT NOT NULL,
    pool_custom_codes TEXT,
    pool_industries TEXT,
    source_experiment_id INTEGER,
    metrics_snapshot TEXT,
    notes TEXT,
    labels TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, strategy_id, name)
);
```

### 7.2 交易库 trading_sim.db / trading_live.db（更新）

```sql
-- 部署表（更新）
CREATE TABLE deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,

    strategy_id TEXT NOT NULL,
    strategy_category TEXT NOT NULL,         -- ⭐ V3新增
    display_name TEXT,
    params TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    mode TEXT DEFAULT 'batch',

    source_experiment_id INTEGER,           -- 来源实验
    source_model_artifact_id INTEGER,       -- ⭐ V3新增: 具体模型版本

    -- ⭐ V3新增: 重训练配置
    requires_retraining INTEGER DEFAULT 0,
    retrain_frequency TEXT,                  -- "never"|"daily"|"weekly"|"monthly"|"quarterly"
    last_retrain_at TEXT,                    -- 上次重训练时间
    current_model_version INTEGER DEFAULT 1, -- 当前模型版本
    current_model_path TEXT,                 -- 当前模型文件路径

    position_mode TEXT DEFAULT 'equal_weight',
    position_config TEXT,

    status TEXT DEFAULT 'active',
    status_tags TEXT,                        -- JSON数组
    user_notes TEXT,

    deployed_at TEXT DEFAULT (datetime('now')),
    last_signal_at TEXT,
    last_rebalance_at TEXT,
    stopped_at TEXT,

    created_at TEXT DEFAULT (datetime('now'))
);

-- 每个组合策略的独立资金袖套账本；net_flow 剔除内部调拨对收益率的影响
CREATE TABLE strategy_nav_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    deployment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    opening_equity REAL NOT NULL,
    net_flow REAL NOT NULL DEFAULT 0,
    cash_balance REAL NOT NULL,
    market_value REAL NOT NULL,
    total_equity REAL NOT NULL,
    daily_pnl REAL NOT NULL,
    daily_return REAL,
    cumulative_return REAL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    transaction_cost REAL NOT NULL DEFAULT 0,
    turnover REAL NOT NULL DEFAULT 0,
    contribution_pnl REAL NOT NULL DEFAULT 0,
    contribution_return REAL,
    simulation_run_id TEXT,
    UNIQUE(portfolio_id, deployment_id, date)
);

-- 其余表 (portfolios, portfolio_versions, portfolio_allocations,
-- daily_signals, position_snapshots, orders, nav_history) 保持不变
```

---

## 八、模型生命周期管理

### 8.1 生命周期状态机

```
         ┌─────────────┐
         │  实验训练    │  ← experiment.db
         │  model_v1   │
         └──────┬──────┘
                │ 发布 (复制)
                ▼
         ┌─────────────┐
         │  模拟盘部署  │  ← trading_sim.db
         │  model_v1   │
         └──────┬──────┘
                │
        ┌───────┼───────┐
        │               │
        ▼               ▼
   ┌─────────┐    ┌─────────────┐
   │ 手动触发 │    │ 定时重训练   │  ← scheduler
   │ 重训练   │    │ (按frequency) │
   └────┬────┘    └──────┬──────┘
        │               │
        └───────┬───────┘
                ▼
         ┌─────────────┐
         │ model_v2    │  ← 新模型保存到 sim/{strategy}/{deploy_id}/
         │ 替换v1      │
         └──────┬──────┘
                │
         ┌──────▼──────┐
         │ model_v1    │  ← 归档 (保留，标记 is_latest=0)
         │ model_v2    │  ← 当前 (is_latest=1)
         └─────────────┘
```

### 8.2 模型文件路径规范

```
data/models/
├── exp/{strategy_id}/{experiment_id}/
│   └── model_v{version}_{date}.pkl      # experiment.db 的 model_artifacts 记录
│       metadata.json
│
├── sim/{strategy_id}/{deployment_id}/
│   ├── model_v1_2026-07-27.pkl          # 首次部署 (从 exp 复制)
│   ├── model_v2_2026-08-27.pkl          # 第一次重训练
│   ├── model_v3_2026-09-27.pkl          # 第二次重训练
│   └── metadata.json                    # 当前元数据
│
└── live/{strategy_id}/{deployment_id}/   # 预留
```

### 8.3 重训练调度

```python
# backend/jobs/scheduler.py

from apscheduler.schedulers.asyncio import AsyncIOScheduler

class RetrainingScheduler:
    """
    每日收盘后检查所有需要重训练的部署。
    """

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        # 每日 15:30 触发（收盘后30分钟，确保数据已更新）
        self.scheduler.add_job(
            self.check_and_retrain,
            'cron', day_of_week='mon-fri', hour=15, minute=30
        )

    async def check_and_retrain(self):
        """检查所有需要重训练的部署"""
        deployments = await self.db.get_retrain_due_deployments()
        for dep in deployments:
            await self.retrain_deployment(dep)

    async def retrain_deployment(self, deployment):
        """
        重训练流程:
        1. 获取最新数据
        2. 用当前参数重新训练模型
        3. 保存新模型 (版本号+1)
        4. 更新 deployment.current_model_path
        5. 发送通知给用户
        """
        ...
```

---

## 九、实验标注与部署选择器

### 9.1 实验标注

```python
# API: 标注实验
PUT /api/experiments/:id/star
  Body: { is_starred: true }
  → { updated: true }

PUT /api/experiments/:id/labels
  Body: { labels: ["表现最佳", "低回撤", "CSI500专用"] }
  → { updated: true }
```

### 9.2 策略详情页"最佳实验"

策略详情页 `/strategies/:id` 底部增加一个"最佳实验"区块：

```
┌──────────────────────────────────────────┐
│  🏆 该策略的最佳实验                       │
│  ┌────────────────────────────────────┐  │
│  │ ⭐ CSI500+月频调仓  Sharpe:1.41     │  │
│  │ 标签: 表现最佳, 低回撤               │  │
│  │ 2024-01-02 ~ 2026-06-30             │  │
│  │ [查看详情] [对比]                   │  │
│  └────────────────────────────────────┘  │
│  ┌────────────────────────────────────┐  │
│  │ ⭐ CSI800日频  Sharpe:0.97          │  │
│  │ ...                                 │  │
│  └────────────────────────────────────┘  │
└──────────────────────────────────────────┘
```

### 9.3 部署选择器（DeploymentPickerModal）

当用户部署策略到模拟盘/实盘时，弹出档案选择器：

```
┌──────────────────────────────────────────────┐
│  选择部署来源                                  │
│                                              │
│  ┌─ 筛选 ──────────────────────────────────┐ │
│  │ [⭐仅显示标注] [策略: ma_cross_v1 ▼]      │ │
│  │ [排序: Sharpe ▼]                         │ │
│  └──────────────────────────────────────────┘ │
│                                              │
│  ┌──────────────────────────────────────────┐ │
│  │ ⭐ Exp #42  CSI500月频  Sharpe 1.41  [选择] │
│  │ ⭐ Exp #38  CSI800日频  Sharpe 1.24  [选择] │
│  │    Exp #25  CSI500日频  Sharpe 0.97  [选择] │
│  │    Exp #12  CSI500周频  Sharpe 0.54  [选择] │
│  └──────────────────────────────────────────┘ │
│                                              │
│  ┌─ 或手动配置 ────────────────────────────┐ │
│  │ 手动输入参数（不使用已有实验结果）         │ │
│  └──────────────────────────────────────────┘ │
│                                              │
│                        [取消]  [确认部署]      │
└──────────────────────────────────────────────┘
```

---

## 十、API 接口设计（V3）

### 10.1 实验（新增/变更）

```
GET    /api/experiments?starred=true&label=低回撤    # ⭐ 按标注筛选

PUT    /api/experiments/:id/star                     # ⭐ 切换星标
  Body:  { is_starred: true }

PUT    /api/experiments/:id/labels                   # ⭐ 设置标签
  Body:  { labels: ["best", "low_dd"] }

GET    /api/experiments/picker                       # ⭐ 部署选择器数据
  参数: ?strategy_id=ma_cross_v1&starred_only=true&sort=sharpe
  → [{ id, name, starred, labels, sharpe, max_drawdown, params }, ...]
```

### 10.2 策略（新增/变更）

```
GET    /api/strategies?category=composite            # ⭐ 按分类筛选

GET    /api/strategies/:id/sub-strategies            # ⭐ 组合策略的子策略
GET    /api/strategies/:id/parent-strategies         # ⭐ 被哪些组合引用
GET    /api/strategies/:id/best-experiments          # ⭐ 该策略的最佳实验
```

### 10.3 交易（新增/变更）

```
POST   /api/trading/deployments
  Body:  {
    strategy_id, display_name,
    source_experiment_id,            # 从实验发布
    source_model_artifact_id,        # ⭐ 指定模型版本
    retrain_frequency: "monthly",    # ⭐ 重训练频率
    ...
  }

PUT    /api/trading/deployments/:id/retrain          # ⭐ 手动触发重训练
  → { job_id }

GET    /api/trading/deployments/:id/models           # ⭐ 该部署的模型版本历史
  → [{ version, created_at, train_metrics, is_latest }, ...]
```

### 10.4 管理（新增）

```
GET    /api/admin/users                              # 用户列表
POST   /api/admin/users                              # 创建用户
PUT    /api/admin/users/:id/permissions              # ⭐ 设置权限
DELETE /api/admin/users/:id                          # 删除用户
GET    /api/admin/permissions                        # 可用权限列表
```

### 10.5 完整API汇总

```
# Auth
POST   /api/auth/register
POST   /api/auth/login
POST   /api/auth/refresh
GET    /api/auth/me

# Strategies
GET    /api/strategies                    # ?category=technical|ml|factor|portfolio|composite
POST   /api/strategies/scan               # 扫描热加载
GET    /api/strategies/:id                # 策略详情
GET    /api/strategies/:id/sub-strategies # 子策略
GET    /api/strategies/:id/parent-strategies
GET    /api/strategies/:id/best-experiments
POST   /api/strategies/:id/validate

# Experiments
GET    /api/experiments                   # ?starred&label&strategy_id&status
POST   /api/experiments
GET    /api/experiments/:id
DELETE /api/experiments/:id
GET    /api/experiments/:id/metrics
GET    /api/experiments/:id/equity
GET    /api/experiments/:id/trades
GET    /api/experiments/:id/models        # 模型产物列表
PUT    /api/experiments/:id/star
PUT    /api/experiments/:id/labels
GET    /api/experiments/picker            # 部署选择器
POST   /api/experiments/sweep
GET    /api/experiments/sweep/:id
POST   /api/experiments/compare

# Trading (Simulation)
GET    /api/trading/deployments
POST   /api/trading/deployments
PUT    /api/trading/deployments/:id
DELETE /api/trading/deployments/:id
PUT    /api/trading/deployments/:id/retrain       # 手动重训练
GET    /api/trading/deployments/:id/models        # 模型版本历史
GET    /api/trading/portfolios
POST   /api/trading/portfolios
PUT    /api/trading/portfolios/:id
POST   /api/trading/portfolios/:id/validate
POST   /api/trading/portfolios/:id/preview
POST   /api/trading/portfolios/:id/drafts
POST   /api/trading/portfolios/:id/drafts/:revision/publish
GET    /api/trading/portfolios/:id/versions
GET    /api/trading/portfolios/:id/nav
GET    /api/trading/positions
GET    /api/trading/signals
GET    /api/trading/orders
POST   /api/trading/simulate/run
GET    /api/trading/simulate/status
GET    /api/trading/simulate/runs

# Data
GET    /api/data/pools
GET    /api/data/pools/:id/stocks
GET    /api/data/industries
POST   /api/data/industries/refresh
POST   /api/data/point-in-time/imports
POST   /api/data/point-in-time/governance/artifacts
POST   /api/data/point-in-time/governance/packages
GET    /api/data/point-in-time/governance/packages/:id
GET    /api/data/point-in-time/governance/packages/:id/events
POST   /api/data/point-in-time/governance/packages/:id/decision
POST   /api/data/point-in-time/governance/packages/:id/import
GET    /api/data/point-in-time/as-of
GET    /api/data/point-in-time/coverage
GET    /api/data/price-ledger/import-contract
POST   /api/data/price-ledger/imports
GET    /api/data/price-ledger/readiness
GET    /api/data/price-ledger/prices
GET    /api/data/stocks/:code
GET    /api/data/stocks/batch
POST   /api/data/update
GET    /api/data/update/status
POST   /api/data/cache/invalidate

# Factor Research
GET    /api/factor-research/catalog
GET    /api/factor-research/readiness
GET    /api/factor-research/protocols
POST   /api/factor-research/protocols
POST   /api/factor-research/protocols/:id/versions
POST   /api/factor-research/protocols/:id/versions/:version/lock
POST   /api/factor-research/jobs
POST   /api/factor-research/analyze
GET    /api/factor-research/runs
GET    /api/factor-research/runs/:id
GET    /api/factor-research/runs/:id/export
DELETE /api/factor-research/runs/:id
POST   /api/factor-research/compare
POST   /api/factor-research/export-strategy

# AI
POST   /api/ai/analyze-backtest
POST   /api/ai/suggest-params
POST   /api/ai/market-insight
POST   /api/ai/diagnose-error
POST   /api/ai/explain-signal

# Jobs
GET    /api/jobs
GET    /api/jobs/summary
GET    /api/jobs/:id
DELETE /api/jobs/:id
POST   /api/jobs/:id/retry

# Admin (仅admin)
GET    /api/admin/users
POST   /api/admin/users
PUT    /api/admin/users/:id/permissions
DELETE /api/admin/users/:id
GET    /api/admin/permissions

# WebSocket
WS     /ws/training/:experiment_id
WS     /ws/realtime/:deployment_id
WS     /ws/notifications
WS     /ws/jobs
```

WebSocket 鉴权不使用查询字符串：连接建立后 5 秒内的第一条数据帧必须是
`{"type":"authenticate","token":"<access JWT>"}`。后端在返回
`{"type":"authenticated"}` 之前完成 JWT 类型、活跃用户、RBAC 和资源 owner
检查，未认证连接不会注册到广播管理器或资源队列。URL 中出现 `token`、
`access_token` 或 `authorization` 参数时直接失败关闭；日志过滤器还会对这些参数
做纵深脱敏。前后端应作为同一发布单元部署，旧页面刷新后继续使用原 access token，
无需重新登录。

---

## 十一、前端页面设计（V3）

### 调整重点

| 页面 | V2 | V3调整 |
|------|-----|--------|
| 侧边栏 | 固定菜单 | 根据用户权限动态显示/隐藏菜单项 |
| 策略列表 | 统一列表 | 新增"组合策略"分类Tab |
| 策略详情 | 基础信息 | + "最佳实验"区块 + "子策略/被引用"区块 |
| 新建实验 | 策略选择 | 策略卡片区分原子/组合，组合策略展示子策略 |
| 部署 | 手动输ID | 弹出 DeploymentPickerModal（标注优先） |
| 管理 | 无 | 新增 /admin 用户管理+权限编辑页面 |
| 任务中心 | 无 | 资源感知 1↔2 槽调度、优先队列、租约恢复、进度时间线、取消与重试 |

### 策略选择器（新建实验/部署时）

```
┌──────────────────────────────────────────────────────┐
│  选择策略                                             │
│                                                      │
│  [全部] [技术指标] [机器学习] [因子] [组合策略]  ←分类Tab│
│                                                      │
│  ┌──────────────────┐ ┌──────────────────┐          │
│  │ 📈 双均线交叉     │ │ 🧠 Alpha158+LGB   │          │
│  │ 技术指标 | 批量模式 │ │ ML | 需要训练     │          │
│  │ 趋势跟踪经典策略   │ │ 158因子+LGB排序   │          │
│  └──────────────────┘ └──────────────────┘          │
│                                                      │
│  ┌──────────────────────────────────────┐            │
│  │ 🔗 均线趋势+风险平价组合  [组合策略]   │ ← 特殊徽章  │
│  │ 子策略: MA Cross + Risk Parity       │            │
│  │ 整合: 60%/40% 月度再平衡              │            │
│  └──────────────────────────────────────┘            │
└──────────────────────────────────────────────────────┘
```

### 用户管理页面 `/admin`（仅admin可见）

```
┌──────────────────────────────────────────────────────┐
│  用户管理                                             │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │ 用户名    | 角色 | 权限数 | 状态 | 操作           │ │
│  │ admin     | 管理员| 全部   | 活跃 | [编辑权限]     │ │
│  │ zhangsan  | 操作员| 8项    | 活跃 | [编辑权限] [禁用] │
│  │ lisi      | 只读  | 3项    | 活跃 | [编辑权限] [禁用] │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  [+ 创建新用户]                                       │
│                                                      │
│  ┌─ 权限编辑弹窗（点击"编辑权限"）──────────────────┐  │
│  │ 用户: zhangsan                                   │  │
│  │                                                  │  │
│  │ ☑ experiments:read    ☑ experiments:create      │  │
│  │ ☑ experiments:sweep   ☐ experiments:delete      │  │
│  │ ☑ trading:read        ☑ trading:deploy          │  │
│  │ ☑ trading:execute     ☑ trading:rebalance       │  │
│  │ ☑ data:read           ☑ data:update             │  │
│  │ ☑ strategies:read     ☐ strategies:scan         │  │
│  │ ☑ ai:use              ☐ admin:users             │  │
│  │                                                  │  │
│  │ [快速预设: 只读 | 操作员]  [保存]                 │  │
│  └──────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

---

## 十二、策略重训练调度

### 12.1 调度架构

```
APScheduler (后端进程内，到期扫描)
│
├── 读取 active 且 requires_retraining=1 的部署
├── 由 deployed_at / last_retrain_at + frequency 计算日历到期时间
├── 失败候选遵守冷却时间，活动任务按 deployment 原子去重
└── 只提交 retrain 到持久化队列
      │
      └── 统一资源调度器按本机负载决定何时执行

手动触发使用完全相同的持久化任务、权限和去重链路。
```

### 12.2 重训练任务执行

1. 根据来源实验的数据版本、股票池、行业和参数身份重建训练上下文。
2. 将可见历史切分为训练窗、embargo 和独立验证窗。
3. 训练候选并执行样本数、有限数值与 RankIC 门禁。
4. 写入私有候选文件，计算字节长度、SHA-256 和规范
   `model-retrain-manifest/v1`。
5. 在 SQLite 事务中以当前版本 CAS 晋级；文件目标必须不存在。
6. 晋级后模型与元数据文件在 POSIX 主机上设为只读，信号加载前仍重新校验字节。
7. 任一步失败时清理候选、记录失败尝试并保持原冠军。

### 12.3 生命周期收口与安全边界

- APScheduler 只负责到期扫描；自动与人工提交均按 `deployment` 对活动任务原子
  去重，训练仍受统一资源调度器控制。
- 候选使用独立验证窗和至少一个交易日 embargo；RankIC 门禁、规范清单、
  SHA-256 与字节长度全部通过后才可晋级。
- 训练、验证、文件移动或并发晋级任一失败时，当前冠军保持不变；失败尝试单独
  落库供审计并进入有界冷却。
- `/trading/models` 汇总调度、版本与失败证据。API 不返回绝对路径，也不包含
  自动实盘发布或隐式回滚入口。

---

## 附录：与 V2 的差异速查

| 模块 | V2 | V3 |
|------|-----|-----|
| 组合策略 | 独立协调层 + 权重表 | 组合策略 = 策略子类，与原子策略平行 |
| 多用户 | 数据隔离 + user_id | 数据共享 + RBAC权限 |
| 权限 | 无 | 模块级细粒度，管理员分发 |
| 实验标注 | 无 | Star + Label，策略详情页展示 |
| 部署方式 | 手动输入实验ID | 弹出选择器，标注优先，支持手动 |
| 模型生命周期 | 单次训练 | 周期性重训练 + 版本归档 |
| 策略模式 | batch/realtime | batch/realtime + retrain_frequency |
| 数据精度 | 仅日线 | 日线为主，分钟级接口预留 |
