# 外部 SLO 告警投递（默认关闭）

本机的任务 SLO 已会产生 `breach` 与 `recovery` 审计事件。此功能把其中**允许
通知**的转换先写入 `experiment.db` 的 SQLite outbox，再以签名 HTTPS webhook
投递给任意兼容的个人通知端（例如自建 relay、企业微信/钉钉的受控中转、PagerDuty
或 ntfy 的自有 adapter）。它不是通用 HTTP 客户端，也不传输研究、订单或个人数据。

## 安全边界

- 默认 `ALERT_WEBHOOK_ENABLED=false`。未明确启用时绝不发起 DNS 或网络请求；已
  产生的 outbox 记录为 `disabled`，日后启用也不会回放旧事故。
- 仅接受无用户名/密码、无 fragment 的公开 `https://` 地址；拒绝 localhost、
  `.local` 与解析为私网/loopback 地址的 endpoint。URL、响应体和异常文本不会写入
  SQLite 或日志。
- webhook secret 是 `SecretStr`，仅在发送瞬间用于 HMAC-SHA256，绝不出现在
  payload、日志、可观测性接口或数据库中。secret 最少 16 字符，须用密码管理器生成。
- payload 是固定低基数 `slo-webhook-alert/v1`：`alert_id`、event kind、固定 SLO
  objective、breach/recovery、实际值/阈值/窗口与发生时间；不含用户、路径、token、
  job UUID、股票代码、诊断错误或策略/订单内容。
- outbox 对同一 SLO 审计事件只有一条 `transition`，由唯一键保证幂等。发送先 claim
  再投递；进程崩溃后的过期 lease 可重取，因此语义是**至少一次**，接收端必须按
  `X-Quant-Alert-Id` 去重。

## 配置

在私有 `.env`（不得提交）中显式填写。先在受控测试 endpoint 验证签名，再切到实际
通知 relay：

```env
ALERT_WEBHOOK_ENABLED=true
ALERT_WEBHOOK_URL=https://alerts.example.net/quant/slo
ALERT_WEBHOOK_SIGNING_SECRET=用密码管理器生成的至少16字符随机值
ALERT_WEBHOOK_TIMEOUT_SECONDS=5
ALERT_WEBHOOK_MAX_ATTEMPTS=5
ALERT_WEBHOOK_RETRY_BASE_SECONDS=60
ALERT_WEBHOOK_BATCH_SIZE=10
ALERT_WEBHOOK_ACK_ESCALATION_SECONDS=3600
```

不配置上述值时，服务与调度器保持现有行为。修改 `.env` 后需按安全服务安装文档重启
backend，才会加载新配置。不要用 HTTP、局域网地址、带 access token 的 URL query
或公共 webhook 收集器；若通知供应商只能提供这种 URL，应先部署一个受控 HTTPS
relay，将供应商凭据存于 relay 的 secret store。

## 接收与验签

每次 POST 包含 JSON、`X-Quant-Alert-Id`、`X-Quant-Alert-Timestamp` 与
`X-Quant-Alert-Signature`。签名材料是 UTF-8 字节：

```text
timestamp + "\n" + alert_id + "\n" + raw_request_body
```

签名为 `sha256=<hex(HMAC-SHA256(secret, material))>`。接收端应使用恒定时间比较、
拒绝过旧 timestamp，并按 `alert_id` 幂等处理。仅 HTTP 2xx 表示成功；408、429、5xx
与网络/超时按指数退避重试（最大 1 小时），其余 4xx 是终态失败。响应 body 一律被
丢弃。

## 确认、升级与日常验收

`GET /api/jobs/observability` 的
`slo.alerting.external_delivery` 只给安全汇总：启用状态、各 outbox 状态计数、未确认
breach 数和确认升级时限，不泄露 alert ID 或 endpoint。

每个成功投递的 breach 在确认期限内可由管理员确认：

```text
POST /api/jobs/observability/alerts/{alert_id}/acknowledge
Authorization: Bearer <admin JWT>
```

`alert_id` 仅出现在已签名、受控通知 payload 中。未确认 breach 到期后产生一条唯一的
`event_kind=escalation` 投递，接收端可按其提升优先级。确认只能确认已成功投递的
breach；它不等于修复，恢复仍需 SLO 连续观测后由原有逻辑产生。

上线前至少做以下无敏感数据演练并留存结果：

1. 在隔离数据库注入一个 `sqlite_contention_events` breach，确认 webhook 收到且验签、
   去重正确；确认数据库、应用日志与 relay 日志均无 secret/endpoint/业务数据。
2. 让 relay 返回 500、429、400 和超时，确认前两者进入 `retry_wait`，400 进入
   `delivery_failed`，次数不超过配置上限，服务 API/调度器未阻塞。
3. 对一个已投递 breach 调用确认接口，等待确认时限，确认没有 escalation；对另一个
   不确认，确认只有一次 escalation。
4. 真实运行至少一个完整观察窗后，审查无未处理 `delivery_failed`、未确认严重 breach
   与时钟偏差。该观察窗和真实通知端验收尚不能由单元测试替代。

本仓库的故障注入覆盖默认关闭、超时重试、签名、重复入队、确认阻止升级、未确认只升级
一次、私网 endpoint 拒绝，以及 broker SLO 到 disabled outbox 的事务链路：

```bash
python3 -m pytest backend/tests/test_alert_delivery.py -q --timeout=120
```
