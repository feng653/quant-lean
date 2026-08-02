# PIT-only 数据运行边界

> **未来实盘/严格认证策略。** 本文的统一硬门禁不再适用于当前个人探索研究和模拟盘。
> 当前规则是数据可信度问题告警、技术完整性问题阻断、live 恒拒绝；见
> [ROADMAP.md](ROADMAP.md) 和 [RESEARCH_DATA_MANAGEMENT.md](RESEARCH_DATA_MANAGEMENT.md)。

## 结论

未来实盘及严格认证研究只消费本地、已激活、可按研究日重建的数据。当前成分快照、
`all_a`、自定义静态集合、quarantine/pending-review 包、legacy Parquet 和运行时
联网结果都不是可运行数据。

统一门禁位于 `backend/data/pit_runtime.py`，并由每个写入口和后台 worker 双重
执行。提交时通过不代表任务启动时仍通过；worker 会重新核验同一窗口。

| 正式入口 | 用途门禁 | 拒绝发生时点 |
|---|---|---|
| 单实验、精确重跑 | PIT 成分、双价格账本、权威日历、PIT 基准、cache-only | 创建记录和入队之前；worker 再检 |
| 参数扫描、锁定测试 | 上述全部 + real-tuning readiness | sweep/子实验写入之前；worker 再检 |
| 因子研究 | PIT 成分、双价格账本、权威日历、可信研究价 | job 入队之前；worker 再检 |
| 本地/远程模型重训 | PIT 成分、双价格账本、权威日历、训练用途 readiness | 读取或训练之前 |
| 每日模拟、历史回放 | PIT 成分、raw/hfq 双角色、权威日历/状态/公司行为 | job 入队之前；run row 和订单之前 |
| 自动更新 | 仅官方证据采集 | 只写治理 evidence root 与 review queue |

## 自动更新

`POST /api/data/update` 只允许 `csi300/csi500/csi800/csi1000`。一次任务遍历完整、
未过滤的中证公告归档，保存内容寻址证据、checkpoint、coverage report 与独立复核
队列。它固定返回：

- `automatic_approval_permitted=false`
- `production_import_performed=false`
- `activation_performed=false`
- `runtime_data_changed=false`

因此自动采集不能把最新成分、解析提案或未复核附件变成运行时真相。必须完成独立
逐行复核、权威交易日历绑定、四个 CSI scope 的治理 receipt，并原子激活后，查询
路径才可能看到批次。无人值守采集还必须配置正整数
`PIT_AUTOMATION_ACTOR_USER_ID`；默认 `0` 会停止自动采集，禁止伪造管理员身份。

自动任务还会校验包 ID、覆盖区间和三个隔离产物的目录边界、文件类型、大小与
SHA-256。API 只返回内容摘要，不泄露 evidence root 的本机绝对路径；手工触发和
模拟盘调度共享同一个全局去重资源键，不能并发重复采集全量公告归档。

## 历史实验只读隔离

升级前的实验不会删除，仍可在实验列表中审计；它们默认标记为
`legacy_read_only=true`。用于参数候选、策略最佳实验、相关性/组合分析、精确重跑、
调优或部署时，服务端会重新校验 manifest 的 hash/实验身份、cache-only 策略、PIT
timeline、canonical price binding、质量与基准证据。缺任一项即拒绝，不因
`completed` 或历史收益较高而放行。

具体安全控制和残余风险见
`docs/reports/PIT_RUNTIME_SECURITY_HARDENING_20260801.md`。

## 生产与测试隔离

生产数据库没有 PIT 批次时，所有正式入口返回结构化 409 或 worker fail-closed，
不会创建正式研究结果、模拟订单或模型。测试可以在 `tmp_path` 下构造经 receipt 与
activation 的 PIT fixture，并注入用途级 readiness；fixture 的 SQLite、Parquet、
快照和证据根均位于临时目录，不读取、复制或写入 `data/` 生产目录。测试绕过只靠
pytest monkeypatch/in-memory fake，没有环境变量、HTTP 参数或生产 API 开关。

## 尚未满足的外部证据

当前正式运行仍应保持关闭，直至至少完成：独立历史成分复核、权威交易日历及发布
时间、双时态 `known_at`/修订版本、获许可 raw/hfq/公司行为/逐日可交易状态、PIT
基准版本，以及公司行为持仓现金状态机。完整公告下载或当前锚点都不能替代这些证据。
