# 本机加密备份与隔离恢复演练记录

> 演练时间：2026-07-31（Asia/Shanghai）
> 生产源访问：只读 SQLite online backup
> 恢复目标：`/private/tmp` 下权限 `0700` 的随机隔离目录

## 结果

演练成功完成“活动库一致性快照 → 认证加密 → 解密 → 安全解包 → manifest 验证 →
四库完整性复核”。演练未停止服务、未写生产数据库、未修改 Caddy/VNC/DNS。

| 证据 | 结果 |
|---|---|
| 加密 | AES-256-GCM；scrypt N=32768/r=8/p=1 |
| 归档大小 | 211,671,428 bytes |
| 归档 SHA-256 | `d8dc54d82b10a92d82c2bf45cf06158f4c3f5aa16559950c1b18f38b4e177486` |
| Backup ID | `e693687195954c07b5875b4df4e19462` |
| Manifest SHA-256 | `8d232ef4f1fa9263966f2b78d3abadf0a73213fbbb8567669103b5263eb11ed8` |
| 文件数 | 39 |
| `users.db` | `PRAGMA integrity_check = ok` |
| `experiment.db` | `PRAGMA integrity_check = ok`（恢复后再以只读 sqlite3 复核） |
| `trading_sim.db` | `PRAGMA integrity_check = ok` |
| `jobs.db` | `PRAGMA integrity_check = ok` |
| 归档权限 | `0600` |
| 恢复 audit 权限 | `0600` |
| 隔离标志 | `destination_is_isolated=true` |

测试同时覆盖错误密钥、最后认证 tag 篡改、证据目录符号链接、源目录内备份目标、
宽松密钥权限和进程 umask 为 `000` 的负向用例，均按 fail-closed 预期处理。

演练结束后，包含 `.env`、数据库和研究证据的临时归档、临时密钥与恢复副本已经从
精确的随机演练目录删除，不能恢复；仓库只保留本记录中的非敏感审计摘要。部署后的
日常归档由固定的最小权限目录保存。

## 结论与限制

本轮关闭的是“本机研究/模拟数据能否生成一致、加密、可验证副本，并在隔离目录
恢复”的控制缺口。它不关闭异地故障域、自动保留策略、PostgreSQL、高可用、券商
日终对账或实盘恢复认证，因此平台继续保持 `not_certified`。
