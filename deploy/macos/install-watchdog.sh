#!/bin/sh

set -eu

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

if [ "$(/usr/bin/id -u)" -ne 0 ]; then
    echo "请使用 sudo 运行此安装器。" >&2
    exit 77
fi

SCRIPT_DIR=$(
    CDPATH= cd -- "$(dirname -- "$0")"
    pwd
)
SOURCE_SCRIPT="$SCRIPT_DIR/quant-platform-watchdog.sh"
SOURCE_PLIST="$SCRIPT_DIR/com.quant-platform.watchdog.plist"
TARGET_SCRIPT=/usr/local/sbin/quant-platform-watchdog.sh
TARGET_PLIST=/Library/LaunchDaemons/com.quant-platform.watchdog.plist
LABEL=com.quant-platform.watchdog

/usr/bin/plutil -lint "$SOURCE_PLIST" >/dev/null
/bin/mkdir -p /usr/local/sbin
/usr/bin/install -o root -g wheel -m 0755 "$SOURCE_SCRIPT" "$TARGET_SCRIPT"
/usr/bin/install -o root -g wheel -m 0644 "$SOURCE_PLIST" "$TARGET_PLIST"

# Replacing the exact watchdog job is intentional and idempotent. The guarded
# application services are not stopped by installation.
/bin/launchctl bootout "system/${LABEL}" 2>/dev/null || true
/bin/launchctl bootstrap system "$TARGET_PLIST"
/bin/launchctl enable "system/${LABEL}"
/bin/launchctl kickstart "system/${LABEL}"

printf '已安装 %s。查看状态：\n' "$LABEL"
printf '  sudo launchctl print system/%s\n' "$LABEL"
echo "日志：/var/log/quant-platform-watchdog.log"
