# 量化验证平台 V2 — 完整架构设计

> **历史设计，未作为当前契约。** 当前实现见
> [ARCHITECTURE_V3.md](ARCHITECTURE_V3.md)，任务规划见 [ROADMAP.md](ROADMAP.md)。

> 版本: v0.1 | 日期: 2026-07-27 | 状态: 设计阶段

---

## 目录

1. [系统分层总览](#一系统分层总览)
2. [代码目录结构](#二代码目录结构)
3. [策略注册接口（契约设计）](#三策略注册接口)
4. [数据库设计（三库隔离）](#四数据库设计)
5. [多策略协调层设计](#五多策略协调层)
6. [API 接口设计](#六api-接口设计)
7. [WebSocket 设计](#七websocket-设计)
8. [前端页面设计](#八前端页面设计)
9. [AI 嵌入设计](#九ai-嵌入设计)
10. [待决策问题](#十待决策问题)

---

## 一、系统分层总览

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 6  前端 (React + Tailwind)                            │
│  仪表盘 | 实验中心 | 交易工作台 | 数据中心 | 策略管理 | AI面板 │
├─────────────────────────────────────────────────────────────┤
│  Layer 5  API网关 (FastAPI)                                  │
│  REST路由 | WebSocket | JWT认证中间件 | 多用户隔离             │
├───────────┬─────────────┬────────────┬──────────────────────┤
│  Layer 4  │  协调层      │  AI服务层   │  数据服务层           │
│           │  权重优化    │  DeepSeek   │  AKShare管道          │
│           │  表现跟踪    │  分析/建议   │  缓存/版本控制         │
├───────────┴─────────────┴────────────┴──────────────────────┤
│  Layer 3  业务层                                             │
│  实验管理 | 模拟交易 | 仓位适配 | 参数扫描 | 模型存储       │
├─────────────────────────────────────────────────────────────┤
│  Layer 2  执行内核 (纯Python，零框架依赖)                      │
│  回测引擎 | 成本模型 | 信号→订单 | T+1/整手/涨跌停规则         │
├─────────────────────────────────────────────────────────────┤
│  Layer 1  策略算法层 (共享代码)                                │
│  base.py → 技术策略 / ML策略 / 因子策略 / 组合策略             │
└─────────────────────────────────────────────────────────────┘
```

### 三层域隔离

```
                    ┌──────────────────────┐
                    │   Layer 1 算法层      │
                    │   strategies/*.py    │
                    │   纯代码 | 只读引用    │
                    └──────────┬───────────┘
                               │
       ┌───────────────────────┼───────────────────────┐
       │                       │                       │
  ┌────▼─────┐          ┌──────▼──────┐         ┌──────▼──────┐
  │ 实验域    │          │  模拟交易域  │         │  实盘域(预留) │
  │           │          │             │         │             │
  │ exp.db   │  发布→   │  sim.db     │  升级→  │  live.db    │
  │ 只追加    │  复制    │  可覆盖     │  复制   │  可覆盖     │
  │           │ 模型文件 │             │ 模型文件 │             │
  │ models/   │ ──────→ │ models/     │ ──────→ │ models/     │
  │  exp/     │  参数快照│  sim/       │  参数快照│  live/      │
  └──────────┘          └─────────────┘         └─────────────┘
```

发布语义：
- **实验→模拟盘**: 复制模型文件 + 参数快照到 sim.db 的 deployments 表，原始实验记录不可变
- **模拟盘→实盘**: 复制模型文件 + 参数快照到 live.db（预留）
- 两个域各自独立运行，互不污染

---

## 二、代码目录结构

```
quant-platform/
│
├── backend/                          # Python 后端
│   ├── main.py                       # FastAPI 入口 + 生命周期
│   ├── config.py                     # 全部配置（数据库路径/API key/数据源）
│   ├── dependencies.py               # FastAPI Depends（认证/DB会话/用户）
│   │
│   ├── api/                          # REST 路由层
│   │   ├── __init__.py
│   │   ├── auth.py                   # 注册/登录/Token刷新
│   │   ├── strategies.py             # 策略注册表/扫描/详情
│   │   ├── experiments.py            # 实验CRUD/执行/指标/曲线
│   │   ├── trading.py                # 部署/组合/持仓/信号/订单
│   │   ├── coordination.py           # 协调层API
│   │   ├── data.py                   # 数据源/股票池/行业/更新
│   │   ├── ai.py                     # AI分析端点
│   │   └── jobs.py                   # 后台任务状态
│   │
│   ├── ws/                           # WebSocket 处理
│   │   ├── __init__.py
│   │   ├── training.py               # 训练进度推送
│   │   ├── realtime.py               # 实时信号推送
│   │   └── notifications.py          # 浏览器通知推送
│   │
│   ├── core/                         # ═══ 执行内核（零框架依赖） ═══
│   │   ├── __init__.py
│   │   ├── engine.py                 # 回测引擎主循环
│   │   ├── cost_model.py             # CostModel(commission, slippage, stamp_duty)
│   │   ├── rules.py                  # A股规则：T+1, 100股整手, 涨跌停
│   │   ├── metrics.py                # 36项指标计算
│   │   └── types.py                  # SignalDict, Order, Trade, 等核心类型
│   │
│   ├── strategies/                   # ═══ 策略算法层（共享） ═══
│   │   ├── __init__.py
│   │   ├── base.py                   # StrategyProtocol — 策略契约基类
│   │   ├── registry.py               # 策略发现/扫描/注册/元数据管理
│   │   ├── position_adapter.py       # 信号→仓位适配器
│   │   │
│   │   ├── technical/                # 技术策略
│   │   │   ├── ma_cross.py
│   │   │   ├── rsi_reversal.py
│   │   │   ├── bollinger_breakout.py
│   │   │   └── macd_signal.py
│   │   │
│   │   ├── ml/                       # 机器学习策略
│   │   │   ├── alpha158_lgb.py
│   │   │   ├── alpha158_xgb.py
│   │   │   ├── lstm_rank.py
│   │   │   └── transformer_rank.py
│   │   │
│   │   ├── factor/                   # 因子策略
│   │   │   └── alphamaster_gbr.py
│   │   │
│   │   └── portfolio/                # 组合策略
│   │       └── risk_parity.py
│   │
│   ├── models/                       # 模型持久化层
│   │   ├── __init__.py
│   │   ├── store.py                  # 保存/加载 .pkl + metadata.json
│   │   ├── metadata.py               # 元数据 schema
│   │   └── lifecycle.py              # exp→sim→live 发布流程
│   │
│   ├── coordination/                 # ═══ 多策略协调层 ═══
│   │   ├── __init__.py
│   │   ├── base.py                   # CoordinationProtocol
│   │   ├── algorithms/
│   │   │   ├── __init__.py
│   │   │   ├── equal_weight.py       # 等权分配
│   │   │   ├── risk_parity.py        # 风险平价（跨策略波动率）
│   │   │   ├── momentum.py           # 动量加权（近期表现最好权重最高）
│   │   │   └── mean_variance.py      # 均值-方差优化
│   │   ├── tracker.py                # 读取各策略近期表现
│   │   └── rebalancer.py             # 生成再平衡订单
│   │
│   ├── data/                         # 数据层
│   │   ├── __init__.py
│   │   ├── sources/
│   │   │   ├── __init__.py
│   │   │   ├── base.py               # DataSource ABC（支持多分辨率）
│   │   │   ├── akshare_source.py     # AKShare 实现
│   │   │   └── tushare_source.py     # Tushare 实现（备用）
│   │   ├── pipeline.py               # ETL管道：下载→清洗→缓存
│   │   ├── cache.py                  # 本地Parquet缓存管理
│   │   ├── calendar.py               # 交易日历
│   │   ├── universe.py               # 股票池定义（沪深300/中证500/CSI800/行业分类）
│   │   └── versioning.py             # 数据版本哈希
│   │
│   ├── db/                           # 数据库层
│   │   ├── __init__.py
│   │   ├── base.py                   # 基础CRUD + 连接管理
│   │   ├── experiment.py             # experiment.db 操作
│   │   ├── trading.py                # trading_sim.db / trading_live.db 操作
│   │   ├── user.py                   # 用户表操作
│   │   └── migrations/               # SQL迁移脚本
│   │       ├── 001_init_experiment.sql
│   │       ├── 002_init_trading.sql
│   │       └── 003_init_users.sql
│   │
│   ├── auth/                         # 认证模块
│   │   ├── __init__.py
│   │   ├── models.py                 # User ORM
│   │   ├── jwt_handler.py            # JWT签发/验证
│   │   └── middleware.py             # 认证中间件
│   │
│   ├── jobs/                         # 后台任务
│   │   ├── __init__.py
│   │   ├── broker.py                 # 任务代理（内存队列 + 持久化状态）
│   │   ├── tasks.py                  # 实验执行/参数扫描/数据更新任务
│   │   ├── scheduler.py              # APScheduler 每日自动任务
│   │   └── worker.py                 # 异步Worker
│   │
│   └── ai/                           # AI服务层
│       ├── __init__.py
│       ├── client.py                 # DeepSeek API客户端
│       ├── prompts.py                # Prompt模板库
│       └── cache.py                  # AI响应缓存（相同输入不重复调用）
│
├── frontend/                         # React前端
│   ├── public/
│   ├── src/
│   │   ├── main.tsx                  # 入口
│   │   ├── App.tsx                   # 路由 + 布局
│   │   │
│   │   ├── pages/                    # 页面组件
│   │   │   ├── Dashboard/            # 总览仪表盘
│   │   │   ├── ExperimentCenter/     # 实验中心
│   │   │   │   ├── ExperimentList.tsx
│   │   │   │   ├── ExperimentNew.tsx
│   │   │   │   ├── ExperimentDetail.tsx
│   │   │   │   └── ParamSweep.tsx
│   │   │   ├── TradingWorkbench/     # 交易工作台
│   │   │   │   ├── PortfolioManager.tsx
│   │   │   │   ├── PositionMonitor.tsx
│   │   │   │   ├── SignalPanel.tsx
│   │   │   │   └── OrderHistory.tsx
│   │   │   ├── DataCenter/           # 数据中心
│   │   │   ├── StrategyManager/      # 策略管理
│   │   │   └── Auth/                 # 登录/注册
│   │   │
│   │   ├── components/               # 共享组件
│   │   │   ├── layout/               # AppShell/Sidebar/Navbar
│   │   │   ├── charts/               # ECharts封装（净值曲线/回撤/持仓饼图）
│   │   │   ├── ai/                   # AI卡片组件（嵌入各面板旁）
│   │   │   │   ├── ParamSuggestion.tsx
│   │   │   │   ├── BacktestAnalysis.tsx
│   │   │   │   └── MarketInsight.tsx
│   │   │   ├── strategy/             # 策略选择器/参数表单/模式切换
│   │   │   ├── job/                  # 后台任务进度/列表
│   │   │   └── shared/               # Button/Modal/Table/Badge等
│   │   │
│   │   ├── hooks/                    # 自定义Hooks
│   │   │   ├── useWebSocket.ts
│   │   │   ├── useExperiment.ts
│   │   │   ├── useAI.ts
│   │   │   └── useAuth.ts
│   │   │
│   │   ├── services/                 # API客户端
│   │   │   ├── api.ts                # Axios实例 + 拦截器
│   │   │   ├── strategies.ts
│   │   │   ├── experiments.ts
│   │   │   ├── trading.ts
│   │   │   ├── coordination.ts
│   │   │   ├── data.ts
│   │   │   ├── ai.ts
│   │   │   └── jobs.ts
│   │   │
│   │   ├── store/                    # 状态管理 (Zustand)
│   │   │   ├── authStore.ts
│   │   │   ├── experimentStore.ts
│   │   │   ├── tradingStore.ts
│   │   │   └── notificationStore.ts
│   │   │
│   │   └── types/                    # TypeScript类型
│   │       ├── strategy.ts
│   │       ├── experiment.ts
│   │       ├── trading.ts
│   │       └── api.ts
│   │
│   ├── package.json
│   ├── vite.config.ts
│   └── tailwind.config.js
│
├── data/                             # 运行时数据（gitignore）
│   ├── cache/                        # Parquet缓存（所有用户共享）
│   │   ├── daily/                    # 日线数据
│   │   └── minute/                   # 分钟数据（预留）
│   └── models/                       # 训练模型文件
│       ├── exp/{user_id}/{strategy}/{run_id}/
│       ├── sim/{user_id}/{strategy}/{deploy_id}/
│       └── live/{user_id}/{strategy}/{deploy_id}/
│
├── requirements.txt
├── docker-compose.yml               # 可选
└── README.md
```

---

## 三、策略注册接口（契约设计）

### 3.1 策略基类（StrategyProtocol）

```python
# backend/strategies/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Any
from enum import Enum
import pandas as pd


class StrategyMode(str, Enum):
    BATCH = "batch"           # 批量历史信号
    REALTIME = "realtime"     # 实时单点信号


class StrategyCategory(str, Enum):
    TECHNICAL = "technical"
    ML = "ml"
    FACTOR = "factor"
    PORTFOLIO = "portfolio"


@dataclass
class ParamField:
    """单个参数定义"""
    name: str
    type: str                 # "int" | "float" | "str" | "bool" | "choice"
    default: Any
    description: str          # 中文说明，前端直接展示
    required: bool = True
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    choices: Optional[list] = None  # 如果是 choice 类型


@dataclass
class TrainingConfig:
    """ML策略的训练配置"""
    epochs_default: int = 100
    batch_size_default: int = 32
    learning_rate_default: float = 0.001
    early_stop_patience: int = 10
    gpu_support: bool = False
    estimated_duration_seconds: int = 60  # 预估训练耗时
    progress_callbacks: list[str] = field(default_factory=list)  # ["epoch", "loss", "val_loss"]


@dataclass
class PositionConfig:
    """仓位设定接口声明"""
    supported_modes: list[str] = field(default_factory=lambda: ["equal_weight"])
    # equal_weight | vol_adaptive | custom_ratio | fixed_shares
    max_position_pct: float = 0.05       # 单只股票最大仓位占比
    default_capital_pct: float = 0.3     # 默认策略占总资金比例


@dataclass
class StrategyMetadata:
    """策略元数据 — 策略作者声明，框架自动读取"""
    strategy_id: str              # 唯一标识，如 "ma_cross_v1"
    display_name: str             # 前端展示名
    version: str                  # 语义版本 "1.0.0"
    category: StrategyCategory
    description: str              # ⭐ 策略原理自述（AI和前端直接展示）
    author: str = ""
    source_url: str = ""

    # 能力声明
    supported_modes: list[StrategyMode] = field(default_factory=lambda: [StrategyMode.BATCH])
    requires_training: bool = False
    training_config: Optional[TrainingConfig] = None

    # 参数schema
    params: list[ParamField] = field(default_factory=list)

    # 仓位接口
    position_config: PositionConfig = field(default_factory=PositionConfig)

    # 标签（前端显示用）
    tags: list[str] = field(default_factory=list)
    # 例如: ["趋势跟踪", "低回撤", "适合震荡市"]


@dataclass
class SignalItem:
    """单条信号"""
    code: str                   # 股票代码
    action: str                 # "BUY" | "SELL" | "HOLD"
    score: float                # 信号强度 0~1
    weight: float = 0.0         # 目标权重（用于组合策略）


SignalDict = dict[str, list[SignalItem]]  # {date: [SignalItem, ...]}


@dataclass
class RealtimeSignal:
    """实时信号"""
    code: str
    action: str
    score: float
    confidence: float           # 置信度 0~1
    reasoning: str = ""         # 信号理由（可展示给用户）


@dataclass
class TrainedModel:
    """训练产物"""
    model: Any                          # 模型对象
    feature_importance: Optional[dict]  # 特征重要性
    metrics: dict                       # 训练集指标
    metadata: dict                      # 额外元数据


class StrategyProtocol(ABC):
    """
    策略契约 — 所有策略必须实现的接口。

    策略文件放到 strategies/ 对应分类目录下，
    继承本类并实现所有 abstract 方法，
    点击前端"扫描策略"即可自动注册。

    生命周期：register → validate → generate_signals (循环)
              └→ train (如需要) → generate_signals
    """

    # ═══════════════════════════════════════════════
    # 元数据（子类必须覆写）
    # ═══════════════════════════════════════════════
    @classmethod
    @abstractmethod
    def metadata(cls) -> StrategyMetadata:
        """返回策略元数据。框架读取此方法完成注册。"""
        ...

    # ═══════════════════════════════════════════════
    # 生命周期钩子
    # ═══════════════════════════════════════════════
    def on_register(self) -> None:
        """策略被注册到系统时调用。可用于初始化资源。"""
        pass

    def on_unregister(self) -> None:
        """策略被移除时调用。可用于清理资源。"""
        pass

    def validate_params(self, params: dict) -> tuple[bool, str]:
        """
        验证用户输入的参数。
        返回 (是否合法, 错误信息)。
        默认根据 PARAM_SCHEMA 自动校验。
        """
        # 框架层提供默认校验，策略可以覆写以增加自定义规则
        return True, ""

    # ═══════════════════════════════════════════════
    # 数据准备（可选覆写）
    # ═══════════════════════════════════════════════
    def prepare_data(self, pivot: pd.DataFrame, params: dict) -> pd.DataFrame:
        """
        对输入数据进行预处理。
        pivot: 股票×日期 收盘价矩阵
        返回处理后数据（可添加计算列）。
        """
        return pivot

    # ═══════════════════════════════════════════════
    # 核心信号生成
    # ═══════════════════════════════════════════════
    @abstractmethod
    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str
    ) -> SignalDict:
        """
        批量模式：给定历史收盘价矩阵，生成全时段信号字典。

        参数:
            pivot: index=date, columns=code, values=close_price
            params: 用户传入的参数字典
            start_date: 信号起始日 (YYYY-MM-DD)
            end_date: 信号截止日 (YYYY-MM-DD)

        返回:
            { "2024-01-15": [SignalItem(code="600000", action="BUY", score=0.85), ...], ... }

        注意:
            - 信号 T 日产出，引擎自动按 T+1 日收盘执行（遵守A股规则）
            - 此方法应该是纯函数，不应有副作用
        """
        ...

    def generate_realtime_signal(
        self,
        market_snapshot: pd.DataFrame,
        params: dict
    ) -> RealtimeSignal:
        """
        实时模式：给定当前市场快照，生成即时信号。

        仅当策略声明 supported_modes 包含 "realtime" 时才需要实现。

        参数:
            market_snapshot: 当前市场快照数据
            params: 用户传入的参数字典

        返回:
            RealtimeSignal 对象
        """
        raise NotImplementedError(f"{self.metadata().strategy_id} 不支持实时模式")

    # ═══════════════════════════════════════════════
    # 模型训练（ML策略需要覆写）
    # ═══════════════════════════════════════════════
    def train(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
        progress_callback: Optional[callable] = None
    ) -> TrainedModel:
        """
        训练模型。仅当 requires_training=True 时需要实现。

        参数:
            pivot: 收盘价矩阵（可扩展为多特征DataFrame）
            params: 训练超参数
            train_start, train_end: 训练数据时间范围
            progress_callback: 训练进度回调 f(epoch, loss, val_loss) → None

        返回:
            TrainedModel（包含模型对象 + 训练指标）
        """
        raise NotImplementedError(f"{self.metadata().strategy_id} 不需要训练")

    def load_model(self, path: str) -> Any:
        """从文件加载已训练的模型。默认用 joblib。"""
        import joblib
        return joblib.load(path)

    def save_model(self, model: Any, path: str) -> None:
        """保存模型到文件。默认用 joblib。"""
        import joblib
        joblib.dump(model, path)
```

### 3.2 策略注册表（Registry）

```python
# backend/strategies/registry.py

import importlib
import inspect
import os
from pathlib import Path
from typing import Optional

class StrategyRegistry:
    """
    策略注册中心。

    职责:
    1. 扫描 strategies/ 目录下所有 .py 文件
    2. 发现所有 StrategyProtocol 的子类
    3. 实例化、调用 metadata() 收集元数据
    4. 维护内存中的策略索引
    5. 向前端暴露可用策略列表
    """

    def __init__(self):
        self._strategies: dict[str, StrategyProtocol] = {}     # id → 实例
        self._metadata: dict[str, StrategyMetadata] = {}       # id → 元数据
        self._by_category: dict[str, list[str]] = {}           # 分类索引

    def scan_directory(self, base_path: str = "strategies") -> dict:
        """
        扫描策略目录，自动发现并注册所有策略。

        发现逻辑:
        1. 遍历 strategies/ 下所有子目录
        2. 对每个 .py 文件 import
        3. 找 StrategyProtocol 子类
        4. 实例化并调用 metadata()

        返回:
            {
                "new": ["ma_cross_v1"],        # 新发现的策略
                "updated": ["macd_v1"],         # 代码已变更的策略
                "removed": ["old_strategy"],    # 代码已删除的策略
                "errors": {"bad_strategy": "错误信息"}
            }
        """
        ...

    def get_strategy(self, strategy_id: str) -> Optional[StrategyProtocol]:
        """获取策略实例。"""
        return self._strategies.get(strategy_id)

    def list_all(self, user_id: int = None) -> list[StrategyMetadata]:
        """
        列出所有已注册策略的元数据。
        如果提供 user_id，附加该用户的实验数/部署数等统计。
        """
        ...

    def validate_user_params(self, strategy_id: str, params: dict) -> tuple[bool, str]:
        """校验用户参数是否合法。"""
        ...
```

### 3.3 策略示例（MA Cross）

```python
# backend/strategies/technical/ma_cross.py

import pandas as pd
from ..base import (
    StrategyProtocol, StrategyMetadata, StrategyMode,
    StrategyCategory, ParamField, PositionConfig,
    SignalItem, SignalDict
)


class MACrossStrategy(StrategyProtocol):

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="ma_cross_v1",
            display_name="双均线交叉策略",
            version="1.0.0",
            category=StrategyCategory.TECHNICAL,
            description="""双均线交叉是最经典的趋势跟踪策略。

核心思想：计算两条移动平均线（快速线20日、慢速线60日），
当快速线从下方上穿慢速线（金叉）时买入，从上方下穿（死叉）时卖出。

优势：在趋势行情中能较好捕获主升浪。
劣势：震荡市中频繁假信号，且均线本身有滞后性。""",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            params=[
                ParamField("fast_period", "int", 20,
                    description="快速均线周期（日）", min=5, max=60),
                ParamField("slow_period", "int", 60,
                    description="慢速均线周期（日）", min=20, max=250),
                ParamField("signal_delay", "int", 1,
                    description="信号延迟（日），1=T+1执行"),
            ],
            position_config=PositionConfig(
                supported_modes=["equal_weight"],
                max_position_pct=0.05,
                default_capital_pct=0.3,
            ),
            tags=["趋势跟踪", "经典策略", "低换手率"],
        )

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str
    ) -> SignalDict:
        fast = params.get("fast_period", 20)
        slow = params.get("slow_period", 60)
        delay = params.get("signal_delay", 1)

        signals: SignalDict = {}

        for code in pivot.columns:
            close = pivot[code].dropna()
            if len(close) < slow:
                continue

            ma_fast = close.rolling(fast).mean()
            ma_slow = close.rolling(slow).mean()

            # 金叉：快线上穿慢线
            cross_up = (ma_fast > ma_slow) & (ma_fast.shift(1) <= ma_slow.shift(1))
            cross_down = (ma_fast < ma_slow) & (ma_fast.shift(1) >= ma_slow.shift(1))

            for date in cross_up[cross_up].index:
                if start_date <= str(date)[:10] <= end_date:
                    sig_date = str(date)[:10]
                    signals.setdefault(sig_date, []).append(
                        SignalItem(code=code, action="BUY", score=0.8)
                    )

            for date in cross_down[cross_down].index:
                if start_date <= str(date)[:10] <= end_date:
                    sig_date = str(date)[:10]
                    signals.setdefault(sig_date, []).append(
                        SignalItem(code=code, action="SELL", score=0.8)
                    )

        return signals
```

---

## 四、数据库设计（三库隔离）

### 4.1 共用表：users.db（独立）

```sql
-- 用户表（单独数据库，所有库共享引用）
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    email TEXT,
    role TEXT DEFAULT 'user',       -- 'user' | 'admin'
    created_at TEXT DEFAULT (datetime('now')),
    last_login TEXT,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_jti TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

### 4.2 实验库：experiment.db（只追加）

```sql
-- ═══════════════════════════════════════
-- 实验记录
-- ═══════════════════════════════════════
CREATE TABLE experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,

    -- 实验标识
    name TEXT,                              -- 用户命名的实验名称
    strategy_id TEXT NOT NULL,              -- 对应策略 strategy_id

    -- 股票池
    pool_preset TEXT,                       -- "csi300" | "csi500" | "csi800" | "custom"
    pool_custom_codes TEXT,                 -- JSON数组，自定义股票代码
    pool_industries TEXT,                   -- JSON数组，行业筛选

    -- 时间窗口
    train_start TEXT,                       -- 训练数据起始 (若需训练)
    train_end TEXT,
    test_start TEXT NOT NULL,               -- 回测起始
    test_end TEXT NOT NULL,                 -- 回测截止

    -- 参数
    params TEXT NOT NULL,                   -- JSON: 完整参数字典
    params_hash TEXT NOT NULL,              -- MD5(params)
    mode TEXT DEFAULT 'batch',              -- 'batch' | 'realtime_simulation'

    -- 状态
    status TEXT DEFAULT 'pending',          -- pending|running|completed|failed|cancelled
    error_log TEXT,                         -- 失败时的完整traceback
    ai_diagnosis TEXT,                      -- AI对错误的诊断

    -- 进度
    progress_pct REAL DEFAULT 0,            -- 0~100
    progress_message TEXT,                  -- 当前步骤描述（如"正在训练LGB模型 Epoch 45/100"）

    -- 版本指纹
    data_version TEXT,                      -- 数据哈希
    code_version TEXT,                      -- git commit hash

    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX idx_exp_user ON experiments(user_id);
CREATE INDEX idx_exp_strategy ON experiments(strategy_id);
CREATE INDEX idx_exp_status ON experiments(status);

-- ═══════════════════════════════════════
-- 实验指标（36项）
-- ═══════════════════════════════════════
CREATE TABLE experiment_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL UNIQUE,

    -- 收益类
    cumulative_return REAL,                 -- 累计收益率
    annualized_return REAL,                 -- 年化收益率
    excess_return REAL,                     -- 超额收益（相对基准）

    -- 风险类
    volatility REAL,                        -- 年化波动率
    downside_volatility REAL,               -- 下行波动率
    max_drawdown REAL,                      -- 最大回撤
    max_drawdown_duration INTEGER,          -- 最长回撤回补天数
    var_95 REAL,                            -- 95% VaR
    cvar_95 REAL,                           -- 95% CVaR

    -- 比率类
    sharpe_ratio REAL,                      -- 夏普比率
    sortino_ratio REAL,                     -- 索提诺比率
    calmar_ratio REAL,                      -- 卡尔玛比率
    information_ratio REAL,                 -- 信息比率

    -- 基准相对
    benchmark_return REAL,                  -- 基准同期累计收益
    alpha REAL,                             -- Jensen's Alpha
    beta REAL,                              -- Beta
    up_capture REAL,                        -- 上行捕获率
    down_capture REAL,                      -- 下行捕获率

    -- 交易类
    total_trades INTEGER,                   -- 总成交笔数
    win_rate REAL,                          -- 胜率
    profit_factor REAL,                     -- 盈亏比
    avg_hold_days REAL,                     -- 平均持仓天数
    turnover_rate REAL,                     -- 年化换手率
    cost_drag REAL,                         -- 交易成本拖累（%）

    -- 扩展指标（预留）
    extra_metrics TEXT,                     -- JSON: 策略自定义指标

    calculated_at TEXT DEFAULT (datetime('now'))
);

-- ═══════════════════════════════════════
-- 净值曲线
-- ═══════════════════════════════════════
CREATE TABLE equity_curve (
    experiment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    equity REAL NOT NULL,                   -- 组合净值
    benchmark_equity REAL,                  -- 基准净值
    drawdown REAL,                          -- 当日回撤幅度
    cash REAL,                              -- 现金余额
    PRIMARY KEY (experiment_id, date)
);

-- ═══════════════════════════════════════
-- 成交明细
-- ═══════════════════════════════════════
CREATE TABLE trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    action TEXT NOT NULL,                   -- BUY | SELL
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    amount REAL NOT NULL,                   -- 成交金额
    cost REAL NOT NULL,                     -- 交易成本（佣金+印花税+滑点）
    signal_strategy TEXT,                   -- 产生该交易的策略ID
    signal_score REAL,                      -- 信号强度

    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

-- ═══════════════════════════════════════
-- 模型产物
-- ═══════════════════════════════════════
CREATE TABLE model_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    model_file_path TEXT NOT NULL,          -- 模型文件路径
    metadata_file_path TEXT NOT NULL,       -- metadata.json 路径
    params_hash TEXT NOT NULL,
    train_window_start TEXT,
    train_window_end TEXT,
    feature_count INTEGER,
    train_samples INTEGER,
    train_metrics TEXT,                     -- JSON: 训练集指标
    feature_importance TEXT,                -- JSON: 特征重要性
    created_at TEXT DEFAULT (datetime('now')),

    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

-- ═══════════════════════════════════════
-- 参数扫描
-- ═══════════════════════════════════════
CREATE TABLE param_sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    parent_experiment_id INTEGER,           -- 基线实验
    sweep_name TEXT,
    sweep_config TEXT NOT NULL,             -- JSON: {param_name: [v1, v2, ...]}
    total_combinations INTEGER,
    completed_combinations INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',          -- pending|running|completed
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE sweep_results (
    sweep_id INTEGER NOT NULL,
    experiment_id INTEGER NOT NULL,         -- 子实验ID
    param_values TEXT NOT NULL,             -- JSON: {"fast_period": 20, "slow_period": 50}
    rank INTEGER,                           -- 按Sharpe排名
    PRIMARY KEY (sweep_id, experiment_id)
);
```

### 4.3 交易库：trading_sim.db / trading_live.db（相同schema）

```sql
-- ═══════════════════════════════════════
-- 策略部署
-- ═══════════════════════════════════════
CREATE TABLE deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,

    -- 策略信息
    strategy_id TEXT NOT NULL,              -- 策略ID
    display_name TEXT,                      -- 用户自定义名称
    params TEXT NOT NULL,                   -- JSON
    params_hash TEXT NOT NULL,
    mode TEXT DEFAULT 'batch',              -- 'batch' | 'realtime'

    -- 关联实验（可空，表示手动部署）
    source_experiment_id INTEGER,
    model_artifact_id INTEGER,

    -- 仓位配置
    position_mode TEXT DEFAULT 'equal_weight',
    -- equal_weight | vol_adaptive | custom_ratio | fixed_shares
    position_config TEXT,                   -- JSON: 详细仓位参数

    -- 部署状态
    status TEXT DEFAULT 'active',           -- active | paused | stopped | error
    status_tags TEXT,                       -- JSON: 用户自定义标签 ["观察中", "高波动注意"]
    user_notes TEXT,                        -- 用户备注

    -- 状态流转时间线
    deployed_at TEXT DEFAULT (datetime('now')),
    last_signal_at TEXT,
    last_rebalance_at TEXT,
    stopped_at TEXT,

    created_at TEXT DEFAULT (datetime('now'))
);

-- ═══════════════════════════════════════
-- 组合配置（多策略资金分配）
-- ═══════════════════════════════════════
CREATE TABLE portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    total_capital REAL NOT NULL,            -- 总资金
    rebalance_frequency TEXT DEFAULT 'monthly',
    -- 'daily' | 'weekly' | 'monthly' | 'manual'

    -- 协调算法
    coordination_algorithm TEXT DEFAULT 'equal_weight',
    -- 'equal_weight' | 'risk_parity' | 'momentum' | 'mean_variance' | 'custom'
    coordination_params TEXT,               -- JSON: 协调算法参数

    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE portfolio_allocations (
    portfolio_id INTEGER NOT NULL,
    deployment_id INTEGER NOT NULL,
    weight REAL NOT NULL,                   -- 资金权重 0~1
    capital REAL NOT NULL,                  -- 分配资金
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (portfolio_id, deployment_id)
);

-- ═══════════════════════════════════════
-- 每日信号
-- ═══════════════════════════════════════
CREATE TABLE daily_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    signal_type TEXT NOT NULL,              -- BUY | SELL | HOLD
    score REAL,
    target_weight REAL,                     -- 目标仓位权重
    confidence REAL,                        -- 置信度
    reasoning TEXT,                         -- 信号理由
    created_at TEXT DEFAULT (datetime('now'))
);

-- ═══════════════════════════════════════
-- 持仓快照（每日收盘后写入）
-- ═══════════════════════════════════════
CREATE TABLE position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    shares INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    close_price REAL NOT NULL,
    market_value REAL NOT NULL,
    unrealized_pnl REAL,
    weight_in_portfolio REAL               -- 在总组合中的仓位占比
);

-- ═══════════════════════════════════════
-- 订单记录
-- ═══════════════════════════════════════
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id INTEGER NOT NULL,
    portfolio_id INTEGER,                   -- 所属组合（可选）
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    action TEXT NOT NULL,                   -- BUY | SELL
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    amount REAL NOT NULL,
    cost REAL NOT NULL,
    order_type TEXT DEFAULT 'signal',       -- signal | rebalance | manual | coordination
    status TEXT DEFAULT 'filled',           -- filled | partial | rejected | pending
    reject_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- ═══════════════════════════════════════
-- 净值记录（每日）
-- ═══════════════════════════════════════
CREATE TABLE nav_history (
    deployment_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    nav REAL NOT NULL,                      -- 单位净值
    daily_return REAL,                      -- 日收益率
    cumulative_return REAL,                 -- 累计收益
    drawdown REAL,                          -- 当日回撤
    PRIMARY KEY (deployment_id, date)
);
```

### 4.4 数据隔离总结

```
users.db
├── users                    # 所有用户共享
└── user_sessions

experiment.db                # 每个用户独立
├── experiments              # 实验记录（只追加）
├── experiment_metrics       # 36项指标
├── equity_curve             # 净值曲线
├── trade_log                # 成交明细
├── model_artifacts          # 模型产物
├── param_sweeps             # 参数扫描
└── sweep_results

trading_sim.db               # 模拟交易（每用户独立）
├── deployments              # 策略部署
├── portfolios               # 组合配置
├── portfolio_allocations    # 资金分配
├── daily_signals            # 每日信号
├── position_snapshots       # 持仓快照
├── orders                   # 订单记录
└── nav_history              # 净值历史

trading_live.db              # 实盘交易（预留，相同schema）
└── (同上7表)
```

---

## 五、多策略协调层设计

### 5.1 协调层定位

协调层是一个**特殊策略**，它的"输入"是其他策略的部署表现，"输出"是各策略的权重分配。它不直接做选股，而是做**策略间的资金分配**。

```
协调层输入:
├── 各策略近期NAV曲线（从 trading_sim.db nav_history 读取）
├── 各策略近期指标（Sharpe/MaxDD/胜率 等）
├── 各策略当前信号（从 daily_signals 读取）
└── 用户配置（风险偏好/再平衡频率/算法选择）

协调层输出:
└── 权重字典 {deployment_id: weight}
    └→ 写入 portfolio_allocations 表
    └→ 交易引擎根据权重分配资金
```

### 5.2 协调算法协议

```python
# backend/coordination/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
import pandas as pd


@dataclass
class StrategyPerformance:
    """单个策略的近期表现快照"""
    deployment_id: int
    strategy_name: str
    recent_nav: pd.Series          # 近期净值序列
    recent_returns: pd.Series      # 近期日收益率
    sharpe_3m: float               # 近3月Sharpe
    sharpe_6m: float
    max_drawdown_3m: float
    volatility_3m: float
    win_rate_3m: float
    correlation_matrix: pd.DataFrame  # 与其他策略的相关性矩阵


@dataclass
class WeightAllocation:
    """权重分配结果"""
    weights: dict[int, float]       # {deployment_id: weight}
    rationale: str                  # 分配理由（供AI解读和用户理解）


class CoordinationProtocol(ABC):
    """
    协调算法契约。每个协调算法实现此接口。
    可以像策略一样注册、扫描、选择。
    """

    @classmethod
    @abstractmethod
    def algorithm_id(cls) -> str:
        """唯一标识，如 'equal_weight_v1'"""
        ...

    @classmethod
    @abstractmethod
    def display_name(cls) -> str:
        """显示名，如 '等权分配'"""
        ...

    @classmethod
    @abstractmethod
    def description(cls) -> str:
        """算法说明"""
        ...

    @abstractmethod
    def compute_weights(
        self,
        performances: list[StrategyPerformance],
        total_capital: float,
        user_params: dict
    ) -> WeightAllocation:
        """
        计算各策略的权重分配。

        参数:
            performances: 各策略近期表现数据
            total_capital: 总资金
            user_params: 用户自定义参数（如风险厌恶系数）

        返回:
            WeightAllocation 对象
        """
        ...
```

### 5.3 内置协调算法

| 算法ID | 名称 | 逻辑 | 适用场景 |
|--------|------|------|----------|
| `equal_weight` | 等权分配 | 每个策略分配相同资金 | 基线，没有先验偏好 |
| `risk_parity` | 风险平价 | 按波动率倒数加权，使各策略风险贡献相等 | 稳健型，控制组合波动 |
| `momentum` | 动量加权 | 近期表现好的策略权重更高（按3月Sharpe排名） | 趋势型，追强势策略 |
| `mean_variance` | 均值方差 | Markowitz优化，最大化Sharpe | 理论最优，但依赖协方差估计 |

### 5.4 协调层执行流程

```
用户触发再平衡
    │
    ▼
协调层 tracker.py:
  读取 trading_sim.db → 各部署的 nav_history
  计算 StrategyPerformance (含相关性矩阵)
    │
    ▼
协调层算法 (如 Risk Parity):
  输入: 各策略波动率 + 相关性
  输出: WeightAllocation
    │
    ▼
协调层 rebalancer.py:
  比较新权重 vs 当前权重
  生成调整订单:
    - 权重增加 → BUY 信号
    - 权重减少 → SELL 信号
    │
    ▼
写入 portfolio_allocations 表
写入 orders 表 (order_type='coordination')
    │
    ▼
交易引擎执行订单 → 更新持仓
```

---

## 六、API 接口设计

### 6.1 认证

```
POST   /api/auth/register
  Body:  { username, password, display_name, email }
  →      { user_id, token }

POST   /api/auth/login
  Body:  { username, password }
  →      { token, user }

POST   /api/auth/refresh
  →      { token }

GET    /api/auth/me
  →      { user }
```

### 6.2 策略管理

```
GET    /api/strategies
  参数: ?category=ml&mode=realtime
  →     [StrategyMetadata, ...]               # 所有已注册策略

POST   /api/strategies/scan
  Body:  { base_path: "strategies" }
  →     { new: [...], updated: [...], removed: [...], errors: {...} }

GET    /api/strategies/:id
  →     StrategyMetadata + 该用户的实验数/部署数

POST   /api/strategies/:id/validate
  Body:  { params: {...} }
  →     { valid: true/false, errors: {...} }
```

### 6.3 实验中心

```
GET    /api/experiments
  参数: ?strategy_id=ma_cross_v1&status=completed&page=1&limit=20
  →     { experiments: [...], total, page, limit }

POST   /api/experiments
  Body:  {
    name, strategy_id,
    pool_preset, pool_custom_codes, pool_industries,
    train_start, train_end, test_start, test_end,
    params, mode
  }
  →     { experiment_id, job_id }            # 立即返回，后台执行

GET    /api/experiments/:id
  →     { experiment, metrics, progress }    # 含进度信息

DELETE /api/experiments/:id
  →     { deleted: true }

GET    /api/experiments/:id/metrics
  →     全部 36 项指标

GET    /api/experiments/:id/equity
  参数: ?resolution=daily                    # 'daily' | 'weekly' | 'monthly'
  →     { dates: [...], equity: [...], benchmark: [...], drawdown: [...] }

GET    /api/experiments/:id/trades
  参数: ?page=1&limit=50
  →     [Trade, ...]

GET    /api/experiments/:id/model
  →     { model_artifact, feature_importance }

# 参数扫描
POST   /api/experiments/sweep
  Body:  {
    base_experiment_id,    # 基线实验（可空）
    strategy_id, params_range: { fast_period: [10,20,30,40,50], slow_period: [40,60,80] },
    pool_preset, test_start, test_end
  }
  →     { sweep_id, total_combinations, job_ids: [...] }

GET    /api/experiments/sweep/:sweep_id
  →     { sweep, results: [{rank, params, metrics}] }

# 对比
POST   /api/experiments/compare
  Body:  { experiment_ids: [1, 2, 3] }
  →     { metrics_comparison: [...], equity_curves: [...] }
```

### 6.4 交易工作台

```
# 部署管理
GET    /api/trading/deployments
  参数: ?status=active
  →     [Deployment, ...]

POST   /api/trading/deployments
  Body:  {
    strategy_id, display_name,
    source_experiment_id,    # 从实验发布
    params, mode,
    position_mode, position_config
  }
  →     { deployment_id }

PUT    /api/trading/deployments/:id
  Body:  { status, position_config, status_tags, user_notes }
  →     { updated: true }

DELETE /api/trading/deployments/:id
  →     { deleted: true }

# 组合管理
GET    /api/trading/portfolios
  →     [Portfolio, ...]

POST   /api/trading/portfolios
  Body:  { name, total_capital, rebalance_frequency, coordination_algorithm, allocations: [{deployment_id, weight}] }
  →     { portfolio_id }

PUT    /api/trading/portfolios/:id
  Body:  { allocations, total_capital, ... }
  →     { updated: true }

# 持仓
GET    /api/trading/positions
  参数: ?portfolio_id=1&date=2026-07-27
  →     { positions: [{code, shares, market_value, pnl, ...}], total_value, total_pnl }

# 信号
GET    /api/trading/signals
  参数: ?deployment_id=1&date=2026-07-27
  →     [Signal, ...]

# 订单
GET    /api/trading/orders
  参数: ?deployment_id=1&page=1&limit=50
  →     { orders: [...], total }

# 模拟执行
POST   /api/trading/simulate/run
  Body:  { date: "2026-07-27" }
  →     { job_id }

GET    /api/trading/simulate/status
  →     { last_run_date, status, next_scheduled }
```

### 6.5 多策略协调

```
GET    /api/coordination/strategies
  参数: ?portfolio_id=1
  →     { performances: [StrategyPerformance, ...], correlation_matrix }

POST   /api/coordination/optimize
  Body:  { portfolio_id, algorithm: "risk_parity", algorithm_params: {} }
  →     { weights: { deployment_id: weight, ... }, rationale }

GET    /api/coordination/weights
  参数: ?portfolio_id=1
  →     { current_weights: {...}, optimized_weights: {...}, last_optimized_at }

PUT    /api/coordination/weights
  Body:  { portfolio_id, weights: { 1: 0.4, 2: 0.3, 3: 0.3 } }
  →     { updated: true }
```

### 6.6 数据中心

```
GET    /api/data/pools
  →     [{ id: "csi300", name: "沪深300", stock_count: 300 }, ...]

GET    /api/data/pools/:pool_id/stocks
  →     [{ code, name, industry, market_cap, ... }]

GET    /api/data/industries
  →     [{ code: "SW801730", name: "电力设备" }, ...]

GET    /api/data/stocks/:code
  参数: ?start=2024-01-01&end=2026-07-27&resolution=daily
  →     { code, name, data: [{ date, open, high, low, close, volume }] }

POST   /api/data/update
  Body:  { pool_ids: ["csi500"], resolution: "daily", force: false }
  →     { job_id }

GET    /api/data/update/status
  →     { last_update, status, data_version }
```

### 6.7 AI 服务

```
POST   /api/ai/analyze-backtest
  Body:  { experiment_id }
  →     { analysis: "..." }                 # 回测结果分析文本

POST   /api/ai/suggest-params
  Body:  { strategy_id, current_params: {...} }
  →     { suggestions: [{ param, current_value, suggested_value, reason }] }

POST   /api/ai/market-insight
  Body:  { portfolio_id }
  →     { insight: "..." }                  # 市场解读文本

POST   /api/ai/diagnose-error
  Body:  { experiment_id, error_log: "..." }
  →     { diagnosis: "...", fix_suggestion: "..." }

POST   /api/ai/explain-signal
  Body:  { strategy_id, signal: {...}, context: "..." }
  →     { explanation: "..." }
```

### 6.8 后台任务

```
GET    /api/jobs
  参数: ?status=running&user_id=me
  →     [{ job_id, type, status, progress, created_at }, ...]

GET    /api/jobs/:id
  →     { job_id, type, status, progress_pct, progress_message, result, error }

DELETE /api/jobs/:id
  →     { cancelled: true }
```

---

## 七、WebSocket 设计

### 7.1 训练进度推送

```
WS:  /ws/training/{experiment_id}

服务端 → 客户端:
  { type: "progress", epoch: 45, total_epochs: 100, loss: 0.0231, val_loss: 0.0342 }
  { type: "completed", model_artifact_id: 5 }
  { type: "error", message: "CUDA out of memory" }
```

### 7.2 实时信号推送（仅实时模式策略）

```
WS:  /ws/realtime/{deployment_id}

服务端 → 客户端:
  { type: "signal", code: "600000", action: "BUY", score: 0.85, timestamp: "..." }
  { type: "heartbeat", timestamp: "..." }
```

### 7.3 浏览器通知

```
WS:  /ws/notifications

服务端 → 客户端:
  { type: "job_complete", job_id: 5, experiment_id: 3, message: "回测完成" }
  { type: "error", message: "数据更新失败: API限流" }
  { type: "signal_alert", deployment_id: 2, message: "MACD策略发出强卖信号" }
```

---

## 八、前端页面设计

### 8.1 全局布局

```
┌──────────────────────────────────────────────────────────┐
│  Sidebar (可折叠)                                         │
│  ┌─────────────┐                                         │
│  │ 📊 总览      │  ┌─────────────────────────────────┐   │
│  │ 🧪 实验中心   │  │                                 │   │
│  │ 💼 交易工作台 │  │       主内容区                    │   │
│  │ 📡 数据中心   │  │                                 │   │
│  │ 📚 策略管理   │  │                                 │   │
│  │              │  │                                 │   │
│  │ ─────────── │  │                                 │   │
│  │ 🔔 通知 (3) │  │                                 │   │
│  │ 👤 用户      │  │                                 │   │
│  └─────────────┘  └─────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### 8.2 总览仪表盘 `/dashboard`

- **顶部**: 组合总净值卡片（今日/累计）、总资金、总盈亏
- **中间**: 主净值曲线图（多策略叠加 + 基准对比）
- **右侧**: AI 市场解读卡片（嵌入式）
- **下方**: 各策略表现卡片网格（每个策略一个小卡片：名称/收益/状态标签/备注）
- **底部**: 最近信号列表（实时刷新）

### 8.3 实验中心 `/experiment`

**列表页** `/experiment`：
- 表格：实验名称 | 策略 | 股票池 | 时间范围 | 状态 | Sharpe | 收益 | 创建时间
- 顶部筛选栏：策略类型 / 状态 / 时间
- "新建实验"按钮

**新建实验** `/experiment/new`：
- 步骤1：选择策略（卡片式，按分类排列，可搜索）
- 步骤2：配置参数（表单 + AI调参建议卡片在右侧）
- 步骤3：选择股票池（预设池 / 自定义勾选 / 行业筛选三个Tab）
- 步骤4：选择时间范围（日历选择器 + 快捷选择：近1年/3年/5年）
- 点击"开始实验" → 后端创建job → 跳转到实验详情页（显示进度）

**实验详情** `/experiment/:id`：
- 进度条（训练中：实时epoch/loss）
- 完成后自动展示：
  - 净值曲线（带回撤阴影）
  - 36项指标卡片（可排序）
  - 成交明细表（前50笔）
  - AI回测分析卡片（自动触发）
  - 模型文件信息（如有训练）

**参数扫描** `/experiment/sweep`：
- 选择基线实验或策略
- 选择要扫描的参数 → 设定范围/步长
- 实时显示扫描进度矩阵（热力图）
- 完成后排序，标注最优参数组合

**策略对比** `/experiment/compare`：
- 多选实验（最多5个）
- 指标差异表（红绿着色）
- 净值曲线叠加图
- 回撤对比图

### 8.4 交易工作台 `/trading`

**组合管理** `/trading/portfolio`：
- 组合总览卡片
- 各策略资金分配饼图
- 权重调整面板（拖动滑块 + 协调层一键优化按钮）
- 协调算法选择下拉 → 点击"优化" → 显示建议权重 + 理由 → 用户确认/手动调整 →
  点击"应用"

**持仓监控** `/trading/positions`：
- 总持仓表格：代码 | 名称 | 数量 | 成本 | 现价 | 市值 | 盈亏 | 所属策略
- 右侧AI市场解读卡片
- 实时刷新（如是实时模式策略）

**信号面板** `/trading/signals`：
- 模式切换开关：实时模式 / 日频模式
- 实时模式：信号流（类似消息列表，新信号自动推到顶部）
- 日频模式：当日信号表格，每个信号可点击查看AI解释

**成交记录** `/trading/orders`：
- 订单表格（可分策略筛选）
- 成交额/成本汇总

### 8.5 策略管理 `/strategies`

- 已注册策略列表（卡片式，显示元数据/状态/实验数/部署数）
- "扫描策略"按钮 → 扫描结果弹窗
- 点击策略卡片 → 策略详情页 `/strategies/:id`

**策略详情**：
- 基本信息（名称/版本/分类/来源/标签）
- **策略原理**（自述文本，大段展示）
- 参数Schema（表格形式）
- 策略能力（支持模式/是否需要训练）
- **多状态标签**（同时显示实验/模拟盘/实盘状态）
- 用户备注编辑

---

## 九、AI 嵌入设计

### 9.1 Prompt 模板

```python
# backend/ai/prompts.py

BACKTEST_ANALYSIS_PROMPT = """
你是一个量化策略分析师。请分析以下回测结果：

策略: {strategy_name}
策略原理: {strategy_description}
回测时间: {test_start} ~ {test_end}
基准: {benchmark_name}

## 核心指标
- 累计收益: {cumulative_return}%
- 年化收益: {annualized_return}%
- Sharpe: {sharpe}
- Sortino: {sortino}
- 最大回撤: {max_drawdown}%
- 胜率: {win_rate}%
- 总交易: {total_trades}笔
- 基准同期收益: {benchmark_return}%

## 资金曲线关键点
{equity_highlights}

请给出：
1. 整体评价（2-3句话）
2. 亮点（1-2个）
3. 风险点（1-2个）
4. 改进建议（1-2条具体可操作的）
"""

PARAM_SUGGESTION_PROMPT = """
你是一个量化策略参数优化专家。

策略: {strategy_name}
策略原理: {strategy_description}
当前参数: {current_params}
参数说明: {param_schema}

请分析当前参数设置，给出2-3条调参建议。每条建议包含：
- 参数名
- 当前值
- 建议值
- 理由（一句话）

考虑该策略在A股市场的典型表现特征。
"""

MARKET_INSIGHT_PROMPT = """
你是一个投资组合分析师。请基于以下信息给出市场解读：

今日市场: {market_summary}
我的持仓: {positions_summary}
今日信号: {signals_summary}

请给出：
1. 今日市场概况（1句话）
2. 我的组合表现分析（是否跑赢/跑输，哪个策略贡献最大）
3. 需要关注的风险（如有）
4. 操作建议（如果信号面板有重大信号，提醒关注）

使用通俗易懂的语言。
"""
```

### 9.2 AI 调用缓存策略

```python
# backend/ai/cache.py
# 相同输入（strategy_id + params_hash + data_version）的AI分析结果
# 缓存24小时，避免重复调用浪费API配额
```

### 9.3 前端 AI 组件行为

| 组件 | 触发方式 | 加载状态 | 位置 |
|------|---------|---------|------|
| ParamSuggestion | 参数修改后3秒无操作 | 骨架屏 | 参数配置面板右侧 |
| BacktestAnalysis | 回测完成后自动 | 打字机效果 | 指标面板下方 |
| MarketInsight | 进入交易工作台时 | 骨架屏 | 持仓表格右侧 |
| ErrorDiagnosis | 实验失败时显示按钮，点击触发 | 加载Spinner | 错误信息旁 |
| SignalExplain | 点击信号行 | 弹出Popover | 信号面板内 |

---

## 十、待决策问题

以下是我在设计过程中还拿不准的点，请逐一确认：

### 问题1：后台任务引擎
实验回测/参数扫描/数据更新都是异步任务。后台任务引擎选什么：
- **A. 自建简单队列** — 内存队列 + SQLite 持久化状态（最简单，单进程够用）
- **B. Celery + Redis** — 工业级，但引入额外依赖
- **C. ARQ（asyncio原生）** — Python原生协程队列

推荐 **A**（你一个人用，不需要分布式），后续可升级到 C。

### 问题2：前端技术栈
- **A. React + Vite + Tailwind CSS + ECharts**（我最熟悉，生态最好）
- **B. Vue 3 + Vite + Tailwind + ECharts**
- **C. 你来定**

推荐 **A**，React 生态下的图表库（Recharts/ECharts）和组件库（shadcn/ui）最成熟。

### 问题3：协调层的"动量加权"算法
"近期表现好"怎么定义？
- **A. 按近3月 Sharpe 排名加权** — 纯风险调整后收益
- **B. 按近3月累计收益排名加权** — 纯收益
- **C. 混合打分** — 综合 Sharpe + 收益 + 回撤 + 胜率

### 问题4：分钟级数据精度
你说"预留分钟级数据接口"。AKShare 支持的精度：
- 1分钟 / 5分钟 / 15分钟 / 30分钟 / 60分钟

哪个精度是你需要的？这影响数据缓存的存储策略。

### 问题5：组合内策略冲突
两个策略同时想买同一只股票（比如 MACD 和 Alpha158 都看多600000），资金怎么分配？
- **A. 按策略权重比例分配** — MACD占40%就用40%的资金买
- **B. 先到先得** — 先执行的策略全买，后执行的不买
- **C. 合并信号** — 信号叠加（两个策略都看好 → 超配）

### 问题6：策略扫描的安全边界
你说要在Web UI上点"扫描"按钮扫描Python文件并加载。多用户场景下，这是否意味着：
- **A. 只允许管理员扫描策略**（普通用户只能使用已注册的策略）
- **B. 所有用户都可以上传/扫描策略**（需要沙箱隔离，复杂度高）

---

以上，整个平台的完整设计。**请重点确认前 3 个技术选型问题 + 后 3 个业务逻辑问题**，然后我立刻进入开发。
