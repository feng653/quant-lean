#!/bin/sh

set -eu

PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077

PROJECT_ROOT=/Users/xuhe/Developer/quant-platform
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
STATE_ROOT="/Users/xuhe/Library/Application Support/QuantPlatform"
BACKUP_DIR="$STATE_ROOT/backups"
PASSPHRASE_FILE="$STATE_ROOT/backup.key"
REPOSITORY_CONFIG="$STATE_ROOT/backup-repository.json"
REPOSITORY_SCRIPT=/usr/local/sbin/quant-platform-backup-repository.sh

if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    echo "backup must run as the unprivileged xuhe service account" >&2
    exit 77
fi
if [ "$(/usr/bin/id -un)" != xuhe ]; then
    echo "backup service account mismatch" >&2
    exit 77
fi
if [ ! -x "$PYTHON_BIN" ] || [ ! -d "$PROJECT_ROOT" ]; then
    echo "fixed production path is unavailable" >&2
    exit 66
fi

/bin/mkdir -p "$STATE_ROOT" "$BACKUP_DIR"
/bin/chmod 700 "$STATE_ROOT" "$BACKUP_DIR"

result_file=$(/usr/bin/mktemp "$STATE_ROOT/.backup-result.XXXXXX")
cleanup()
{
    /bin/rm -f -- "$result_file"
}
trap cleanup EXIT HUP INT TERM

if ! "$PYTHON_BIN" -m backend.ops.disaster_recovery backup \
    --source-root "$PROJECT_ROOT" \
    --destination "$BACKUP_DIR" \
    --passphrase-file "$PASSPHRASE_FILE" >"$result_file"
then
    /bin/cat "$result_file"
    exit 1
fi
/bin/cat "$result_file"

archive_name=$("$PYTHON_BIN" -c \
    'import json, pathlib, sys; print(pathlib.Path(json.loads(pathlib.Path(sys.argv[1]).read_text())["archive"]).name)' \
    "$result_file")

# Repository upload is opt-in.  The config is created only after an
# authenticated, read-only GitHub PRIVATE check; no token is stored in plist.
if [ -L "$REPOSITORY_CONFIG" ] \
    || { [ -e "$REPOSITORY_CONFIG" ] && [ ! -f "$REPOSITORY_CONFIG" ]; }
then
    echo "encrypted backup retained locally; repository config path is unsafe" >&2
    exit 77
elif [ -f "$REPOSITORY_CONFIG" ]; then
    if [ ! -x "$REPOSITORY_SCRIPT" ]; then
        echo "encrypted backup retained locally; repository helper is unavailable" >&2
        exit 69
    fi
    if ! "$REPOSITORY_SCRIPT" sync "$archive_name"; then
        echo "encrypted backup retained locally; private repository sync failed" >&2
        exit 1
    fi
else
    echo '{"repository_sync":"disabled","local_archive_retained":true}'
fi
