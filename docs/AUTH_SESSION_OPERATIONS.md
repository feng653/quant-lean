# 认证会话运维边界

## 已启用的最小闭环

- 每次交互式登录创建一个独立的设备会话；access JWT 带 `sid`，服务端每次访问
  检查该会话仍有效。
- refresh JWT 带 session/family/JTI，数据库只保存其 SHA-256 哈希。刷新只能使用一次；
  同一 refresh 重放、过期或状态不一致会撤销整个 family（本实现中为该设备会话），旧
  access token 随即失效。
- `POST /api/auth/logout` 默认注销当前设备，`{"all_sessions": true}` 注销全部设备；
  `GET /api/auth/sessions` 列出本人的无凭据元数据，`DELETE /api/auth/sessions/{id}`
  可撤销单一设备。
- 登录、刷新和会话管理是有界进程内限流。默认分别为 5 分钟 8 次、5 分钟 30 次、
  1 分钟 60 次；可用 `.env` 的 `AUTH_*` 参数调低。此限流在服务重启后清空，不能代替
  Caddy/防火墙的公网 DoS 防护，也不能替代多实例共享限流存储。

旧 access JWT 没有 `sid`，仅在其原有短寿命内兼容，以避免升级时强制踢出所有用户；
重新登录后获得可撤销会话。旧 token 不能生成新型 refresh family。

## 管理员操作

停用或撤销账号会同时撤销其所有新、旧会话。遇到疑似 refresh 泄露时，以管理员停用
账号或用户的 `all_sessions` 注销为优先处置，并立刻轮换该用户密码；不要把 JWT、refresh
token、密码或数据库查询结果写入工单、日志或截图。

## MFA 现状

MFA **尚未启用**。本项目尚未具备受保护的 TOTP/WebAuthn 凭据存储、恢复码、设备丢失
处理、管理员双人恢复和真实告警验收；在这些前提具备前，强行启用 MFA 可能造成管理员
永久锁定或绕过式恢复。管理员应使用独立长密码、受控网络入口和定期 `all_sessions` 注销。

## 部署前检查

1. `ENVIRONMENT=production` 且 `JWT_SECRET` 至少 32 字节，不能使用默认值。
2. `.env` 权限仅限服务账号；LaunchDaemon、日志和备份不含 token/password。
3. 运行 `pytest backend/tests/test_auth_sessions.py backend/tests/test_jwt_security.py -v`，
   并通过反向代理调用一次 login → refresh → logout → `/api/auth/me` 的 401 验证。
4. 若将服务扩展到多个进程或主机，先把限流放入经过认证的共享存储；不要宣称当前
   进程内实现具有分布式攻击防护能力。
