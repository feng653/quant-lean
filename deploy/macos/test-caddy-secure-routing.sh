#!/bin/sh

# Non-privileged regression test for the public Caddy routing boundary.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG="$SCRIPT_DIR/Caddyfile.secure"
CADDY=${CADDY_BIN:-/usr/local/bin/caddy}

fail()
{
    echo "secure Caddy routing regression test failed: $1" >&2
    exit 1
}

[ -x "$CADDY" ] || fail "Caddy is not executable at $CADDY"
command -v python3 >/dev/null 2>&1 || fail "python3 is required to inspect adapted routing"

adapted=$(mktemp "${TMPDIR:-/tmp}/quant-caddy-adapted.XXXXXX")
trap 'rm -f "$adapted"' EXIT HUP INT TERM

"$CADDY" adapt --config "$CONFIG" --adapter caddyfile >"$adapted"
"$CADDY" validate --config "$CONFIG" --adapter caddyfile >/dev/null

python3 - "$adapted" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    config = json.load(stream)

def find_security_routes(value):
    if isinstance(value, dict):
        routes = value.get("routes")
        if isinstance(routes, list):
            for route in routes:
                matches = route.get("match", []) if isinstance(route, dict) else []
                if any("/openapi.json" in match.get("path", []) for match in matches):
                    return routes
        for nested in value.values():
            found = find_security_routes(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = find_security_routes(nested)
            if found is not None:
                return found
    return None

routes = find_security_routes(config)
assert routes is not None, "missing ordered security route group"

def handler_index(handler):
    for index, route in enumerate(routes):
        handlers = route.get("handle", [])
        if any(item.get("handler") == handler for item in handlers):
            return index
    raise AssertionError(f"missing handler: {handler}")

static_routes = []
for index, route in enumerate(routes):
    handlers = route.get("handle", [])
    if any(item.get("handler") == "static_response" for item in handlers):
        static_routes.append((index, route))

docs = None
for index, route in static_routes:
    paths = route.get("match", [{}])[0].get("path", [])
    if "/openapi.json" in paths:
        docs = (index, paths, route["handle"][0])
        break

assert docs is not None, "missing public API documentation deny route"
docs_index, docs_paths, docs_handler = docs
assert docs_handler.get("status_code") == 404
for path in ("/docs", "/docs/*", "/redoc*", "/openapi.json"):
    assert path in docs_paths, f"missing restricted path: {path}"

proxy_index = handler_index("subroute")
fallback_index = next(
    index
    for index, route in enumerate(routes)
    if any(
        item.get("handler") == "subroute"
        and any(
            nested.get("handler") == "vars"
            for nested_route in item.get("routes", [])
            for nested in nested_route.get("handle", [])
        )
        for item in route.get("handle", [])
    )
)
assert docs_index < proxy_index < fallback_index, (
    "restricted docs route must precede API proxy and SPA fallback"
)
PY

echo "secure Caddy routing regression checks passed"
