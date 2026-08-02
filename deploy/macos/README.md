# Quant Platform macOS health watchdog

The existing backend and frontend LaunchDaemons use `KeepAlive`, which restarts
a process only after it exits. A process can still listen on its port while an
internal worker has stopped. This companion checks service health every 30
seconds and converts three consecutive failures into a launchd restart.

Install from the repository root:

```sh
sudo /bin/sh deploy/macos/install-watchdog.sh
```

The watchdog:

- checks `http://127.0.0.1:8000/api/health`, including the backend critical-task
  state;
- checks `http://127.0.0.1:5173/`;
- first requests `SIGTERM`, waits up to 10 seconds, and uses
  `launchctl kickstart -k` only if graceful recovery fails;
- uses an execution lock, three-failure threshold, and 180-second per-service
  cooldown to avoid restart storms;
- distinguishes an explicit backend `503` from an HTTP timeout. A `503`
  restarts after three checks, while a timeout needs 40 checks (20 minutes) so
  legitimate CPU-heavy in-process research is not killed after 90 seconds;
- stores only numeric counters under `/var/run`; it stores no user credentials,
  API keys, cookies, or authorization headers.

To health-check Caddy as well, add a local, credential-free URL to the watchdog
plist's `EnvironmentVariables`, then reinstall:

```xml
<key>QUANT_WATCHDOG_CADDY_URL</key>
<string>https://mac.feng37.top/</string>
```

Do not put a token or password in this URL because LaunchDaemon environment
variables are visible to privileged process inspection.

## Loopback boundary and encrypted backups

`install-secure-services.sh` installs the hardened backend/frontend
LaunchDaemons, the Caddy boundary, and a daily encrypted backup job. It validates
production secrets, plist files, the Caddyfile, and frontend artifacts before
changing a service. It keeps an exact configuration rollback copy and restores
it automatically if reload, bootstrap, or the final three-way health check
fails. It does not alter VNC, router, or DNS records.

```sh
sudo /bin/sh deploy/macos/install-secure-services.sh
/bin/sh deploy/macos/audit-network-boundary.sh
```

Before a privileged install, run the non-mutating installer and Caddy routing
regression checks:

```sh
/bin/sh deploy/macos/test-install-secure-services.sh
/bin/sh deploy/macos/test-caddy-secure-routing.sh
```

The application ports bind to loopback only. Caddy remains the sole HTTPS
application entry, blocks public API documentation, and admits `/api/admin/*`
only from private client addresses before the normal JWT/RBAC checks run.

The backup process runs as `xuhe`, uses SQLite online backup plus integrity
checks, and writes only AES-256-GCM archives and sanitized audit records to a
`0700` directory. Restore drills always target a random isolated directory and
never overwrite production. See
[`docs/LOCAL_SECURITY_AND_RECOVERY.md`](../../docs/LOCAL_SECURITY_AND_RECOVERY.md)
for the complete boundary, key, restore, RPO/RTO, and residual-risk contract.

The bulk frontend experiment runner is intentionally not installed as a
LaunchDaemon: it requires an interactive password and must not persist that
credential. Its checkpoint and deterministic submission intents provide
restart recovery, while idempotent browser `GET` calls use bounded retries for
short backend restart windows. Browser write actions are never blindly retried.
