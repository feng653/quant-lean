# A 股日线双源核验 v2

日期：2026-07-31

## 结论

生产研究日线改为：

1. 主源：BaoStock 独立公共接口；
2. 复核源：AKShare 的新浪日线适配器；
3. 原始价格门禁：两源 `raw` 代码覆盖、交易日期、OHLC 截面形状、收盘日收益
   和成交量变化必须全部通过，`max_conflict_ratio=0`；
4. 研究价格：只在 raw 门禁通过后，使用 BaoStock `raw close/preclose`
   recurrence 重建 `hfq`，并验证因子有限、为正及公司行为跳变证据；
5. 新浪 hfq 仅作为 informational 差异观察，绝不参与通过投票，也不把不同
   供应商的复权因子口径差异伪装成 raw 数据错误或“已通过”。

双源一致只能提供公共聚合源级别的异常检测，不能冒充交易所授权数据，也
不能解决历史指数成分的幸存者偏差。正式缓存仍标注
`public_cross_validated_research_only`，不得作为原始成交价账本使用。

## 故障定位

失败请求覆盖 288 只沪深 300 当前成分股、2015-01-01 至 2026-07-31。
原链路以腾讯 hfq 为主源、以新浪 hfq 为复核源：

- 112,440 个重叠日收益中出现 11,667 个冲突；
- 多只股票因为停牌产生 panel 联合索引中的空值，被旧比较器误判为数据缺失；
- 小样本中腾讯与新浪、BaoStock 的日收益长期系统性偏离，不是恒定复权锚、
  日期偏移或普通四舍五入；
- 新浪原始 OHLCV 与 BaoStock 原始 OHLCV 基本一致，成交量只有固定单位尺度
  差异时也可被正确识别；
- BaoStock 直接返回的个别预复权区间存在因子连续性异常，因此不能未经内部
  约束直接用作独立复核。

本机只读 smoke 使用 `000002`、`000063`、`600519`，覆盖
2015-01-01 至 2026-07-30。这三只股票包含长期分红/公司行为，且中兴通讯
样本含停牌区间。新链路比较 8,224 个重叠收益，冲突为 0；最大日收益差分别
为 0.000384、0.000324、0.001307。该结果只证明新适配语义在样本上相容，
不降低全量请求的 fail-closed 门槛。

## 原始价格门禁与后复权语义

BaoStock 主源只请求 `adjustflag=3` 的原始 OHLCV、`preclose` 和
`tradestatus`，先排除明确的停牌行，再按其官方公布的后复权递推重建价格：

```text
factor[t] = factor[t-1] * raw_close[t-1] / raw_preclose[t]
hfq_ohlc[t] = raw_ohlc[t] * factor[t]
```

首个观测的 factor 固定为 1。不同提供商不仅可以选择不同绝对锚点，也可能
采用不同公司行为口径，因此严格发布门禁比较的是两源 raw 收益、OHLC/close
比例和成交量，而不是要求两家 hfq 精确相等。重建过程另外校验 recurrence、
有限性、正值，并持久化最多 20 个公司行为 factor 跳变样例。新浪 hfq 比较
保留原有零冲突阈值和真实 `acceptable` 结果，但标记为 informational；其失败
不会覆盖已经通过的 raw 门禁，也不会被改写成“通过”。

该公式与 BaoStock 的官方说明一致：[BaoStock 复权因子简介](https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf)。
BaoStock 包及查询字段来自其维护者发布的
[PyPI 项目说明](https://pypi.org/project/baostock/)；依赖锁定为 0.9.3。
新浪接口的 hfq、OHLCV 和成交量字段依据
[AKShare 官方股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html)。

## 核验规则

- 两源必须绑定不同 `upstream_id`，禁止同一上游的两个包装器冒充独立来源；
- 两源必须具有相同的 `raw` 调整语义和完整股票代码覆盖；
- 停牌造成的整行共同缺失是合法状态；任一 OHLCV 字段单独缺失会失败；
- 两源成交日期集合必须一致，不能用交集静默删除冲突日；
- 收盘日收益差、open/high/low 相对 close 的形状差超过 0.5% 会失败；
- 成交量允许“股/手”的恒定单位比例，但零值掩码或相对该恒定比例的异常
  变化超过 0.5% 会失败；
- `max_conflict_ratio` 保持为 0：不存在通过提高冲突比例阈值放行错误值；
- 正式 provenance 分别记录 `all_batches_raw_cross_validated` 与
  `all_batches_adjusted_factor_validated`；后复权研究缓存必须两者都为真；
- 任一失败保留旧正式缓存，绝不混合、修补或降级为单源。

## 可恢复暂存

双源请求在主源完整返回后写入独立的 `data/staging/market-validation`：

- key 绑定代码集合、日期范围、provider、endpoint、adjustment 和 adapter；
- Parquet 与元数据均为 0600，目录为 0700；
- 元数据和数据文件分别做 SHA-256、大小、schema、证据和 DataFrame digest
  校验；
- 采用同目录临时文件、`fsync` 和 `os.replace` 原子发布；
- 48 小时 TTL，过期、篡改、软链接或宽权限全部拒绝；
- 复核源故障或进程重启后可复用完整主源，不复用部分结果；
- 双源成功后立即删除暂存；研究读取路径永远不读取暂存目录。

暂存不是 schema-v4 正式缓存，不能绕过数据质量或 provenance 门禁。

## 进度与恢复

数据更新 job 在 `result.market_data_progress` 中持续公开：

- `source_role`：`primary`、`reference`、`validation` 或
  `adjusted_reference`（仅 informational hfq 观察）；
- `provider`：当前提供商身份；
- `completed_codes` / `total_codes`：完成股票数；
- `reused_staging`：是否从安全暂存恢复。

前端沿用 job 的 `progress`、`current_stage` 和 `progress_message` 即可展示，
不再在下载数百只股票时长期停留在 0.1。

## 事件循环隔离与失败证据

全量 pandas 双源比较、证据 digest、staging Parquet/hash，以及正式 cache 的
质量检查和 Parquet 读写都通过单线程、进程内的
`quant-data-integrity` executor 执行。它不使用 `fork` 或
`ProcessPoolExecutor`，不会复制大型 DataFrame；并发硬上限为 1，适合 8 GB
主机。等待协程被取消时会立即停止后续发布，已经进入原生库的单次调用在后台
收尾，其计算结果会被丢弃。若取消发生在原子文件操作已经开始之后，该次操作
仍可能完成完整替换，但不会留下半写文件，也不会继续后续链路。

双源失败会在 job result 中保存
`cross-source-failure-summary/v1`，最多包含 10 个冲突代码和 10 个样例：

- 每只股票分别记录 return、OHLC geometry、volume scale 冲突数与最大差；
- 样例只含六位代码、交易日期、冲突维度和两源收益；
- 不包含本地路径、请求 URL、凭据、完整股票列表或完整 DataFrame；
- 文本 error 截断到 2,048 字符，详细定位使用受限结构化 result；
- 失败仍保留主源 staging，只有双源通过并形成正式证据后才清理。

## 2026-07-31 沪深 300 全量复核

使用只读的 BaoStock 主源 staging、重新下载 Sina 复核源，对 2015-01-01
至 2026-07-31 的 288 只有效成分股进行了全量比较。698,354 个相邻交易日收益
全部具备足够覆盖，但严格零冲突策略检出 3 个 return 冲突，因此请求按设计
失败，主源 staging 被保留：

| 代码 | 日期 | BaoStock 重建 hfq 收益 | Sina hfq 收益 | 绝对差 |
| --- | --- | ---: | ---: | ---: |
| 002602 | 2019-06-06 | -0.543971% | 0.361215% | 0.905186% |
| 600025 | 2020-06-19 | 0.000000% | 1.036269% | 1.036269% |
| 600346 | 2019-06-27 | 2.015113% | 2.779467% | 0.764353% |

针对这三个日期又查询了独立的腾讯 raw/hfq。两家 raw 数据与 BaoStock
`close/preclose` 一致；腾讯 hfq 与 BaoStock 重建收益的差分别约为
0.0436%、0.0000%、0.1127%，均在既有 0.5% 阈值内。事实指向 Sina 在这
  三次除权附近的 hfq 因子差异，而不是放宽 raw 阈值的理由。生产契约因此
改为 BaoStock raw + Sina raw、`max_conflict_ratio=0` 的 fail-closed 门禁；
Sina hfq 与腾讯结果都只作为复权口径的 informational 证据，不能静默投票或
修补正式行情。
