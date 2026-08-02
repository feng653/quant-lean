#!/bin/sh

set -eu

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077

if [ "$#" -ne 1 ]; then
    echo "usage: quant-platform-restore-drill.sh <archive-basename.qpbak>" >&2
    exit 64
fi

case "$1" in
    quant-platform-[0-9]*T[0-9]*Z-[0-9a-f]*.qpbak) ;;
    *)
        echo "archive must be a generated .qpbak basename" >&2
        exit 64
        ;;
esac

PROJECT_ROOT=/Users/xuhe/Developer/quant-platform
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
STATE_ROOT="/Users/xuhe/Library/Application Support/QuantPlatform"
BACKUP_DIR="$STATE_ROOT/backups"
DRILL_ROOT="$STATE_ROOT/restore-drills"
PASSPHRASE_FILE="$STATE_ROOT/backup.key"
ARCHIVE_PATH="$BACKUP_DIR/$1"

if [ "$(/usr/bin/id -u)" -eq 0 ] || [ "$(/usr/bin/id -un)" != xuhe ]; then
    echo "restore drill must run as the unprivileged xuhe service account" >&2
    exit 77
fi
if [ ! -f "$ARCHIVE_PATH" ] || [ -L "$ARCHIVE_PATH" ]; then
    echo "archive is missing or unsafe" >&2
    exit 66
fi

/bin/mkdir -p "$STATE_ROOT" "$DRILL_ROOT"
/bin/chmod 700 "$STATE_ROOT" "$DRILL_ROOT"
DRILL_DIR=$(/usr/bin/mktemp -d "$DRILL_ROOT/drill.XXXXXXXX")
case "$DRILL_DIR" in
    "$DRILL_ROOT"/drill.*) ;;
    *)
        echo "mktemp returned an unsafe drill path" >&2
        exit 70
        ;;
esac
cleanup()
{
    /bin/rm -rf -- "$DRILL_DIR"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

"$PYTHON_BIN" -m backend.ops.disaster_recovery restore-drill \
    --archive "$ARCHIVE_PATH" \
    --destination "$DRILL_DIR/restored" \
    --passphrase-file "$PASSPHRASE_FILE"

REPORT_DIR="$DRILL_ROOT/reports"
/bin/mkdir -p "$REPORT_DIR"
/bin/chmod 700 "$REPORT_DIR"
REPORT_PATH="$REPORT_DIR/${1%.qpbak}.restore-audit.json"
/usr/bin/install -m 0600 \
    "$DRILL_DIR/restored/restore-audit.json" \
    "$REPORT_PATH"
echo "verified restore audit: $REPORT_PATH"
