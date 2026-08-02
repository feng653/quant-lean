# 非机器学习策略前端批量调优

## 范围与研究边界

`scripts/run_frontend_non_ml_tuning.mjs` 为注册中心当前全部 15 个
`requires_training=false` 策略执行同一份预注册协议：

- 15 个默认参数基准实验；
- 129 个选模窗口参数组合；
- 每个策略只允许一个组合晋级，共 15 个锁定测试实验；
- 合计 159 个实验，45 次实验类写请求均由真实 Chromium 页面操作触发。

协议位于 `scripts/frontend_tuning/non_ml_tuning.v1.json`。Python 契约测试会把它
与后端策略注册中心逐项核对，策略增删、版本变化、参数失效或组合数变化都会使
测试失败。

数据是 30 股、2015-01-01 至 2024-03-29 的确定性合成行情，证据等级为
`declared`。结果仅用于软件链路和参数研究流程验收，不代表真实市场表现，
不得用于实盘准入或收益宣传。

## 固定研究协议

| 阶段 | 区间 | 实验数 |
|---|---|---:|
| 默认参数基准 | 2023-07-31 ~ 2023-12-29 | 15 |
| 参数选模 | 2023-07-31 ~ 2023-12-29 | 129 |
| 锁定测试 | 2024-01-02 ~ 2024-03-29 | 15 |

主排序指标是选模 Sharpe。距离最优值不超过 0.02 的近似并列组合，依次按
最大回撤绝对值较小、年化收益较高、胜率较高、距离默认参数较近、实验 ID
较小确定唯一晋级项。锁定测试指标不返回选模阶段，也不允许据此改选参数。

参数组合数：

| 策略 | 组合数 |
|---|---:|
| liquidity_factor_v1 | 12 |
| low_volatility_v1 | 12 |
| multi_factor_score_v1 | 12 |
| momentum_cross_v1 | 12 |
| short_reversal_v1 | 9 |
| composite_regime_v1 | 6 |
| composite_equal_v1 | 3 |
| composite_riskparity_v1 | 3 |
| composite_momentum_v1 | 3 |
| risk_parity_v1 | 12 |
| macd_signal_v1 | 12 |
| bollinger_breakout_v1 | 9 |
| ma_cross_v1 | 9 |
| donchian_breakout_v1 | 6 |
| rsi_reversal_v1 | 9 |

## 前置条件

1. 后端和前端分别运行在 `http://localhost:8000` 与
   `http://localhost:5173`。实时模式拒绝非 loopback 地址、URL 内嵌凭据和
   带路径的来源。
2. 前端必须包含参数扫描 URL 恢复契约：
   `?baseline_id=<id>` 和 `?sweep_id=<id>`，以及 JSON 数组形式的自定义扫描
   值解析。当前主线提交 `c1bce5f` 已包含该能力。
3. 运行账户必须是管理员，或同时拥有 `data:read`、`strategies:read`、
   `experiments:read`、`experiments:create` 和 `experiments:sweep`。
4. 本地缓存 `custom_2ee693c36bca7e34` 必须通过
   `POST /api/data/experiment-readiness` 原子硬门禁：schema 4、30 股、2412 日、
   qfq、六个固定字段、完整 declared 来源覆盖、与协议一致的代码摘要和帧摘要，
   以及覆盖选模至锁定测试窗口的本地 `000300` 基准。端点虽使用 POST 传递检查
   条件，但语义只读，不初始化外部数据源、不写缓存，也不会回退到联网抓取。
5. Playwright 作为外部工具模块提供，避免把浏览器测试依赖加入生产前端包。

## 凭据与环境变量

不要把用户名或密码写入脚本、配置、命令参数或 Git 文件。可在当前终端临时输入：

```bash
read -r "QUANT_TUNING_USERNAME?调优用户名: "
read -rs "QUANT_TUNING_PASSWORD?调优密码: "
echo
export QUANT_TUNING_USERNAME QUANT_TUNING_PASSWORD

export QUANT_TUNING_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs
export QUANT_TUNING_ARTIFACT_DIR=/tmp/quant-platform-non-ml-tuning-20260730
```

若 Playwright 自带 Chromium 不可用，可另设
`QUANT_TUNING_BROWSER_EXECUTABLE` 为本机 Chromium 可执行文件。报告和
checkpoint 不保存 token 或密码，目录权限为 0700，JSON 与失败截图为 0600。
结束后执行：

```bash
unset QUANT_TUNING_USERNAME QUANT_TUNING_PASSWORD
```

## 三阶段运行

本机完整执行优先使用交互式父启动器。它只在当前 shell 内保存凭据，对报告中明确
分类为瞬态的网络或页面导航故障最多续跑 5 次；其他错误立即退出。续跑重新进入
checkpoint/intent 恢复流程，不会直接重放某个 POST：

```bash
/bin/zsh scripts/run_frontend_non_ml_tuning_interactive.sh
```

可用 `QUANT_TUNING_RESUME_ATTEMPTS=1..10` 调整有限续跑次数。不要在脚本、配置或
命令参数中设置用户名和密码。

先做纯离线协议检查。它不会登录、不会创建 checkpoint、不会提交实验：

```bash
node scripts/run_frontend_non_ml_tuning.mjs --dry-run
node --test scripts/frontend_tuning/*.test.mjs
```

再做真实浏览器预检。它只从登录页登录，并通过浏览器执行只读 API 门禁；
除了登录/必要的 token 刷新，不提交实验：

```bash
node scripts/run_frontend_non_ml_tuning.mjs --live-preflight
```

人工复核 dry-run 报告、机器负载和队列状态后，才解除 159 次执行锁：

```bash
export QUANT_TUNING_EXECUTE_CONFIRM=159_FRONTEND_EXPERIMENTS
node scripts/run_frontend_non_ml_tuning.mjs --execute
```

同一 artifact 目录只允许一个执行进程。存活锁会阻止重复提交；进程异常退出后，
下一次运行会保留旧锁证据并接管。不要同时用不同 artifact 目录启动同一 campaign。

## 恢复与结果查看

每个基准提交、扫描提交、晋级及完成状态都会原子写入 `checkpoint.json`。使用
相同配置和 artifact 目录重新执行 `--execute`，运行器会：

- 在点击提交前持久化确定性的提交 intent；
- 按当前账户、精确名称和策略只读查询已提交记录，核对股票池、窗口、参数、模式、
  `cache_only` 策略和来源实验；
- 等待已经提交的基准，不重复创建；
- 通过 `/experiment/sweep?sweep_id=<id>` 恢复每个扫描页；
- 重新验证选模窗口、锁定窗口、全部扫描成员的 `cache_only` 继承和唯一晋级组合；
- 等待已经创建的锁定测试，并打开详情页确认前端可见。

配置摘要、前后端来源或运行账户变化时旧 checkpoint 会被拒绝。若浏览器 POST
已成功但返回 ID 尚未写入 checkpoint，下一次执行会找回唯一精确匹配记录；多个
同名候选或任一身份字段不符时 fail-closed。扫描成员失败或取消时运行器停止，
不会静默缩小候选集或换用次优结果。

最终 `report.json` 包含基准指标、扫描 URL、入选参数、选模指标、近似并列 ID、
晋级来源及锁定测试指标。全部实验还可从以下页面查看：

- `/experiment`：159 个实验的状态与详情；
- `/experiment/sweep?sweep_id=<id>`：该策略全部候选及人工晋级状态；
- `/experiment/<id>`：基准、候选或锁定测试的完整数据。

失败报告会保留阶段、脱敏错误和隐藏登录信息后的截图。浏览器写保护会阻断未在
当前 UI 动作中授权的 POST，并在报告中列出实际观察到的写请求类型和路径。
