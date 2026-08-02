# 已治理股票池的 PIT 展示契约

本契约适用于 `csi300`、`csi500`、`csi800`、`csi1000` 的数据 API、数据中心和新建实验页面。
它解决的是**页面展示的时间语义**；实验、调优和模拟的运行时门禁仍要求精确 PIT 覆盖，不能因为页面能显示周末数据而放宽。

## 唯一数据来源

四个池的 `/api/data/pools`、`/api/data/pools/{pool_id}` 和
`/api/data/pools/{pool_id}/stocks` 只读取本机 PIT master 中已经激活的
`index_membership` 批次。它们不得：

- 初始化 `UniverseManager`、AKShare 或其他网络数据源；
- 读取 `pool_*.json` 等现时/静态成分缓存；
- 写入、激活、补齐或替换任何 PIT 批次；
- 将当前成分伪装为历史成分。

没有激活证据时接口返回空成分和 `availability.ready=false`，而不是回退。

## 日期解析

每个响应都同时返回：

| 字段 | 含义 |
|---|---|
| `requested_as_of` | 客户端请求的自然日 |
| `resolved_as_of` | 实际读取的 PIT 观察日期；不可用时为 `null` |
| `resolution` | `exact_activated_observation`、`weekend_prior_activated_observation` 或 `unavailable` |
| `staleness_calendar_days` | 请求日相对解析观察值的自然日差；精确值为 `0` |

精确日期有完整、已激活且非空的成分证据时，`resolved_as_of` 必须等于
`requested_as_of`。

只为人机页面提供一个窄的例外：周六或周日的精确证据缺失时，可以只读使用**紧邻的周五**已激活观察值。响应会附加
`point_in_time_display_uses_prior_activated_observation` 和陈旧天数风险标记。

任何周一至周五的缺口均失败关闭；实现不会猜测交易日历、越过缺失工作日，或向前寻找更旧记录。工作日恰逢休市而未接入权威日历时也会保守地失败关闭。

## 前端行为

新建实验页在 PIT 股票池不可用时显示原因，并明确说明不会回退或联网抓取；周末显示时会提示解析日期。数据中心把成分证据与独立的行情缓存状态分开：行情数据能否用于实验，还必须经过 PIT、双价格账本和可交易状态门禁。

## 验收

单元测试覆盖：

1. 周日只能解析到紧邻周五的已激活记录，并报告两天陈旧；
2. 缺失工作日只查询该日并返回失败关闭；
3. 未初始化 PIT store 的股票池读取不初始化公共数据源，且返回结构化不可用状态；
4. 详情接口返回 PIT 请求/解析日期与 lineage，不使用现时 UniverseManager。
