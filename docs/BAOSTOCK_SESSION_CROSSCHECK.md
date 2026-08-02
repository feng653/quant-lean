# BaoStock 历史逐日状态交叉证据

`backend.data.sources.baostock_session_crosscheck` 只处理 Tushare 会话求交报告中已经
明确列出的 `code/date/reason` blocker。它通过 BaoStock SDK 读取当天
`tradestatus/volume/amount`，将 SDK 返回的原始字符串表按 SHA-256 保存到独立隔离区，
帮助治理复核人员判断“缺 daily + 停牌 timing 不明确”等候选证据。

这不是新的生产数据源，也不会自动消除 Tushare blocker。即使两个候选源一致，仍需
官方停复牌/交易所证据、供应商许可与独立治理复核；报告始终返回
`production_pit_ready=false`、`runtime_data_changed=false` 和
`tushare_blocker_resolved=false`。

## 输入与有界运行

输入只允许显式 blocker 对，不接受账号、密码、token、任意 provider 参数或股票池：

```json
{
  "schema_version": "baostock-session-crosscheck-input/v1",
  "blocker_pairs": [
    {
      "ts_code": "000002.SZ",
      "trade_date": "2016-01-04",
      "tushare_reason": "suspend_without_daily_semantics_ambiguous"
    }
  ]
}
```

一次调用最多请求 64 对；默认 8 对。计划经规范排序后生成稳定 `run_id`，每一对成功
写入内容寻址 artifact 后才原子推进 0600 checkpoint，重复执行只继续剩余对：

```bash
.venv/bin/python scripts/collect_baostock_session_crosscheck.py \
  --input /absolute/quarantine/path/baostock-blockers.json \
  --max-calls 8
```

SDK 仅在存在待处理对时登录一次，并在查询成功或异常时执行 `logout()`。Provider 的
`error_msg` 不写进异常、checkpoint、报告或日志；SDK import、login、query、结果迭代和
logout 的 stdout/stderr 也由不留存 sink 捕获，因此 CLI 只打印单个结构化 JSON。默认隔离区：

```text
data/pit_evidence/provider_candidates/baostock_session_crosscheck/
  artifacts/sha256/
  manifests/sha256/
  reports/sha256/
  checkpoints/
```

BaoStock 日线接口没有历史 `available_at` 或 provider revision 字段，因此 manifest 只把
采集时间声明为可得时间上界，把响应摘要声明为 observation revision，并持续保留
`historical_available_at_not_proven` 与 `provider_retention_terms_unverified`。

## 保守分类

| BaoStock 行 | 候选分类 | 允许的解释 |
|---|---|---|
| `tradestatus=0` 且 volume/amount 均为 0 | `provider_reports_not_trading_without_liquidity` | 第二候选源报告当天未交易；不是权威全天停牌证明 |
| `tradestatus=1` 且 volume/amount 均为正 | `provider_reports_trading_with_liquidity` | 第二候选源观察到流动性；不能解释 Tushare 停牌 timing |
| 状态与流动性冲突 | `provider_state_liquidity_conflict` | 保持 blocker，转人工/官方复核 |
| 缺行、零流动性但状态为 1、未知状态 | `*_ambiguous` | 保持 blocker |

`annotate_tushare_session_reconciliation()` 是唯一可选集成入口。它验证交叉报告 hash，
只给完全匹配的 Tushare `trade_date/code/reason` 增加 receipt 与对照结果；原 `blockers`
和 `valid` 原样保留。任何调用方都不得以候选一致代替官方治理审查。

## 验证

```bash
.venv/bin/ruff check \
  backend/data/sources/baostock_session_crosscheck.py \
  scripts/collect_baostock_session_crosscheck.py \
  backend/tests/test_baostock_session_crosscheck.py

.venv/bin/python -m pytest backend/tests/test_baostock_session_crosscheck.py -q
```

测试完全使用 fake SDK，不联网、不写 `data/`、不读取 `.env`，覆盖登录失败脱敏、异常
登出、调用预算/续跑、内容寻址、同意/分歧分类、报告篡改拒绝和 blocker 不变性。
