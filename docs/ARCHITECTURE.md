# 架构文档

> **历史文档，已被取代。** 本文描述 2026-07-26 的早期 dispatch/kernel 架构，不能作为
> 当前实现或数据边界依据。现行架构见 [ARCHITECTURE_V3.md](ARCHITECTURE_V3.md)，文档
> 状态见 [DOCUMENT_INDEX.md](DOCUMENT_INDEX.md)。

> 最后更新：2026-07-26（研究工作台 P0 + ML 训练隔离完成）

## 总览

系统由两条互补的链路组成：

1. **生产链路**（dispatch）— 单一时间线的每日模拟交易：固定起点、每日覆盖、
   邮件/微信分发。回答"今天该买什么、各策略战绩如何"。
2. **研究链路**（dispatch/kernel + dispatch/research）— 多维实验空间：
   策略 × 参数 × 窗口 × 成本 × 股票池，结果只追加不覆盖。
   回答"该把钱交给哪个策略，凭什么"。

两条链路**共用同一个执行内核**（kernel/sim_engine），保证研究结论与生产行为一致；
但**数据库物理隔离**（trades.db vs research.db），生产战绩永远不被实验污染。

```
                   ┌──────────────────────────────────────┐
                   │     dispatch/web/app.py (Flask)      │
                   │  主页/总览/策略/成交/对比 + 蓝图:     │
                   │  research / lab / admin / reports /  │
                   │  assistant                           │
                   └───────┬──────────────────┬───────────┘
                           │                  │
              ┌────────────▼─────┐   ┌────────▼─────────┐
              │  kernel 执行内核  │   │  research 研究层  │
              │  data_service    │   │  store (5表)      │
              │  signal_service  │   │  metrics (36项)   │
              │  sim_engine      │──►│  (P1: stats /     │
              │  runner(统一接口) │   │   robustness)     │
              └────────┬─────────┘   └────────┬─────────┘
                       │                      │
              ┌────────▼─────────┐   ┌────────▼─────────┐
              │  trades.db       │   │  research.db     │
              │  生产战绩(覆盖)   │   │  实验库(只追加)   │
              └──────────────────┘   └──────────────────┘
                       ▲
              ┌────────┴─────────┐
              │ 每日 15:35 pipeline│  run_daily: 数据更新 →
              │ (APScheduler)    │  walk-forward 信号 → 重模拟 →
              └──────────────────┘  推荐邮件+表现邮件+企业微信
```

## 执行内核（dispatch/kernel/）

纯 Python，零 Flask 依赖，生产与研究共用。

| 模块 | 职责 |
|---|---|
| `sim_engine.py` | 确定性全窗口重模拟。**成本模型参数化**为 `CostModel(commission, slippage, stamp_duty)`，默认值 (0.001/0.001/0.001) 与原硬编码常量逐笔一致；`scaled(n)` 用于成本敏感性分析。双仓位模式：等权 / 波动率自适应 |
| `runner.py` | 统一运行接口：`RunSpec`（策略/窗口/池/模式/成本/标签/**实验参数**）→ `RunResult`。`execute_and_save()` 一步执行并落库，自动计算 `effective_params`（注册默认值 + RunSpec.params 覆盖）+ `params_hash` |
| `model_store.py` | ML 模型持久化：训练产物以 `{scope}/{strategy}/{params_hash}/{retrain_date}.pkl` 落盘，附 `.json` 元数据（data_version/code_version/n_samples）。同参数二次运行直接加载，跳过重训 |
| `data_service.py` | re-export services 的数据管道（缓存读取、增量更新、交易日历、基准） |
| `signal_service.py` | re-export services 的信号生成（技术/因子/ML walk-forward） |

执行语义：T 日信号 → T+1 日收盘执行；每 RB 个交易日为调仓日（清仓后按前一日
信号重建）；卖收印花税，双边佣金+滑点；A 股 100 股整手。

## 研究层（dispatch/research/）

| 模块 | 职责 |
|---|---|
| `store.py` | 实验存储 research.db。五表：`runs`（元数据+版本指纹）、`run_metrics`（长表，指标可扩展不改 schema）、`run_equity`（净值曲线）、`run_trades`（逐笔成交）、`sweeps`（参数扫描父子关系）。WAL 模式支持运行中并发读 |
| `metrics.py` | 36 项指标：收益类（总/年化/超额）、风险类（波动/下行波动/最大回撤/回撤时长/水下时间/VaR/CVaR）、比率类（Sharpe/Sortino/Calmar/IR）、基准相对（Alpha/Beta/上下行捕获）、交易类（胜率/盈亏比/换手/成本拖累）。`VOL_FLOOR` 防护：波动率低于 1e-6 的退化曲线比率为 0，不产生伪 Sharpe |
| （P1）`stats.py` | Sharpe 标准误/t 值、bootstrap 置信区间、deflated Sharpe |
| （P1）`robustness.py` | 参数扫描、成本扫描、分时段/分环境 |

可复现性：每个 run 记录 `data_version`（行数|股票数|截止日的数据指纹，pivot 和 raw 双格式兼容）、
`code_version`（git HEAD）、`params_hash`（生效参数全集 MD5），结果永远可追溯到产生它的确切输入。

## 缓存与训练隔离

### 信号缓存（signals_cache/）

路径：`state/signals_cache/{scope}/{strategy}/{params_hash}.json`

- `scope ∈ {prod, research}` — 生产与实验物理隔离
- 不同参数集天然不同文件，**无需全局失效逻辑**
- 写入使用 `tempfile + os.replace` 原子替换，并发安全
- 生产 pipeline 传 `cache_scope="prod"`（sim_runner.py:85），实验默认 `"research"`
- 旧版全局缓存文件（`ml_{strategy}.json`）已废弃

### 模型存储（models/）

路径：`state/models/{scope}/{strategy}/{params_hash}/{retrain_date}.pkl`

- 每次 walk-forward 重训后保存模型 + 元数据 JSON
- 同参数二次运行→直接加载，跳过重训（LSTM 从约 40 秒降到秒级）
- 使用 joblib/pickle 双 fallback 序列化
- 元数据含 `data_version/code_version/n_samples/feature_list`，可追溯

### 参数传递管道

```
RunSpec.params (用户指定超参覆盖)
  → runner.py: generate_all_signals(pivot, start, strategies, params_overrides, cache_scope="research")
    → signal_service: generate_all_signals → 逐策略 {**get_params(sn), **overrides.get(sn, {})}
      → signal 策略: spec.signal_func(pivot, merged_params)
      → ML 策略: generate_ml_signals → _ml_top_codes_at_retrains(..., params_override, cache_scope)
            ├── 训练前: load_model(strategy, params, retrain_date, scope) → 命中则跳过
            ├── 训练后: save_model(model, strategy, params, retrain_date, ...)
            └── 信号缓存: signals_cache/{scope}/{strategy}/{params_hash}.json (原子写)
  → execute_and_save: effective_params = get_params(strategy) | spec.params → 落 research.db
```

生产链路：`sim_runner.py` → `generate_all_signals(cache_scope="prod")`，信号缓存独立，
模型缓存独立，参数来自 `deployments` 表（P1 实施）。

## Web 层（dispatch/web/）

- `ui/layout.py` — 页面外壳（page/nav/CSS/表格助手）。从 app.py 抽出后，
  各蓝图模块级直接引用，根除了原先函数内 `from web.app import page` 的循环导入
- `app.py` — 主页（最新报告 iframe）、总览、策略管理、成交、对比、调度
- `research.py` — 研究工作台蓝图：
  - `/research` 实验发射台（同步执行 15-60 秒，完成跳转详情页）
  - `/research/runs` 排行榜：36 项指标列、客户端排序、策略/标签/池筛选，
    NULL 指标永远沉底
  - `/research/run/<id>` 深潜：净值+回撤填充图、全指标卡、成交明细（前 50 笔）、
    参数/成本/版本指纹、删除
  - `/research/compare?runs=a,b` 头对头：指标差异表（红绿着色）+ 归一化净值叠加
- `lab.py` — 实验室：数据面板、一键训练、一键回测（结果自动入 research.db）
- `admin.py` / `reports.py` / `assistant.py` — 策略管理 / 报告归档 / AI 助手

## 生产链路（dispatch/services/ 等）

- `sim_runner.py` — 每日重模拟：逐策略读注册中心的 rebalance_days/max_positions，
  全窗口重放后刷新 trades.db（幂等、无增量状态补丁）
- `daily_recommend.py` / `daily_performance.py` — 邮件构建（matplotlib 图表）
- `scheduler.py` — APScheduler：交易日 15:35 pipeline、周日 03:00 清理、当日去重锁
- `services/sim_engine.py` — **兼容 shim**：re-export kernel 实现，
  生产代码零改动；默认成本常量保持原值
- `services/backtest_service.py` — 实验室回测：`persist=True` 默认把每个
  策略×模式写入 research.db 并返回 run_id（存储失败只告警，不影响回测返回）

## 策略层（core/strategies/）

注册中心模式：`@register_strategy` 装饰器声明 label/rebalance_days/max_positions/
param_schema，`scan_strategies()` 自动发现。11 个策略：

- **technical**：MA Cross、RSI Reversal、Bollinger、MACD
- **portfolio**：Risk Parity（Pairs Trading 已停用——A 股无法做空）
- **ml**：Alpha158+LGB/XGB（walk-forward 月度重训）、LSTM、Transformer（序列模型）
- **factor**：AlphaMaster GBR（13 维特征 GBDT 排序，默认月频——
  日频已被[验证报告](GBR_VALIDATION.md)证伪）

信号优先架构：策略产出无状态信号字典 `{date: [{code, action, score}]}`，
引擎独立回放。策略是可独立测试的纯信号生成器。

## 历史研究系统（research/，根目录）

第一代回测系统，基于 backtrader，用于全历史（2019-2026）批量对比报告。
与新内核的关系：`research/run_backtest.py`（backtrader 引擎）产出静态报告；
`dispatch` 内核产出可积累的实验。新研究一律走 kernel + research.db；
backtrader 系统保留用于长历史全量复核。

## A 股市场规则

| 规则 | 实现 |
|------|---------------|
| T+1 交收 | 信号次日执行，当日买入不可当日卖出 |
| 印花税 0.1%（卖出） | CostModel.stamp_duty |
| 佣金 0.1%（双边） | CostModel.commission |
| 滑点 0.1% | CostModel.slippage |
| 100 股整手 | 买入数量向下取整到 100 的倍数 |
| 现金约束 | 成本+佣金超出可用现金的买单被拒绝 |

## 数据流

```
AKShare (主数据源)
    ├─ stock_zh_a_daily(前复权) ──► 个股日线 ──► core/data/cache/full_<pool>_*.parquet
    ├─ stock_zh_index_daily ──────► CSI 500 基准 ──► cache/index_000905.parquet
    └─ tool_trade_date_hist_sina ─► 交易日历
BaoStock (备用)

data_service.auto_update(): 比对缓存截止日与最近交易日，缺的天数多线程补齐
signal_service: pivot(收盘价矩阵) × 参数 = 策略信号字典
    ├─ signal cache: state/signals_cache/{scope}/{strategy}/{params_hash}.json (原子写)
    ├─ model store:  state/models/{scope}/{strategy}/{params_hash}/{retrain_date}.pkl (skip if hit)
    └─ walk-forward: fd <= rt_date 严格无前视
sim_engine: 信号 × pivot × 成本模型 → 逐日快照 + 逐笔成交
research.metrics / store: 快照 → 36 项指标 → research.db (含 params_hash)
web: research.db → 排行榜/详情/对比页
```

## 已知局限

- **幸存者偏差**：akshare 仅提供当前指数成分股，历史回测全部使用当前成分
  （P3 计划修复，需另找历史成分数据源）。所有实验结论需带此前提阅读
- **前复权锚点**：qfq 价格锚定最新交易日，窗口内有公司行为时历史行可能轻微漂移
  （已接受的折衷，见 data_service 注释）
- **无容量模型**：模拟不考虑大单冲击成本（P3）

## 依赖

| 包 | 用途 |
|---------|---------|
| flask + apscheduler | Web 服务与调度 |
| pandas, numpy | 数据处理 |
| matplotlib | 图表（Agg 后端，base64 嵌入） |
| akshare / baostock | A 股数据 |
| lightgbm, xgboost, torch, sklearn | ML 策略 |
| statsmodels | 协整检验（pairs trading） |
| backtrader | 历史研究系统（research/ 根目录） |
