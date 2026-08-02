# Tushare PIT 候选契约限量实证（2026-08-02）

## 结论

本次通过本机 loopback HTTP 代理 `127.0.0.1:12001` 完成 quarantine-only 实证，证明
当前账户和技术链路能够读取若干 2016 至今的离散四指数月末权重、30 只样本证券的同日
raw 行情/复权因子/规模、上市退市清单、停牌和部分公司行为。它**不能**证明连续 PIT
历史、首次可得时间、供应商旧 revision 留存或本地研究留存许可，所以不得导入/激活
生产 PIT，也不得清理旧数据。

内容寻址汇总报告：

- report SHA-256：`798260e89f8683f4fd37755605b73406960c7e102e86bc022e28e139b08c1d95`
- stored report SHA-256：`e50fca537150182de9b83e355ff11541594a45f04832717e7bcd3c371ddc7a02`
- classification：`quarantine`
- `candidate_collection_valid=true`，仅表示本次限量契约样点完整
- `production_pit_ready=false`、`promotion.eligible=false`

精确响应字节和 manifest 保存在运行机的
`data/pit_evidence/provider_candidates/tushare/{artifacts,manifests,reports}/sha256`，未提交
供应商原始数据到 Git。探测未调用 PIT master、行情 cache、import、approval 或 activate。

## 四指数稀疏月份

| 指数 | 2016-01 | 2020-01 | 2023-01 | 2025-01 | 2026-06 | 2026-07 |
|---|---:|---:|---:|---:|---:|---:|
| 沪深 300 `000300.SH` | 300 | 300 | 300 | 300 | 300 | 0 |
| 中证 500 `000905.SH` | 500 | 500 | 500 | 500 | 500 | 0 |
| 中证 800 `000906.SH` | 800 | 800 | 800 | 800 | 800 | 0 |
| 中证 1000 `000852.SH` | 1000 | 1000 | 1000 | 1000 | 1000 | 0 |

四个指数在每个非空月都只有一个供应商 `trade_date`；2016-01、2020-01、2023-01、
2025-01、2026-06 分别为月内最后交易日附近的完整候选快照。2026-07 的完整自然月请求
均 HTTP 成功但返回空表。因为 2026-06 同一权限立即返回完整行数，当前证据排除了“此
账户完全没有 index_weight 权限”，但不能在没有供应商答复时区分发布滞后、留存策略或
其他 entitlement。报告将最近观察到的完整月记为 2026-06、其后的首个空月记为
2026-07，并明确 `cutoff_is_exact=false`。

这些离散月份之间没有逐月采集，不能从五个成功样点推导 2016 至今无空洞；指数权重
端点也不提供历史 `available_at`，因此仍不能直接形成无前视 PIT 时间线。

## 30 只证券与事件契约

样本从四指数 2026-06 完整候选快照按指数轮转、代码去重后确定性选择 30 只证券；同日
横截面是 2026-06-30：

| 数据集 | 供应商横截面行数 | 样本命中 | 结论 |
|---|---:|---:|---|
| `daily` raw+amount | 5508 | 30/30 | 候选横截面完整 |
| `adj_factor` | 5534 | 30/30 | 候选横截面完整 |
| `daily_basic` 规模/股本 | 5508 | 30/30 | 候选横截面完整 |
| `trade_cal` | 1 | 当日开市 | 候选日历观察 |
| `suspend_d` | 23 | 不适用 | 当日停牌候选；未出现不能证明无事件 |

证券主表返回在市 5534、退市 339、暂停上市 0。`stock_basic` 是当前查询观察，不保留
供应商历史版本；`list_date/delist_date` 可以作为 effective 候选，仍缺每次修订的
available/revision 历史。

对确定性样本前 5 只证券各请求 `dividend` 和 `namechange`：分红分别返回
42、20、39、27、35 行；名称/ST 变化分别返回 2、1、1、1、1 行。`ann_date` 有供应商
字段覆盖；分红 `imp_ann_date` 只有部分行非空（17/42、6/20、10/39、7/27、10/35）。
因此可以构造公司行为候选，但不能把缺失 `imp_ann_date` 或空事件表解释为权威“无事件”
证明，必须继续与巨潮/交易所公告对账。

## 自动服务与代理实证

正式 `com.quant-platform.backend` LaunchDaemon 的 plist 和活动环境只有 PATH、
`ENVIRONMENT`、`PYTHONUNBUFFERED`，没有 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY`。因此 shell
中手工设置系统代理不能保证后台任务联网。新增的
`PIT_CANDIDATE_OUTBOUND_PROXY_URL`：

- 只允许 `http/https` loopback 和显式端口；
- 使用 `SecretStr`，完整 URL 不进入 job、artifact、report 或日志；
- 只传给 quarantine-only Tushare 客户端；
- 报告仅保留 `explicit_proxy_configured=true`、`proxy_boundary=loopback_only` 和
  `proxy_url_retained=false`。

本次完整探测的报告确认以上三个脱敏字段；代理地址和供应商 token 均不在报告中。

## 尚未关闭的生产门禁

1. 获取并归档 Tushare 对本地自动批量获取、长期留存和历史 revision 的书面许可。
2. 对 2016 至今每个目标月份/交易日完整采集四指数事件或月末权重，验证无缺口、重复、
   重叠和跨指数冲突；离散探测不能替代回填。
3. 用中证历史调样公告/文件确认 effective/available 时间，并对至少 20 个事件对账。
4. 补齐证券、行业、ST、停复牌、上市退市和逐日规模的历史 `available_at`/revision。
5. 建立 raw、调整因子、公司行为与研究价的确定性复算，随后才可进入受治理导入、原子
   激活和运行绑定。

在这些条件完成前，本报告只推进 Q-01/NEXT-01/02 的技术 PoC，不关闭 Q-01，不允许
Q-02 生产导入，也不授权非 PIT 数据清理。
