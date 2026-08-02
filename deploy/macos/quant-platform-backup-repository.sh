#!/bin/sh

set -eu

PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077

PROJECT_ROOT=/Users/xuhe/Developer/quant-platform
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
STATE_ROOT="/Users/xuhe/Library/Application Support/QuantPlatform"
BACKUP_DIR="$STATE_ROOT/backups"
CONFIG_FILE="$STATE_ROOT/backup-repository.json"
LOCK_FILE="$STATE_ROOT/backup-repository.lock"
AUDIT_FILE="$STATE_ROOT/backup-repository-audit.jsonl"

if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    echo "backup repository operations must not run as root" >&2
    exit 77
fi
if [ "$(/usr/bin/id -un)" != xuhe ]; then
    echo "backup repository service account mismatch" >&2
    exit 77
fi
if [ ! -x "$PYTHON_BIN" ] || [ ! -d "$PROJECT_ROOT" ]; then
    echo "fixed production path is unavailable" >&2
    exit 66
fi

/bin/mkdir -p "$STATE_ROOT" "$BACKUP_DIR"
/bin/chmod 700 "$STATE_ROOT" "$BACKUP_DIR"

command=${1:-}
case "$command" in
    configure)
        if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
            echo "usage: $0 configure OWNER REPOSITORY [RETENTION_COUNT]" >&2
            exit 64
        fi
        retention_count=${4:-30}
        exec "$PYTHON_BIN" -m backend.ops.backup_repository configure \
            --config "$CONFIG_FILE" \
            --owner "$2" \
            --repository "$3" \
            --retention-count "$retention_count"
        ;;
    check)
        if [ "$#" -ne 1 ]; then
            echo "usage: $0 check" >&2
            exit 64
        fi
        exec "$PYTHON_BIN" -m backend.ops.backup_repository check \
            --config "$CONFIG_FILE"
        ;;
    sync)
        if [ "$#" -ne 2 ]; then
            echo "usage: $0 sync ARCHIVE_BASENAME" >&2
            exit 64
        fi
        case "$2" in
            */* | .* | *[!A-Za-z0-9._-]*)
                echo "archive basename is invalid" >&2
                exit 64
                ;;
        esac
        exec "$PYTHON_BIN" -m backend.ops.backup_repository sync \
            --archive "$BACKUP_DIR/$2" \
            --config "$CONFIG_FILE" \
            --lock "$LOCK_FILE" \
            --audit "$AUDIT_FILE"
        ;;
    download-verify)
        if [ "$#" -ne 2 ]; then
            echo "usage: $0 download-verify ARCHIVE_BASENAME" >&2
            exit 64
        fi
        case "$2" in
            */* | .* | *[!A-Za-z0-9._-]*)
                echo "archive basename is invalid" >&2
                exit 64
                ;;
        esac
        exec "$PYTHON_BIN" -m backend.ops.backup_repository download-verify \
            --archive-name "$2" \
            --destination "$BACKUP_DIR" \
            --config "$CONFIG_FILE" \
            --lock "$LOCK_FILE" \
            --audit "$AUDIT_FILE"
        ;;
    *)
        echo "usage: $0 {configure OWNER REPOSITORY [RETENTION_COUNT]|check|sync ARCHIVE_BASENAME|download-verify ARCHIVE_BASENAME}" >&2
        exit 64
        ;;
esac
