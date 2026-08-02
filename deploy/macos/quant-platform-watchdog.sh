#!/bin/sh
#
# Health-aware companion for the launchd-managed Quant Platform services.
# This script is intentionally credential-free and is run periodically by
# com.quant-platform.watchdog.plist. launchd remains the process supervisor;
# this companion only turns persistent health failures into a supervised
# restart.

set -eu

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077

STATE_DIR=${QUANT_WATCHDOG_STATE_DIR:-/var/run/quant-platform-watchdog}
LOCK_DIR=${QUANT_WATCHDOG_LOCK_DIR:-/var/run/quant-platform-watchdog.lock}
FAILURE_THRESHOLD=${QUANT_WATCHDOG_FAILURE_THRESHOLD:-3}
BACKEND_UNRESPONSIVE_THRESHOLD=${QUANT_WATCHDOG_BACKEND_UNRESPONSIVE_THRESHOLD:-40}
RESTART_COOLDOWN_SECONDS=${QUANT_WATCHDOG_RESTART_COOLDOWN_SECONDS:-180}
BACKEND_URL=${QUANT_WATCHDOG_BACKEND_URL:-http://127.0.0.1:8000/api/health}
FRONTEND_URL=${QUANT_WATCHDOG_FRONTEND_URL:-http://127.0.0.1:5173/}
CADDY_URL=${QUANT_WATCHDOG_CADDY_URL:-}

case "$FAILURE_THRESHOLD:$BACKEND_UNRESPONSIVE_THRESHOLD:$RESTART_COOLDOWN_SECONDS" in
    *[!0-9:]* | :* | *:)
        echo "watchdog numeric configuration is invalid" >&2
        exit 64
        ;;
esac
if [ "$FAILURE_THRESHOLD" -lt 1 ] \
    || [ "$BACKEND_UNRESPONSIVE_THRESHOLD" -lt "$FAILURE_THRESHOLD" ] \
    || [ "$RESTART_COOLDOWN_SECONDS" -lt 30 ]
then
    echo "watchdog threshold must be >=1 and cooldown must be >=30 seconds" >&2
    exit 64
fi

log()
{
    message=$1
    /usr/bin/logger -t quant-platform-watchdog -- "$message"
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $message"
}

acquire_lock()
{
    if /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "$$" >"$LOCK_DIR/pid"
        return 0
    fi

    lock_pid=
    if [ -r "$LOCK_DIR/pid" ]; then
        lock_pid=$(/bin/cat "$LOCK_DIR/pid" 2>/dev/null || true)
    fi
    case "$lock_pid" in
        '' | *[!0-9]*) ;;
        *)
            if /bin/kill -0 "$lock_pid" 2>/dev/null; then
                return 1
            fi
            ;;
    esac

    # The recorded owner no longer exists. Remove only this exact lock path.
    /bin/rm -f "$LOCK_DIR/pid"
    /bin/rmdir "$LOCK_DIR" 2>/dev/null || return 1
    /bin/mkdir "$LOCK_DIR" 2>/dev/null || return 1
    echo "$$" >"$LOCK_DIR/pid"
}

if ! acquire_lock; then
    exit 0
fi
cleanup()
{
    /bin/rm -f "$LOCK_DIR/pid"
    /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

/bin/mkdir -p "$STATE_DIR"
/bin/chmod 700 "$STATE_DIR"

backend_healthy()
{
    body_path="$STATE_DIR/backend-health.json"
    curl_status=0
    http_status=$(/usr/bin/curl \
        --silent --show-error \
        --connect-timeout 2 --max-time 5 \
        --output "$body_path" \
        --write-out '%{http_code}' \
        "$BACKEND_URL") || curl_status=$?
    # CPU-heavy research currently executes in the API process. A curl timeout
    # can therefore mean legitimate computation rather than a dead process.
    # Return a distinct status so it receives a much longer threshold. Explicit
    # HTTP 503 still uses the normal fast recovery path.
    if [ "$curl_status" -eq 28 ]; then
        return 2
    fi
    if [ "$curl_status" -ne 0 ]; then
        return 1
    fi
    [ "$http_status" = 200 ] \
        && /usr/bin/grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' "$body_path"
}

frontend_healthy()
{
    /usr/bin/curl \
        --fail --silent --show-error \
        --connect-timeout 2 --max-time 5 \
        --output /dev/null \
        "$FRONTEND_URL"
}

caddy_healthy()
{
    [ -n "$CADDY_URL" ] || return 0
    /usr/bin/curl \
        --fail --silent --show-error \
        --connect-timeout 2 --max-time 5 \
        --output /dev/null \
        "$CADDY_URL"
}

read_number()
{
    path=$1
    fallback=$2
    value=
    if [ -r "$path" ]; then
        value=$(/bin/cat "$path" 2>/dev/null || true)
    fi
    case "$value" in
        '' | *[!0-9]*) echo "$fallback" ;;
        *) echo "$value" ;;
    esac
}

restart_service()
{
    service_name=$1
    label=$2
    health_function=$3

    log "$service_name remained unhealthy; requesting graceful restart"
    /bin/launchctl kill SIGTERM "system/$label" 2>/dev/null || true

    wait_count=0
    while [ "$wait_count" -lt 10 ]; do
        /bin/sleep 1
        if "$health_function"; then
            log "$service_name recovered after graceful restart"
            return 0
        fi
        wait_count=$((wait_count + 1))
    done

    log "$service_name did not recover gracefully; forcing launchd kickstart"
    /bin/launchctl kickstart -k "system/$label"
}

check_service()
{
    service_name=$1
    label=$2
    health_function=$3
    failure_path="$STATE_DIR/$service_name.failures"
    restart_path="$STATE_DIR/$service_name.last-restart"

    health_status=0
    "$health_function" || health_status=$?
    if [ "$health_status" -eq 0 ]; then
        echo 0 >"$failure_path"
        return 0
    fi

    threshold=$FAILURE_THRESHOLD
    if [ "$service_name" = backend ] && [ "$health_status" -eq 2 ]; then
        threshold=$BACKEND_UNRESPONSIVE_THRESHOLD
    fi
    failures=$(read_number "$failure_path" 0)
    failures=$((failures + 1))
    echo "$failures" >"$failure_path"
    log "$service_name health check failed ($failures/$threshold)"
    if [ "$failures" -lt "$threshold" ]; then
        return 0
    fi

    now=$(/bin/date +%s)
    last_restart=$(read_number "$restart_path" 0)
    since_restart=$((now - last_restart))
    if [ "$since_restart" -lt "$RESTART_COOLDOWN_SECONDS" ]; then
        log "$service_name restart suppressed by cooldown"
        return 0
    fi

    # Record the restart before acting so a failed launchctl call cannot create
    # a tight restart loop on the next watchdog interval.
    echo "$now" >"$restart_path"
    echo 0 >"$failure_path"
    if ! restart_service "$service_name" "$label" "$health_function"; then
        log "$service_name launchd restart failed"
        return 1
    fi
}

status=0
check_service \
    backend com.quant-platform.backend backend_healthy || status=1
check_service \
    frontend com.quant-platform.frontend frontend_healthy || status=1
if [ -n "$CADDY_URL" ]; then
    check_service \
        caddy com.quant-platform.caddy caddy_healthy || status=1
fi
exit "$status"
