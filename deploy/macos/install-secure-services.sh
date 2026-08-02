#!/bin/sh

set -eu

PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077

if [ "$(/usr/bin/id -u)" -ne 0 ]; then
    echo "请使用 sudo 运行安全服务安装器。" >&2
    exit 77
fi

SCRIPT_DIR=$(
    CDPATH= cd -- "$(dirname -- "$0")"
    pwd
)
PROJECT_ROOT=/Users/xuhe/Developer/quant-platform
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
ENV_FILE="$PROJECT_ROOT/.env"
DIST_DIR="$PROJECT_ROOT/frontend/dist"
STATE_ROOT="/Users/xuhe/Library/Application Support/QuantPlatform"
BACKUP_DIR="$STATE_ROOT/backups"
PASSPHRASE_FILE="$STATE_ROOT/backup.key"
LOG_DIR=/Users/xuhe/Library/Logs/quant-platform
ROLLBACK_ROOT=/var/backups/quant-platform-config
ROLLBACK_DIR="$ROLLBACK_ROOT/$(/bin/date -u '+%Y%m%dT%H%M%SZ')"

BACKEND_SOURCE="$SCRIPT_DIR/com.quant-platform.backend.plist"
FRONTEND_SOURCE="$SCRIPT_DIR/com.quant-platform.frontend.plist"
BACKUP_SOURCE="$SCRIPT_DIR/com.quant-platform.backup.plist"
CADDY_SOURCE="$SCRIPT_DIR/Caddyfile.secure"
BACKUP_SCRIPT_SOURCE="$SCRIPT_DIR/quant-platform-backup.sh"
BACKUP_REPOSITORY_SCRIPT_SOURCE="$SCRIPT_DIR/quant-platform-backup-repository.sh"
RESTORE_SCRIPT_SOURCE="$SCRIPT_DIR/quant-platform-restore-drill.sh"

BACKEND_TARGET=/Library/LaunchDaemons/com.quant-platform.backend.plist
FRONTEND_TARGET=/Library/LaunchDaemons/com.quant-platform.frontend.plist
BACKUP_TARGET=/Library/LaunchDaemons/com.quant-platform.backup.plist
CADDY_TARGET=/usr/local/etc/Caddyfile
BACKUP_SCRIPT_TARGET=/usr/local/sbin/quant-platform-backup.sh
BACKUP_REPOSITORY_SCRIPT_TARGET=/usr/local/sbin/quant-platform-backup-repository.sh
RESTORE_SCRIPT_TARGET=/usr/local/sbin/quant-platform-restore-drill.sh

for required in \
    "$BACKEND_SOURCE" "$FRONTEND_SOURCE" "$BACKUP_SOURCE" \
    "$CADDY_SOURCE" "$BACKUP_SCRIPT_SOURCE" \
    "$BACKUP_REPOSITORY_SCRIPT_SOURCE" "$RESTORE_SCRIPT_SOURCE"
do
    if [ ! -f "$required" ] || [ -L "$required" ]; then
        echo "安装源缺失或为符号链接：$required" >&2
        exit 66
    fi
done
if [ ! -x "$PYTHON_BIN" ] || [ ! -d "$DIST_DIR" ]; then
    echo "生产 Python 或前端 dist 不可用；拒绝替换服务。" >&2
    exit 66
fi
if [ ! -f "$ENV_FILE" ] || [ -L "$ENV_FILE" ]; then
    echo ".env 缺失或为符号链接；拒绝部署。" >&2
    exit 66
fi
env_mode=$(/usr/bin/stat -f '%Lp' "$ENV_FILE")
case "$env_mode" in
    600 | 400) ;;
    *)
        echo ".env 权限必须为 0600 或 0400。" >&2
        exit 77
        ;;
esac

for plist in "$BACKEND_SOURCE" "$FRONTEND_SOURCE" "$BACKUP_SOURCE"; do
    /usr/bin/plutil -lint "$plist" >/dev/null
done
/usr/local/bin/caddy validate \
    --config "$CADDY_SOURCE" \
    --adapter caddyfile >/dev/null

# Validate production-only startup secrets before touching a running service.
(
    cd "$PROJECT_ROOT"
    ENVIRONMENT=production "$PYTHON_BIN" -c \
        'from backend.config import settings; assert len(settings.JWT_SECRET.encode("utf-8")) >= 32'
)

/bin/mkdir -p "$ROLLBACK_DIR"
/bin/chmod 700 "$ROLLBACK_ROOT" "$ROLLBACK_DIR"
for target in \
    "$BACKEND_TARGET" "$FRONTEND_TARGET" "$BACKUP_TARGET" \
    "$CADDY_TARGET" "$BACKUP_SCRIPT_TARGET" \
    "$BACKUP_REPOSITORY_SCRIPT_TARGET" "$RESTORE_SCRIPT_TARGET"
do
    if [ -f "$target" ] && [ ! -L "$target" ]; then
        /bin/cp -p "$target" "$ROLLBACK_DIR/$(/usr/bin/basename "$target")"
    else
        /usr/bin/touch "$ROLLBACK_DIR/$(/usr/bin/basename "$target").absent"
    fi
done

rollback_needed=1
backend_touched=0
frontend_touched=0
backup_touched=0

launchd_job_loaded()
{
    /bin/launchctl print "system/$1" >/dev/null 2>&1
}

wait_launchd_job_absent()
(
    wait_label=$1
    wait_attempt=0
    while [ "$wait_attempt" -lt 20 ]; do
        if ! launchd_job_loaded "$wait_label"; then
            return 0
        fi
        /bin/sleep 1
        wait_attempt=$((wait_attempt + 1))
    done
    echo "LaunchDaemon ${wait_label} 在 20 秒内未完成注销。" >&2
    return 1
)

bootstrap_launchd_service()
(
    bootstrap_label=$1
    bootstrap_target=$2
    bootstrap_attempt=0
    bootstrap_error=
    while [ "$bootstrap_attempt" -lt 10 ]; do
        if bootstrap_error=$(
            /bin/launchctl bootstrap system "$bootstrap_target" 2>&1
        ); then
            return 0
        fi
        # launchctl can report an error after launchd accepted the job.
        if launchd_job_loaded "$bootstrap_label"; then
            return 0
        fi
        bootstrap_attempt=$((bootstrap_attempt + 1))
        [ "$bootstrap_attempt" -lt 10 ] || break
        /bin/sleep 1
    done
    echo "无法注册 LaunchDaemon ${bootstrap_label}（已重试 10 次）：${bootstrap_error}" >&2
    /bin/launchctl print-disabled system >&2 || true
    return 1
)

activate_launchd_service()
{
    activate_label=$1
    activate_target=$2
    activate_changed=$3

    if launchd_job_loaded "$activate_label"; then
        if [ "$activate_changed" -eq 0 ]; then
            echo "LaunchDaemon ${activate_label} 配置未变化；保留现有进程。"
            return 0
        fi
        /bin/launchctl bootout "system/${activate_label}"
        wait_launchd_job_absent "$activate_label"
    fi
    /bin/launchctl enable "system/${activate_label}"
    bootstrap_launchd_service "$activate_label" "$activate_target"
    /bin/launchctl kickstart "system/${activate_label}"
}

wait_http_healthy()
(
    health_name=$1
    health_url=$2
    health_attempt=0
    health_status=000
    while [ "$health_attempt" -lt 30 ]; do
        health_status=$(/usr/bin/curl \
            --silent --connect-timeout 2 --max-time 5 \
            --output /dev/null --write-out '%{http_code}' \
            "$health_url") || health_status=000
        if [ "$health_status" = 200 ]; then
            return 0
        fi
        /bin/sleep 1
        health_attempt=$((health_attempt + 1))
    done
    echo "${health_name} 启动后健康检查失败：http=${health_status}" >&2
    return 1
)

restore_launchd_service()
{
    restore_label=$1
    restore_target=$2
    if launchd_job_loaded "$restore_label"; then
        /bin/launchctl bootout "system/${restore_label}" 2>/dev/null || true
        wait_launchd_job_absent "$restore_label" || true
    fi
    if [ ! -f "$restore_target" ]; then
        return 0
    fi
    /bin/launchctl enable "system/${restore_label}" 2>/dev/null || true
    if ! bootstrap_launchd_service "$restore_label" "$restore_target"; then
        echo "回滚警告：无法恢复 ${restore_label}（配置已恢复至 ${restore_target}）。" >&2
        return 1
    fi
    /bin/launchctl kickstart "system/${restore_label}" 2>/dev/null || true
}

rollback()
{
    exit_status=$?
    trap - EXIT HUP INT TERM
    if [ "$rollback_needed" -eq 0 ]; then
        exit "$exit_status"
    fi
    set +e
    echo "部署未完成；正在恢复安装前的服务配置。" >&2
    for target in \
        "$BACKEND_TARGET" "$FRONTEND_TARGET" "$BACKUP_TARGET" \
        "$CADDY_TARGET" "$BACKUP_SCRIPT_TARGET" \
        "$BACKUP_REPOSITORY_SCRIPT_TARGET" "$RESTORE_SCRIPT_TARGET"
    do
        basename=$(/usr/bin/basename "$target")
        if [ -f "$ROLLBACK_DIR/$basename" ]; then
            /bin/cp -p "$ROLLBACK_DIR/$basename" "$target"
        elif [ -f "$ROLLBACK_DIR/$basename.absent" ]; then
            /bin/rm -f -- "$target"
        fi
    done
    if [ -f "$CADDY_TARGET" ]; then
        /usr/local/bin/caddy validate \
            --config "$CADDY_TARGET" --adapter caddyfile >/dev/null 2>&1 \
            && /usr/local/bin/caddy reload \
                --config "$CADDY_TARGET" --adapter caddyfile >/dev/null 2>&1
    fi
    # Restore only jobs whose launchd state this run actually touched. This
    # prevents a later, unrelated failure from restarting healthy daemons.
    if [ "$backend_touched" -eq 1 ]; then
        restore_launchd_service com.quant-platform.backend "$BACKEND_TARGET" || true
    fi
    if [ "$frontend_touched" -eq 1 ]; then
        restore_launchd_service com.quant-platform.frontend "$FRONTEND_TARGET" || true
    fi
    if [ "$backup_touched" -eq 1 ]; then
        restore_launchd_service com.quant-platform.backup "$BACKUP_TARGET" || true
    fi
    if [ "$backend_touched" -eq 0 ] \
        && [ "$frontend_touched" -eq 0 ] \
        && [ "$backup_touched" -eq 0 ]; then
        echo "服务尚未替换；回滚不会重启现有 LaunchDaemon。" >&2
    fi
    echo "已恢复安装前配置；回滚证据目录：$ROLLBACK_DIR" >&2
    exit "$exit_status"
}
trap rollback EXIT
trap 'exit 130' HUP INT TERM

/bin/mkdir -p "$STATE_ROOT" "$BACKUP_DIR" "$LOG_DIR" /usr/local/sbin
/usr/sbin/chown xuhe:staff "$STATE_ROOT" "$BACKUP_DIR" "$LOG_DIR"
/bin/chmod 700 "$STATE_ROOT" "$BACKUP_DIR" "$LOG_DIR"
if [ ! -e "$PASSPHRASE_FILE" ]; then
    /usr/bin/openssl rand -base64 48 >"$PASSPHRASE_FILE"
fi
if [ -L "$PASSPHRASE_FILE" ] || [ ! -f "$PASSPHRASE_FILE" ]; then
    echo "备份密钥路径不安全；拒绝部署。" >&2
    exit 77
fi
/usr/sbin/chown xuhe:staff "$PASSPHRASE_FILE"
/bin/chmod 600 "$PASSPHRASE_FILE"

# Runtime data is private even though backup archives are encrypted.
/bin/chmod 700 "$PROJECT_ROOT/data"
for database in \
    "$PROJECT_ROOT/data/users.db" \
    "$PROJECT_ROOT/data/experiment.db" \
    "$PROJECT_ROOT/data/trading_sim.db" \
    "$PROJECT_ROOT/data/jobs.db" \
    "$PROJECT_ROOT/data/trading_live.db"
do
    if [ -f "$database" ] && [ ! -L "$database" ]; then
        /bin/chmod 600 "$database"
    fi
done

# Capture service-definition changes before replacing the target bytes. This
# lets an idempotent install keep an already healthy process running.
backend_changed=1
frontend_changed=1
backup_changed=1
/usr/bin/cmp -s "$BACKEND_SOURCE" "$BACKEND_TARGET" && backend_changed=0
/usr/bin/cmp -s "$FRONTEND_SOURCE" "$FRONTEND_TARGET" && frontend_changed=0
/usr/bin/cmp -s "$BACKUP_SOURCE" "$BACKUP_TARGET" && backup_changed=0

/usr/bin/install -o root -g wheel -m 0644 "$BACKEND_SOURCE" "$BACKEND_TARGET"
/usr/bin/install -o root -g wheel -m 0644 "$FRONTEND_SOURCE" "$FRONTEND_TARGET"
/usr/bin/install -o root -g wheel -m 0644 "$BACKUP_SOURCE" "$BACKUP_TARGET"
/usr/bin/install -o root -g wheel -m 0644 "$CADDY_SOURCE" "$CADDY_TARGET"
/usr/bin/install -o root -g wheel -m 0755 \
    "$BACKUP_SCRIPT_SOURCE" "$BACKUP_SCRIPT_TARGET"
/usr/bin/install -o root -g wheel -m 0755 \
    "$BACKUP_REPOSITORY_SCRIPT_SOURCE" "$BACKUP_REPOSITORY_SCRIPT_TARGET"
/usr/bin/install -o root -g wheel -m 0755 \
    "$RESTORE_SCRIPT_SOURCE" "$RESTORE_SCRIPT_TARGET"

# Caddy validates the installed bytes before reload.  Existing DNS credentials
# stay in the root-owned launcher and are neither read nor copied here.
/usr/local/bin/caddy validate \
    --config "$CADDY_TARGET" \
    --adapter caddyfile >/dev/null
/usr/local/bin/caddy reload \
    --config "$CADDY_TARGET" \
    --adapter caddyfile

# Re-register one service at a time. Unchanged loaded jobs are never stopped;
# a missing job is recovered without a preceding bootout. launchd reports
# POSIX error 37 as launchctl's generic EIO while a previous bootout is still
# completing, so each changed job waits for absence and bootstrap is boundedly
# retried before rollback.
if [ "$backend_changed" -eq 1 ] \
    || ! launchd_job_loaded com.quant-platform.backend; then
    backend_touched=1
fi
activate_launchd_service \
    com.quant-platform.backend "$BACKEND_TARGET" "$backend_changed"
wait_http_healthy backend http://127.0.0.1:8000/api/health

if [ "$frontend_changed" -eq 1 ] \
    || ! launchd_job_loaded com.quant-platform.frontend; then
    frontend_touched=1
fi
activate_launchd_service \
    com.quant-platform.frontend "$FRONTEND_TARGET" "$frontend_changed"
wait_http_healthy frontend http://127.0.0.1:5173/

if [ "$backup_changed" -eq 1 ] \
    || ! launchd_job_loaded com.quant-platform.backup; then
    backup_touched=1
fi
activate_launchd_service \
    com.quant-platform.backup "$BACKUP_TARGET" "$backup_changed"

attempt=0
while [ "$attempt" -lt 20 ]; do
    backend_status=$(/usr/bin/curl \
        --silent --connect-timeout 2 --max-time 5 \
        --output /dev/null --write-out '%{http_code}' \
        http://127.0.0.1:8000/api/health) || backend_status=000
    frontend_status=$(/usr/bin/curl \
        --silent --connect-timeout 2 --max-time 5 \
        --output /dev/null --write-out '%{http_code}' \
        http://127.0.0.1:5173/) || frontend_status=000
    public_status=$(/usr/bin/curl \
        --silent --connect-timeout 3 --max-time 8 \
        --output /dev/null --write-out '%{http_code}' \
        https://mac.feng37.top/) || public_status=000
    if [ "$backend_status" = 200 ] \
        && [ "$frontend_status" = 200 ] \
        && [ "$public_status" = 200 ]
    then
        rollback_needed=0
        echo "安全服务边界部署完成；原配置保存在 $ROLLBACK_DIR"
        echo "backend=200 frontend=200 public=200；VNC 与路由器配置未变更。"
        exit 0
    fi
    /bin/sleep 1
    attempt=$((attempt + 1))
done

echo "服务健康预检失败；将执行自动回滚。" >&2
echo "backend=$backend_status frontend=$frontend_status public=$public_status" >&2
exit 1
