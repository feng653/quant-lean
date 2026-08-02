# 研究数据源与版本管理

## 为什么页面曾一直显示“股票池缓存不可用”

旧数据中心把两个不同概念放在同一张表中：

- `data/cache/*.parquet` 是旧运行时行情缓存；
- `data/pit_evidence/provider_candidates` 是 Tushare 原始候选证据。

`POST /api/data/update` 对应未来实盘级 raw/adjusted 双账本。该更新器尚未获准，所以它会
明确返回 `pit_dual_price_update_not_authorized`。候选证据即使已经下载，也不会被旧缓存表
识别。这是页面“不能刷新”的直接原因，不表示 Tushare 历史成分没有保存。

## 当前研究路径

研究路径和生产路径已经分离：

```text
Tushare 原始响应 / checkpoint
        │ 完整性和请求范围复核
        ▼
data/research_data/generations/<hash>.sqlite
        │ active.json 原子切换
        ▼
探索研究 / 个人模拟（风险告警随数据返回）
```

研究 generation 不写入 PIT master、生产价格账本或旧 Parquet 缓存，并永远返回
`live_eligible=false`。局部 checkpoint 分类为 `single_source_research`；只有从 2016-01 到
自动探测截止月的四指数、所需行情窗口和可选数据源都无采集失败时，才可成为条件性的
`tushare_research_trusted`。`daily + adj_factor` 是可计算行情的最低要求；`daily_basic`、
停牌和基准缺失会保留为空值及告警，不会丢弃已有价格。

行情同时保存 raw OHLC、`adj_factor` 和 `hfq=raw*adj_factor`。Tushare `vol`（手）、
`amount`（千元）、总/流通股本（万股）和总/流通市值（万元）分别归一化为股、人民币、股和
人民币；原始响应仍由内容寻址 artifact/JSON 留存。因子研究可直接读取 turnover、量比、
PE、PB、股本和市值 typed 字段。

## 数据管理 API

| API | 用途 |
|---|---|
| `GET /api/data/research-sources` | 数据源安装、配置、实际留存状态、能力和最后观察 |
| `POST /api/data/research-sources/refresh` | 管理员提交有调用预算的 Tushare 研究刷新任务 |
| `GET /api/data/research-sources/conflicts` | Tushare 研究池与本地已激活池的具体成员差异和未比较原因 |
| `GET /api/data/research-pools/{pool_id}/stocks?date=YYYY-MM-DD` | 查询指定日期之前最近的月度研究成分 |
| `GET /api/data/update/status` | 同时返回研究代/研究池状态和独立的旧行情缓存诊断 |

刷新请求示例：

```json
{
  "source_id": "tushare",
  "from_month": "2016-01",
  "max_calls": 16
}
```

省略 `to_month` 时系统每 24 小时探测最近已结束月份；供应商仍滞后时保留旧截止月和探测
receipt，新月份齐备后扩展 plan，并按内容哈希复用旧 checkpoint 任务，不会从 2016 年重抓。
任务每批有调用预算，有实际进展时会自动排队续跑；可选接口的瞬时失败最多自动紧接重试
一次，持续失败等待下一次显式刷新，避免任务忙循环。没有已对账行情会话时不发布空研究代；
之后按首次行情、新增 252 个会话或任务完成节流发布，避免每批重写数 GB 文件。BaoStock 当前只列为校验源，远端
登录和历史指数能力未验证前不可选择刷新；页面显示“未比较”，不会显示“无冲突”。

## 当前可信边界

- 已物化：Tushare 四个 CSI 指数的完整月度成分/权重 artifact（以本机 checkpoint 实际
  完成范围为准）。
- 已物化范围以活动 generation 的 `market.date_start/date_end`、会话数、证券数和各数据集
  row count 为准；checkpoint 未完成部分、benchmark 缺口和跨源未比较均显式告警。
- 月度快照可以显著降低使用当前成分造成的幸存者偏差，但不能证明月内精确调样时点；
  `available_at` 缺失也可能产生前视风险。
- 这些问题对研究和模拟是醒目告警；对未来实盘仍是硬阻断。

跨源报告覆盖成员、权重两侧值/差值/容差，并列出行情、指标、停牌和事件的未比较原因。
`activated_local` 与 Tushare 的来源独立性尚未证明，即使集合一致也只显示
`match_not_independent`，不得把 `conflict_count=0` 解读为全数据无冲突。
