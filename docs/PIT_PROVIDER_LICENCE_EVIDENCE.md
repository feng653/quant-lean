# PIT 供应商许可/留存证据登记

该登记表用于记录已在平台外取得的供应商许可、自动抓取或历史留存确认。它只保存
文件 SHA-256、大小、供应商/数据范围、声称的 effective/available 期间、取得时间和
不可逆的引用指纹。不保存文档内容、token、URL 用户信息/查询参数或本地路径。

所有端点都需要 `admin:users` 权限：

- `GET /api/data/provider-licence-evidence/contract`：查看安全边界。
- `POST /api/data/provider-licence-evidence/records`：追加一条 `unverified` 记录。
- `POST /api/data/provider-licence-evidence/records/{record_sha256}/reviews`：由与登记人不同的管理员追加一次 `approved` 或 `rejected` 复核。
- `GET /api/data/provider-licence-evidence/records`：读取并逐条复算摘要。

数据库表和复核表均有禁止 UPDATE/DELETE 的触发器，文件权限限制为当前账户读写。如果记录
摘要与内容不一致，读取失败关闭。要更正错误记录，必须追加新记录，不能覆盖旧记录。

## 与生产发布的隔离

`approved` 只表示独立复核人确认“该摘要所指的外部文档存在，且登记的声称与复核结论
一致”。它不会复制文档到生产 artifact store，不会生成签名，不会写入 production release
registry，也不会导入/激活 PIT 数据。

生产发布仍必须同时通过：实际许可文件的内容寻址留存与 digest 重算、受信 Ed25519
密钥签名的 approved provider artifact、20 个官方事件/逐 session 独立对账，以及零 blocker
的 production release dry-run。因此此功能不改变 ROADMAP Q-01 的阻塞状态。
