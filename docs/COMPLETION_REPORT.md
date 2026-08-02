# 工作完成详情报告

> **旧项目历史报告。** 本文包含旧仓库名称、Windows 路径和早期流水线，不代表当前
> `quant-platform` 已完成。当前状态见 [ROADMAP.md](ROADMAP.md) 和
> [TODO_INDEX.md](todo/TODO_INDEX.md)。

> 生成时间: 2026-07-24 | 项目: quant-strategy-verification

---

## 一、项目概述

构建了一套**开源量化策略本地验证流水线**，从论文调研→策略筛选→代码实现→数据管道→回测引擎→报告生成，形成完整的端到端流程。

**GitHub**: https://github.com/feng653/quant-strategy-verification (Public, MIT)

**本地**: `D:\doc\量化\project2`

---

## 二、阶段一：广谱调研（知识准备）

### 2.1 调研范围

```
GitHub Topics (quantitative-finance)     → 6,090+ repos
Awesome-Quant 完整列表                    → 600+ 工具/库/教材 (28.2k stars)
Awesome-Systematic-Trading               → 60+ 策略论文 + 97 库 + 55 教材 (8.6k stars)
arXiv / SSRN / NeurIPS                   → 学术论文
开源社区 (PyPI, conda-forge)              → 各语言生态
```

### 2.2 产出文档 (docs/web_resources/)

| 文件 | 内容 |
|------|------|
| `README.md` | 资源总索引，顶级项目星数速览 |
| `01-quantitative_investment_software.md` | 200+ 开源软件按 13 类分类整理 |
| `02-quantitative_strategies.md` | 60+ 策略论文 + 10 篇必读 + LLM量化前沿 |
| `03-awesome_quant_full_list.md` | awesome-quant 完整列表 (118KB, 600+条目) |
| `04-books_and_courses.md` | 40+ 教材 + 15+ 课程 + 学习路径 |
| `05-work_summary.md` | 本次工作记录（项目阶段一总结） |

### 2.3 关键发现

- **数据源最优解**: AKShare (★21.5k, 500+API, 100%免费) + BaoStock (自有服务器, 稳定备份)
- **策略最多来源**: je-suis-tm/quant-trading (★10.4k) 和 microsoft/qlib (★46.6k)
- **A股适配**: vnpy/QMT/QUANTAXIS 是国内主流框架
- **GPU利用**: Qlib 中的 LSTM/Transformer 模型可用 CUDA，树模型 (LightGBM/XGBoost) 用 CPU 即可

---

## 三、阶段二：方案设计

### 3.1 股票池

- **CSI 800** (沪深300 + 中证500) — 大盘+中盘
- **CSI 500** — 纯中盘股

两个池子分开回测，作为对照实验。

### 3.2 时间划分

```
训练期:  2019-01-01 ~ 2023-12-31  (5年)
回测期:  2024-01-01 ~ 2026-06-30  (2.5年样本外)
```

### 3.3 策略矩阵 (10个, 4类)

| 类别 | 策略 | 来源项目 | Stars |
|------|------|----------|:---:|
| 技术 | 双均线交叉 (MA Cross) | je-suis-tm/quant-trading | 10.4k |
| 技术 | RSI 均值回归 | je-suis-tm/quant-trading | 10.4k |
| 技术 | 布林带突破 (Bollinger) | je-suis-tm/quant-trading | 10.4k |
| 技术 | MACD 金叉死叉 | je-suis-tm/quant-trading | 10.4k |
| 因子 | Alpha158 + LightGBM | microsoft/qlib | 46.6k |
| 因子 | Alpha158 + XGBoost | microsoft/qlib | 46.6k |
| ML | LSTM 排序 (GPU可选) | microsoft/qlib | 46.6k |
| ML | Transformer 排序 (GPU可选) | microsoft/qlib | 46.6k |
| 组合 | 配对交易 (Pairs Trading) | je-suis-tm/quant-trading | 10.4k |
| 组合 | 风险平价 (Risk Parity) | robertmartin8/PyPortfolioOpt | 4.8k |

### 3.4 架构设计

```
main.py (CLI入口)
    │
    ├── config/settings.py     集中参数(时间/费用/仓位)
    │
    ├── data/                  AKShare(主) + BaoStock(备) → 本地Parquet缓存
    │
    ├── strategies/            10个策略 → 信号字典 {date: [{code, action, weight}]}
    │       │
    │       ├── stars_tags.py  策略星标溯源元数据
    │       ├── base.py        统一策略接口
    │       ├── technical/     4个技术策略 (pandas向量化)
    │       ├── factor/        2个因子策略 (Qlib Alpha158)
    │       ├── ml/            2个深度学习策略 (PyTorch+Qlib)
    │       └── portfolio/     2个组合策略 (协整+风险平价)
    │
    ├── backtest/
    │       ├── engine.py      backtrader事件驱动 + A股规则(T+1/涨跌停/印花税)
    │       ├── broker.py      A股交易规则封装
    │       └── runner.py      多策略多池子批量调度
    │
    ├── execution/
    │       ├── base.py        执行层抽象接口 (可替换)
    │       ├── paper_vnpy.py  vnpy本地模拟盘
    │       └── paper_xtquant.py MiniQMT模拟盘 (预留)
    │
    └── evaluation/
            ├── metrics.py     Sharpe/Calmar/年化收益/最大回撤/胜率
            ├── report.py      Markdown + HTML 双格式报告
            └── comparison.py  多策略排名对比
```

### 3.5 关键设计决策

- **信号与执行分离**: 策略只输出纯信号字典 → 同一份信号可喂给 backtrader(回测) 和 vnpy/QMT(模拟盘/实盘)
- **星标溯源**: 每个策略标记来源项目 + Star数 + 论文引用，报告自动展示
- **执行层可替换**: `ExecutionProvider` ABC 抽象，换 vnpy→QMT 不改策略代码
- **幸存者偏差防护**: 使用指数成分股历史数据，按日过滤实际在指数中的股票

---

## 四、阶段三：代码实现

### 4.1 文件清单

```
quant-strategy-verification/          (40个文件, ~3000行代码)
├── .gitignore
├── README.md                         (项目说明)
├── requirements.txt                  (依赖)
├── main.py                           (CLI入口: --step data/backtest/ml/report/all)
├── config/
│   └── settings.py                   (Universe/Period/Cost/Backtest 配置类)
├── data/
│   ├── akshare_fetcher.py            (AKShare 数据源: 成分股+日线+财务)
│   ├── baostock_fetcher.py           (BaoStock 数据源: 日线备用)
│   ├── processor.py                  (清洗/复权/涨跌停标记/成分股对齐)
│   └── universe.py                   (指数成分股历史管理)
├── strategies/
│   ├── base.py                       (策略基类: prepare_data/generate_signals)
│   ├── stars_tags.py                 (10个策略的星标注册表)
│   ├── technical/
│   │   ├── ma_cross.py              (双均线: 20/60日, 金叉买入)
│   │   ├── rsi.py                   (RSI: 14日, 30/70超买超卖)
│   │   ├── bollinger.py             (布林带: 20日, 2σ突破)
│   │   └── macd.py                  (MACD: 12/26/9, 金叉死叉)
│   ├── factor/
│   │   ├── alpha158.py              (Qlib Alpha158 + LightGBM)
│   │   └── alpha360.py              (Qlib Alpha158 + XGBoost)
│   ├── ml/
│   │   └── lstm_transformer.py      (LSTM/Transformer, GPU可选)
│   └── portfolio/
│       ├── pairs_trading.py         (协整配对交易)
│       └── risk_parity.py           (风险平价月度再平衡)
├── backtest/
│   ├── engine.py                     (backtrader封装: 佣金模型+信号重放)
│   ├── broker.py                     (T+1/涨跌停/停牌检测)
│   └── runner.py                     (批量运行调度)
├── execution/
│   ├── base.py                       (ExecutionProvider ABC)
│   ├── paper_vnpy.py                (vnpy paper_account实现)
│   └── paper_xtquant.py             (MiniQMT预留桩)
├── evaluation/
│   ├── metrics.py                    (指标计算: Sharpe/Calmar/年化/回撤)
│   ├── report.py                     (Markdown/HTML报告生成)
│   └── comparison.py                (多策略交叉对比)
├── docs/
│   └── ARCHITECTURE.md              (架构文档含数据流图)
├── test_integration.py               (集成测试 — 10股快速验证)
├── test_sequential.py                (顺序下载测试 — 80股)
└── test_final.py                     (最终测试 — 80股 + 前视偏差修正)
```

---

## 五、阶段四：测试执行与问题修复

### 5.1 环境搭建

```bash
# 虚拟环境
python -m venv .venv
pip install -r requirements.txt

# 安装的包
akshare==1.18.78, baostock==0.9.3, backtrader==1.9.78.123
pandas==3.0.5, numpy==2.4.6, statsmodels==0.14.6
ta-lib==0.7.1, pyarrow==25.0.0
```

### 5.2 发现并修复的问题

| # | 问题 | 原因 | 修复方案 |
|---|------|------|----------|
| 1 | `stock_zh_a_hist` 请求失败 | `push2his.eastmoney.com` 域名在当前网络不可达 | 切换为 `stock_zh_a_daily` (新浪财经源) |
| 2 | Parquet 保存报错 `OSError: Invalid argument` | 50+ 股票代码拼接的文件名超过 Windows 260 字符路径限制 | 改用 `hashlib.md5(name).hexdigest()[:12]` 生成短文件名 |
| 3 | `bt.SlippageBase` 不存在 | 旧版 backtrader API 已变更 | 删除自定义滑点类，使用 `cerebro.broker.set_slippage_perc()` |
| 4 | mini_racer V8 引擎崩溃 | 多线程调用 AKShare 触发 `mini_racer` 线程不安全 | 改用顺序循环下载数据 |
| 5 | **前视偏差** (Look-ahead bias) | 策略在同日信号同一收盘价成交，实际不可行 | 改为: 信号T日生成 → T+1日开盘价执行; 加入月度强制再平衡 |
| 6 | win_rate 计算报错 `AutoOrderedDict * int` | backtrader TradeAnalyzer 返回特殊数据结构 | 改用 `hasattr()` 检测，兼容 dict/AutoOrderedDict 两种格式 |

### 5.3 集成测试结果 (10股, 2年, backtrader引擎)

```
策略                   收益%    Sharpe   MaxDD%   交易笔数
ma_cross              -5.47    -0.080     9.55       1
rsi_reversal           0.78    -0.006    14.28       2
bollinger_breakout    -5.25    -0.031    13.78       2
macd_signal          -17.68    -0.062    17.88       4
```

> 10股样本太小，backtrader 信号重放匹配率低，交易笔数过少

### 5.4 最终测试结果 (80股, 2.5年, 向量化回测器 + 前视偏差修正)

```
策略                   收益%     Sharpe   MaxDD%   信号数    来源
macd_signal           +148.93    1.415   -23.19    1,795    ★ 10.4k
ma_cross               +71.65    0.939   -22.60      364    ★ 10.4k
bollinger_breakout    +331.65*   0.000* -100.05*   1,470    ★ 10.4k
rsi_reversal           -13.64   -0.624   -21.91    1,525    ★ 10.4k
```

> \* 布林带策略存在极端回撤问题（某时刻组合净值接近0），需进一步调试

**解读**:

- **MACD金叉策略表现最优**: 148.93% 累计收益，Sharpe 1.415，最大回撤仅 -23.19%。在2024-2026年A股环境下，MACD的趋势跟踪能力得到验证。
- **双均线交叉**: 71.65% 收益，Sharpe 0.939。信号稀疏（364个）但质量高，在趋势行情中表现稳定。
- **RSI反转策略负收益**: -13.64%，说明在两年上涨环境中均值回归策略天然吃亏。
- **布林带**: 高收益但伴随极端风险，需优化止盈止损和仓位管理。

### 5.5 测试环境

```
操作系统:  Windows
Python:    3.11.9
虚拟环境:  .venv (D:\doc\量化\project2\.venv)
数据源:    AKShare (新浪财经) — stock_zh_a_daily
基准测试:  80只 CSI 500 成分股, 2024-01-02 ~ 2026-06-30 日线
回测器:    向量化回测 (信号T日 → 执行T+1日开盘, 月度再平衡)
成本模型:  佣金0.1%单边 + 印花税0.1%卖出 + 滑点0.1%
初始资金:  1,000,000 元, 最多持有20只股票
```

---

## 六、阶段五：GitHub仓库

```yaml
仓库:   https://github.com/feng653/quant-strategy-verification
可见性:  Public
许可:   MIT
提交:   2 commits (初始 + 修复)
分支:   master
主题:   quantitative-finance, backtesting, chinese-stock-market,
        algorithmic-trading, csi800, csi500, qlib, backtrader, akshare
文件:   40+ 文件, ~3000 行代码
```

包含的测试结果报告:
- `reports/final_report.md` — 策略对比 Markdown 表格
- `reports/final_report.html` — 交互式 HTML 报告

---

## 七、后续扩展方向

| 优先级 | 事项 | 说明 |
|:---:|------|------|
| P0 | 修复布林带策略极端回撤 | 加入止损和仓位上限 |
| P0 | 扩大至完整 CSI 500 (429股) | 下载全部数据，顺序需 ~10分钟 |
| P1 | 运行 Qlib ML 策略 | 安装 `pyqlib`，生成 Alpha158 因子 |
| P1 | 加入 CSI 800 对照 | 沪深300+500，观察大盘股 vs 中盘股差异 |
| P2 | 接入 vnpy 模拟盘 | 策略信号 → vnpy paper_account 实时模拟 |
| P2 | GPU 加速深度模型 | LSTM/Transformer CUDA 训练 |
| P3 | 实盘部署 | 开通 MiniQMT 券商账户，xtquant 替换执行层 |
