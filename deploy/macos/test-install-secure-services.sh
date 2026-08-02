#!/bin/sh

# Static regression test for the privileged installer.  It deliberately does
# not invoke sudo, launchctl, Caddy, or write under /Library.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALLER="$SCRIPT_DIR/install-secure-services.sh"

fail()
{
    echo "secure-services installer regression test failed: $1" >&2
    exit 1
}

[ -f "$INSTALLER" ] || fail "installer is missing"
/bin/sh -n "$INSTALLER" || fail "installer has invalid POSIX shell syntax"

if /usr/bin/grep -F -q '/bin/chown' "$INSTALLER"; then
    fail "macOS does not provide /bin/chown; use /usr/sbin/chown"
fi

chown_count=$(/usr/bin/grep -F -c '/usr/sbin/chown xuhe:staff' "$INSTALLER" || true)
[ "$chown_count" -eq 2 ] || fail "state and passphrase ownership must use /usr/sbin/chown"

/usr/bin/grep -F -q 'backend_touched=0' "$INSTALLER" \
    || fail "per-service rollback lifecycle flags are missing"
/usr/bin/grep -F -q 'if [ "$backend_touched" -eq 1 ]; then' "$INSTALLER" \
    || fail "rollback must restore only launchd jobs touched by this run"
/usr/bin/grep -F -q '服务尚未替换；回滚不会重启现有 LaunchDaemon。' "$INSTALLER" \
    || fail "early rollback must report that it leaves services untouched"

/usr/bin/grep -F -q 'wait_launchd_job_absent "$activate_label"' "$INSTALLER" \
    || fail "changed jobs must wait for launchd removal before bootstrap"
/usr/bin/grep -F -q '[ "$bootstrap_attempt" -lt 10 ]' "$INSTALLER" \
    || fail "launchd bootstrap must use a bounded retry"
/usr/bin/grep -F -q '配置未变化；保留现有进程。' "$INSTALLER" \
    || fail "unchanged loaded jobs must not be restarted"

if /usr/bin/grep -Eq '\$[A-Za-z_][A-Za-z0-9_]*（' "$INSTALLER"; then
    fail "variables next to non-ASCII punctuation must use braces"
fi

first_backend=$(/usr/bin/grep -n 'activate_launchd_service \\' "$INSTALLER" \
    | /usr/bin/sed -n '1s/:.*//p')
first_frontend=$(/usr/bin/grep -n 'activate_launchd_service \\' "$INSTALLER" \
    | /usr/bin/sed -n '2s/:.*//p')
[ -n "$first_backend" ] && [ -n "$first_frontend" ] \
    && [ "$first_backend" -lt "$first_frontend" ] \
    || fail "services must be activated sequentially"

echo "secure-services installer static regression checks passed"
