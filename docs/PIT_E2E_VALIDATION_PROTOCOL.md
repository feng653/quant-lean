# PIT 实验全链路只读验收协议

本协议验证“数据是否足以创建正式实验”，不采集、不导入、不激活数据，也不创建实验。
它适用于生产数据库和受控测试夹具，不能替代模拟部署或实盘门禁。

## 唯一判定接口

前端创建实验和参数扫描均先调用 `POST /api/data/experiment-readiness`。请求必须是
`cache_only`，并明确用途：

| 页面/操作 | `price_purpose` | 严格门禁 |
|---|---|---|
| 新建单次实验 | `return_research` | `ready_for_unbiased_return_research` |
| 参数扫描/锁定测试 | `real_tuning` | `ready_for_real_tuning` |
| 模拟执行预检 | `execution_simulation` | `ready_for_execution_simulation` |

响应契约为 `experiment-readiness/v2`，固定声明
`network_accessed=false`、`writes_performed=false` 和
`legacy_or_static_fallback_allowed=false`。验收必须读取 `checks` 和 `blockers`，不能仅凭
旧 Parquet 缓存存在或 `market_data.issues` 为空放行。

## 必须同时通过的证据

1. 行情窗口和基准缓存完整；
2. 已激活的 PIT 成分时间线覆盖请求窗口；
3. raw 成交价与研究调整价存在精确、不可变的双价格 runtime binding；
4. 权威交易日历和点时基准均已绑定；
5. 所选用途级门禁通过。

任一项失败时 `ready=false`，`blockers` 提供稳定机器码。浏览器应把这些机器码转换为
可读原因；即使旧缓存检查没有 issue，也不能显示空错误或继续提交。

## 隔离测试夹具

测试夹具只有在 `ENVIRONMENT=test`、所有可变路径均位于独立 fixture root、隔离标记与
hash-bound QA attestation 完整匹配时才可通过。通过后：

- `evidence.evidence_class=isolated_test_fixture`；
- `eligible_for_formal_experiment=true`，仅用于确定性自动测试；
- `eligible_for_live_trading=false`，且 attestation 在生产环境完全失效。

生产服务不得通过配置测试夹具绕过门禁。没有 QA attestation 的完整生产证据显示为
`governed_runtime`；证据不完整显示为 `incomplete`。

## 浏览器与服务验收顺序

1. 在浏览器登录后打开新建实验页，选择股票池和日期；浏览器请求必须先出现 readiness，
   且未就绪时不得随后出现创建实验请求。
2. 参数扫描页必须以 `real_tuning` 预检；研究级门禁通过而调优门禁未通过时仍应阻断。
3. 只读调用前后记录实验数、job 数、PIT active batch 数和 price binding 数；四项必须不变。
4. 验证阻断文案至少包含 PIT 时间线、双价格 binding、日历或基准中的实际缺口，不能是
   空字符串、`[object Object]` 或内部文件路径。
5. 使用隔离测试数据库执行三种非 ML 策略夹具；结果 manifest 必须包含同一 timeline hash、
   price binding id/digest、`network_accessed=false` 和禁用 legacy fallback。

API 登录凭据应通过浏览器会话或交互式安全输入获得，不写入脚本、命令参数、报告或版本库。
