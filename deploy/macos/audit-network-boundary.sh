#!/bin/sh

set -eu

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

status=0
for port in 8000 5173; do
    listeners=$(/usr/sbin/lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [ -z "$listeners" ]; then
        echo "port $port has no listener" >&2
        status=1
        continue
    fi
    if echo "$listeners" | /usr/bin/grep -Eq "TCP (\\*|0\\.0\\.0\\.0|\\[::\\]):$port"; then
        echo "port $port is exposed beyond loopback" >&2
        status=1
    else
        echo "port $port is loopback-only"
    fi
done

for plist in \
    /Library/LaunchDaemons/com.quant-platform.backend.plist \
    /Library/LaunchDaemons/com.quant-platform.frontend.plist
do
    /usr/bin/plutil -lint "$plist" >/dev/null || status=1
done

backend_status=$(/usr/bin/curl \
    --silent --show-error --connect-timeout 2 --max-time 5 \
    --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:8000/api/health) || backend_status=000
frontend_status=$(/usr/bin/curl \
    --silent --show-error --connect-timeout 2 --max-time 5 \
    --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:5173/) || frontend_status=000
public_status=$(/usr/bin/curl \
    --silent --show-error --connect-timeout 3 --max-time 8 \
    --noproxy '*' \
    --resolve mac.feng37.top:443:127.0.0.1 \
    --output /dev/null --write-out '%{http_code}' \
    https://mac.feng37.top/) || public_status=000

if [ "$backend_status" != 200 ] \
    || [ "$frontend_status" != 200 ] \
    || [ "$public_status" != 200 ]
then
    echo "health boundary failed: backend=$backend_status frontend=$frontend_status public=$public_status" >&2
    status=1
else
    echo "health boundary passed: backend=200 frontend=200 public=200"
fi

for restricted_path in /docs /docs/ /docs/index.html /redoc /redoc/ /redoc-test /openapi.json; do
    restricted_status=$(/usr/bin/curl \
        --silent --show-error --connect-timeout 3 --max-time 8 \
        --noproxy '*' \
        --resolve mac.feng37.top:443:127.0.0.1 \
        --output /dev/null --write-out '%{http_code}' \
        "https://mac.feng37.top$restricted_path") || restricted_status=000
    if [ "$restricted_status" != 404 ]; then
        echo "restricted route exposed: $restricted_path returned $restricted_status, expected 404" >&2
        status=1
    else
        echo "restricted route passed: $restricted_path=404"
    fi
done

exit "$status"
