# 本机网络边界、加密备份与恢复演练

> 适用范围：macOS 单机研究与模拟交易服务。
> 本控制不构成实盘认证，也不替代异地备份、PostgreSQL、券商对账或双机容灾。

## 1. 已实现的边界

```text
Internet / LAN browser
        |
        | HTTPS 443
        v
Caddy (唯一应用入口)
  ├─ 静态 frontend/dist
  ├─ /api/*、/ws/* → 127.0.0.1:8000
  ├─ /api/admin/* → 仅 private_ranges，再进入 JWT + admin:users
  └─ /docs、/redoc、/openapi.json → 公网 404

127.0.0.1:5173  frontend preview（仅本机健康检查）
127.0.0.1:8000  FastAPI（仅 Caddy、本机运维）
```

- 后端只接受来自 `127.0.0.1,::1` 的转发头；互联网客户端伪造
  `X-Forwarded-For` 不会成为受信代理。
- Caddy 在转发前按实际远端地址限制管理面。网络限制不能替代后端 JWT、活跃用户
  与 RBAC 检查，两层必须同时通过。
- API 文档只在本机 `http://127.0.0.1:8000/docs` 可用。
- `mac.feng37.top`、Caddy DNS-01 TLS 和 VNC 服务保持原边界；安装器不读取、不修改
  路由器、外部 DNS、macOS 屏幕共享或 5900 端口。
- 后端、前端、数据库和备份进程均以 `xuhe` 非 root 账号运行；root 只负责
  LaunchDaemon/Caddy 配置安装。

## 2. 部署与回滚

部署前必须已经完成前端生产构建，并保证 `.env` 为普通文件、权限 `0600/0400`、
生产 JWT 密钥至少 32 字节。安装器会先校验全部 plist、Caddy 配置和生产配置，再
触碰运行服务：

```sh
sudo /bin/sh deploy/macos/install-secure-services.sh
```

它将：

1. 把安装前的精确配置保存到权限 `0700` 的
   `/var/backups/quant-platform-config/<UTC时间>/`；
2. 把 `8000/5173` 改为回环监听，并让 Uvicorn 只信任回环代理；
3. 原子安装并 reload Caddy 安全配置；
4. 把 `data/` 收紧为 `0700`、SQLite 收紧为 `0600`；
5. 安装每日 03:15 的非 root 加密备份任务；
6. 检查本机后端、前端和 `mac.feng37.top` 均为 HTTP 200。

任何校验、reload、launchctl 或最终健康检查失败都会恢复安装前的精确配置并重新
bootstrap 原有服务。安装器只操作列出的 Quant Platform 文件和 label。

部署后执行只读验收：

```sh
/bin/sh deploy/macos/audit-network-boundary.sh
sudo launchctl print system/com.quant-platform.backend
sudo launchctl print system/com.quant-platform.frontend
sudo launchctl print system/com.quant-platform.backup
```

`lsof` 中 `8000/5173` 必须显示 `127.0.0.1`，不能显示 `*`、`0.0.0.0` 或
`[::]`。

## 3. 加密备份协议

备份由 `backend.ops.disaster_recovery` 实现，HTTP API 不提供备份、恢复或密钥
入口。固定路径 wrapper 为 `/usr/local/sbin/quant-platform-backup.sh`：

- 使用 SQLite online backup API，而不是直接复制活动的 `.db/.wal/.shm`；
- 源库与副本都执行 `PRAGMA integrity_check`；
- 必备库为 `users.db`、`experiment.db`、`trading_sim.db`、`jobs.db`；
  `trading_live.db` 只在存在时保存，不会因此解除实盘锁定；
- 保存 `.env`、无密钥部署模板、`data/research_snapshots` 和 `data/models`；
- 排除可重新生成且体积大的行情缓存和前端构建目录；
- 每个文件绑定大小与 SHA-256，整体 manifest 绑定 backup ID、UTC 时间和 Git
  commit；
- 归档采用 scrypt（`N=32768,r=8,p=1`）派生密钥与 AES-256-GCM 认证加密；
- 临时目录 `0700`，密钥、归档、audit 和恢复文件均为 `0600`，归档通过同文件系统
  原子替换发布。

密钥固定在
`/Users/xuhe/Library/Application Support/QuantPlatform/backup.key`。安装器仅在
文件不存在时用系统 CSPRNG 生成，不覆盖现有密钥，也不会把密钥放进仓库、plist、
命令参数、环境变量、日志或备份本身。丢失该密钥即无法恢复；真正的灾难恢复仍需
把密钥和至少一份 `.qpbak` 通过受控方式保存到另一故障域。

目标目录为：

```text
/Users/xuhe/Library/Application Support/QuantPlatform/backups/
  quant-platform-<UTC>-<id>.qpbak
  quant-platform-<UTC>-<id>.qpbak.audit.json
```

audit 只包含哈希、文件数量、数据库完整性、commit 和时间，不包含密码、JWT、
API key、文件内容或用户名列表。

### 3.1 GitHub private 异地副本（可选）

安装器同时安装 `/usr/local/sbin/quant-platform-backup-repository.sh`，但不会创建
GitHub 仓库、写入外部账号或自动启用上传。当前约定目标示例为已单独创建并核验的
private 仓库 `feng653/quant-platform-backups`；目标仍由配置决定，脚本不会接受
任意 host、带凭据 URL 或 owner/repository 不一致的 origin。

启用前要求：

- 目标必须是 GitHub private 仓库，并已有一个不含文件的 empty anchor commit/default
  branch（Release tag 需要 commitish；不得用 README、license、日志或配置初始化）；
  归档使用 Releases asset，不进入 Git object；
- `xuhe` 用户的 `gh` 已登录，且对目标有 metadata、Release 上传/删除权限；
- 加密密钥仍以独立受控方式保存，绝不能放进该仓库。远端密文不包含恢复密钥。

安装完成后，以 `xuhe` 用户执行一次配置。该动作仅用 `gh repo view` 只读确认目标
的 `nameWithOwner` 与 `visibility=PRIVATE`，然后把不含 token 的 `0600` 配置写到
Application Support；它不会 push：

```sh
/usr/local/sbin/quant-platform-backup-repository.sh \
  configure feng653 quant-platform-backups 30
/usr/local/sbin/quant-platform-backup-repository.sh check
```

之后每日任务先生成本地 `.qpbak`，再复核 PRIVATE 状态并上传到固定 tag
`quant-platform-encrypted-backups-v1` 的 Release。GitHub Git 单对象上限为 100 MiB，
生产归档可能超过该值，因此工具明确不执行 `git add/commit/push`。上传 allowlist
只接受 `quant-platform-<UTC>-<id>.qpbak`；`.audit.json`、`.env`、密钥、日志、数据库
和任何其他明文都不会作为 asset 上传。每次上传后必须从 GitHub API 读回 asset
size 和服务端 `sha256` digest，与本地密文逐字节绑定；digest 缺失也按失败处理。
配置中的 URL 必须精确等于 owner/repository 对应的 `https://github.com/...`，不能带
凭据或指向其他 host。

同步使用非阻塞 OS 文件锁。同名 asset 已存在时必须先通过 size+digest 才视为幂等
成功，绝不使用 clobber。配置末尾数字为远端密文保留数量（1–1000，默认 30）；
只有当前上传已验证后，才按 Release `created_at` 删除超额且文件名命中密文
allowlist 的旧 asset，本地归档不随远端保留策略删除。远端校验、认证、网络、上传
或清理失败时任务返回失败，本地 `backups/` 原始密文不删除。脱敏结果追加到本机权限 `0600` 的
`backup-repository-audit.jsonl`，该审计文件不进入备份仓库。没有配置文件时仍只做
本地备份并明确报告 upload disabled；发现配置路径为 symlink 或非普通文件则失败
关闭。

可从 private Release 下载指定密文。工具先校验远端 PRIVATE、asset 名称、size 与
服务端 digest，**每次都重新从远端下载**到 `0700` 临时目录，重新计算 SHA-256 并检查
`.qpbak` magic，全部通过后才原子发布到本地 `backups/`；即使 `backups/` 已存在同名
文件也不能跳过网络读取，避免把本地检查误报为远端恢复。下载中断不会破坏原有已验证
副本。随后复用既有 restore drill：

```sh
/usr/local/sbin/quant-platform-backup-repository.sh download-verify \
  quant-platform-20260801T031500Z-0123456789ab.qpbak
/usr/local/sbin/quant-platform-restore-drill.sh \
  quant-platform-20260801T031500Z-0123456789ab.qpbak
```

## 4. 恢复演练

恢复工具拒绝生产目录、非空目录、符号链接、绝对/穿越 tar 成员、硬链接、设备文件、
未登记文件、错误大小、错误 SHA、错误密钥和被篡改密文。GCM、manifest 或任一
SQLite 检查失败时，目标目录整体删除，不留下部分恢复状态。

固定 wrapper 只接受备份目录内由平台生成的 archive basename，并恢复到
`restore-drills` 下的随机临时目录；验证成功后仅保留 `0600` audit，自动删除含
敏感数据的临时恢复副本：

```sh
/usr/local/sbin/quant-platform-restore-drill.sh \
  quant-platform-20260731T031500Z-0123456789ab.qpbak
```

恢复生产服务不是自动操作。实际事故中应先在隔离目录完成上述验证，停止写入者，
核对 commit 与迁移版本，再由变更负责人制定逐库切换和回滚方案；工具不会覆盖
生产数据库。

## 5. RPO、RTO 与剩余风险

- 本机计划任务给出目标 `RPO <= 24h`；launchd/磁盘失败可能使其不成立，必须监控
  最新 audit 时间。
- 约 200 MB 的当前数据集在本机演练中备份和恢复均小于一分钟，但正式 `RTO`
  仍取决于异地介质、代码/依赖重建、迁移和人工验收，不能只用解密时间宣称。
- 本地同盘备份不能抵御整机丢失、磁盘损坏、勒索软件或同账号入侵。
- GitHub private 密文副本可覆盖整机/磁盘故障，但不能覆盖 GitHub 账号接管、仓库
  被删除或本机与远端凭据同时失陷；应监控 upload audit，并另存恢复密钥。
- SQLite 在线备份解决一致性快照，不解决多机高可用和跨数据库原子事务。
- Caddy 私网限制保护 `/api/admin/*`；其他高权限业务接口继续依靠 JWT/RBAC 和
  各自的管理员 attestation。未来若扩展新的管理路由，必须同步加入代理管理面
  matcher 或迁移到独立管理域。
