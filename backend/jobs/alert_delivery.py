"""Safe, provider-neutral delivery of operational SLO alerts.

The job broker owns the SLO transition transaction.  This module owns the
separate *outbox* boundary: events are first committed to SQLite and are sent
later, at-least-once, only when a deliberately enabled HTTPS webhook has been
configured.  The payload is a fixed low-cardinality schema; it never contains
users, paths, job IDs, error text, endpoint URLs, or secrets.

This is intentionally not a generic webhook client.  Keeping the payload and
configuration narrow prevents an operational notification channel from
becoming an arbitrary data-exfiltration channel.
"""

from __future__ import annotations
from backend.core.timeutils import utc_now

import hashlib
import hmac
import ipaddress
import json
import re
import socket
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.config import settings

DELIVERY_SCHEMA_VERSION = "slo-webhook-alert/v1"
_TRANSITIONS = frozenset({"breach", "recovery"})
_EVENT_KINDS = frozenset({"transition", "escalation"})
_STATUSES = frozenset(
    {
        "disabled",
        "pending",
        "sending",
        "retry_wait",
        "delivered",
        "delivery_failed",
        "acknowledged",
    }
)
_MAX_BATCH_SIZE = 50

WebhookTransport = Callable[[str, bytes, dict[str, str], float], int]


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _secret_value(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value or "")


def _safe_webhook_configuration() -> tuple[bool, str | None, str | None]:
    """Return an enabled, safe HTTPS endpoint without ever logging it."""
    if not bool(settings.ALERT_WEBHOOK_ENABLED):
        return False, None, "disabled"
    endpoint = str(settings.ALERT_WEBHOOK_URL or "").strip()
    secret = _secret_value(settings.ALERT_WEBHOOK_SIGNING_SECRET)
    if not endpoint or not secret:
        return False, None, "missing_configuration"
    if len(endpoint) > 2048 or len(secret) < 16:
        return False, None, "invalid_configuration"
    try:
        parsed = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return False, None, "invalid_configuration"
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return False, None, "invalid_configuration"
    # Credentials in URLs leak through proxy/access logs even if this process
    # itself never records the endpoint.  Authentication belongs in the HMAC
    # header, or in a relay's private secret store.
    if any(
        re.search(r"(?:token|secret|password|authorization|api[_-]?key)", key, re.I)
        for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    ):
        return False, None, "unsafe_endpoint"
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        return False, None, "unsafe_endpoint"
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        return False, None, "unsafe_endpoint"
    # A webhook endpoint is an administrator-controlled configuration, but a
    # DNS name resolving exclusively to local/private addresses is still an
    # obvious misconfiguration and an avoidable SSRF footgun.
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError:
        # DNS can be temporarily unavailable.  Delivery records a bounded
        # network failure later; do not convert a transient resolver outage
        # into an unsafe configuration conclusion.
        addresses = set()
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                return False, None, "unsafe_endpoint"
        except ValueError:
            return False, None, "unsafe_endpoint"
    return True, endpoint, None


def initialize_alert_delivery_schema(conn: sqlite3.Connection) -> None:
    """Create append-only alert delivery records in the broker database."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slo_alert_delivery (
            delivery_id             TEXT PRIMARY KEY,
            alert_event_id          INTEGER NOT NULL,
            event_kind              TEXT NOT NULL,
            objective               TEXT NOT NULL,
            transition              TEXT NOT NULL,
            actual                  REAL,
            threshold               REAL NOT NULL,
            window_hours            INTEGER NOT NULL,
            status                  TEXT NOT NULL,
            attempt_count           INTEGER NOT NULL DEFAULT 0,
            next_attempt_at         TEXT,
            lease_expires_at        TEXT,
            delivered_at            TEXT,
            acknowledged_at         TEXT,
            escalation_due_at       TEXT,
            escalation_enqueued_at  TEXT,
            last_error_code         TEXT,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL,
            UNIQUE(alert_event_id, event_kind)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_slo_alert_delivery_due
        ON slo_alert_delivery(status, next_attempt_at, lease_expires_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slo_alert_delivery_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id     TEXT NOT NULL,
            attempt_number  INTEGER NOT NULL,
            outcome         TEXT NOT NULL,
            http_status     INTEGER,
            error_code      TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE(delivery_id, attempt_number)
        )
        """
    )


def queue_slo_alert_delivery(
    conn: sqlite3.Connection,
    *,
    alert_event_id: int,
    objective: str,
    transition: str,
    actual: float | None,
    threshold: float,
    window_hours: int,
    now: datetime | None = None,
) -> str:
    """Queue one immutable delivery record in the caller's SQL transaction.

    Disabled records are terminal by design.  Turning the endpoint on later
    must not replay historical incidents that the operator never opted to send.
    """
    if transition not in _TRANSITIONS:
        raise ValueError("invalid alert transition")
    current = now or utc_now()
    current_text = _timestamp(current)
    retention_hours = max(
        min(int(settings.JOB_OBSERVABILITY_RETENTION_HOURS), 24 * 31), 1
    )
    cutoff = _timestamp(current - timedelta(hours=retention_hours))
    # Keep retryable/awaiting-ack records until an operator resolves them, but
    # bound historical terminal delivery evidence like the parent SLO events.
    conn.execute(
        """
        DELETE FROM slo_alert_delivery_attempts
        WHERE delivery_id IN (
            SELECT delivery_id FROM slo_alert_delivery
            WHERE (status IN ('disabled', 'acknowledged', 'delivery_failed')
                   OR (status='delivered' AND (transition='recovery'
                       OR event_kind='escalation')))
              AND created_at < ?
        )
        """,
        (cutoff,),
    )
    conn.execute(
        """
        DELETE FROM slo_alert_delivery
        WHERE (status IN ('disabled', 'acknowledged', 'delivery_failed')
               OR (status='delivered' AND (transition='recovery'
                   OR event_kind='escalation')))
          AND created_at < ?
        """,
        (cutoff,),
    )
    enabled, _, _ = _safe_webhook_configuration()
    delivery_id = f"slo-{int(alert_event_id)}-transition"
    escalation_due_at = None
    if transition == "breach" and enabled:
        seconds = max(int(settings.ALERT_WEBHOOK_ACK_ESCALATION_SECONDS), 60)
        escalation_due_at = _timestamp(current + timedelta(seconds=seconds))
    conn.execute(
        """
        INSERT OR IGNORE INTO slo_alert_delivery
            (delivery_id, alert_event_id, event_kind, objective, transition,
             actual, threshold, window_hours, status, next_attempt_at,
             escalation_due_at, created_at, updated_at)
        VALUES (?, ?, 'transition', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            delivery_id,
            int(alert_event_id),
            objective,
            transition,
            actual,
            float(threshold),
            int(window_hours),
            "pending" if enabled else "disabled",
            current_text if enabled else None,
            escalation_due_at,
            current_text,
            current_text,
        ),
    )
    return delivery_id


def _payload(row: sqlite3.Row) -> bytes:
    event_kind = str(row["event_kind"])
    if event_kind not in _EVENT_KINDS:
        raise ValueError("invalid event kind")
    transition = str(row["transition"])
    if transition not in _TRANSITIONS:
        raise ValueError("invalid transition")
    body = {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "alert_id": str(row["delivery_id"]),
        "event_kind": event_kind,
        "objective": str(row["objective"]),
        "transition": transition,
        "severity": "critical" if transition == "breach" else "info",
        "actual": float(row["actual"]) if row["actual"] is not None else None,
        "threshold": float(row["threshold"]),
        "window_hours": int(row["window_hours"]),
        "occurred_at": str(row["created_at"]),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def signed_webhook_headers(*, delivery_id: str, body: bytes, timestamp: str) -> dict[str, str]:
    """Build fixed headers.  The signature binds timestamp, ID and body."""
    secret = _secret_value(settings.ALERT_WEBHOOK_SIGNING_SECRET).encode("utf-8")
    material = b"\n".join(
        [timestamp.encode("ascii"), delivery_id.encode("ascii"), body]
    )
    signature = hmac.new(secret, material, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "User-Agent": "quant-platform-alert-delivery/1",
        "X-Quant-Alert-Id": delivery_id,
        "X-Quant-Alert-Timestamp": timestamp,
        "X-Quant-Alert-Signature": f"sha256={signature}",
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _send_https_webhook(
    endpoint: str, body: bytes, headers: dict[str, str], timeout: float
) -> int:
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return int(response.getcode())


def _retry_delay_seconds(attempt_number: int) -> int:
    base = max(int(settings.ALERT_WEBHOOK_RETRY_BASE_SECONDS), 5)
    return min(base * (2 ** max(attempt_number - 1, 0)), 3600)


def _claim_due_delivery(
    conn: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    now_text = _timestamp(now)
    lease_text = _timestamp(
        now + timedelta(seconds=max(int(settings.ALERT_WEBHOOK_TIMEOUT_SECONDS) * 2, 30))
    )
    max_attempts = max(min(int(settings.ALERT_WEBHOOK_MAX_ATTEMPTS), 10), 1)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT * FROM slo_alert_delivery
            WHERE (
                    status IN ('pending', 'retry_wait')
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  )
               OR (status='sending' AND lease_expires_at IS NOT NULL
                   AND lease_expires_at <= ?)
            ORDER BY created_at, delivery_id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        attempts = int(row["attempt_count"] or 0) + 1
        if attempts > max_attempts:
            conn.execute(
                """
                UPDATE slo_alert_delivery
                SET status='delivery_failed', next_attempt_at=NULL,
                    lease_expires_at=NULL, last_error_code='attempts_exhausted',
                    updated_at=?
                WHERE delivery_id=?
                """,
                (now_text, row["delivery_id"]),
            )
            conn.commit()
            return None
        updated = conn.execute(
            """
            UPDATE slo_alert_delivery
            SET status='sending', attempt_count=?, lease_expires_at=?,
                next_attempt_at=NULL, updated_at=?
            WHERE delivery_id=?
              AND (status IN ('pending', 'retry_wait')
                   OR (status='sending' AND lease_expires_at <= ?))
            """,
            (attempts, lease_text, now_text, row["delivery_id"], now_text),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        claimed = conn.execute(
            "SELECT * FROM slo_alert_delivery WHERE delivery_id=?",
            (row["delivery_id"],),
        ).fetchone()
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise


def _record_delivery_result(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: datetime,
    http_status: int | None,
    error_code: str | None,
) -> str:
    now_text = _timestamp(now)
    attempt = int(row["attempt_count"])
    success = http_status is not None and 200 <= http_status < 300
    retryable = (
        http_status is None
        or http_status == 408
        or http_status == 429
        or (http_status is not None and 500 <= http_status < 600)
    )
    max_attempts = max(min(int(settings.ALERT_WEBHOOK_MAX_ATTEMPTS), 10), 1)
    if success:
        status = "delivered"
        next_attempt_at = None
        stored_error = None
    elif retryable and attempt < max_attempts:
        status = "retry_wait"
        next_attempt_at = _timestamp(now + timedelta(seconds=_retry_delay_seconds(attempt)))
        stored_error = error_code or "http_retryable"
    else:
        status = "delivery_failed"
        next_attempt_at = None
        stored_error = error_code or "http_rejected"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE slo_alert_delivery
            SET status=?, next_attempt_at=?, lease_expires_at=NULL,
                delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END,
                last_error_code=?, updated_at=?
            WHERE delivery_id=? AND status='sending' AND attempt_count=?
            """,
            (
                status,
                next_attempt_at,
                status,
                now_text,
                stored_error,
                now_text,
                row["delivery_id"],
                attempt,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO slo_alert_delivery_attempts
                (delivery_id, attempt_number, outcome, http_status, error_code, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["delivery_id"],
                attempt,
                status,
                http_status,
                stored_error,
                now_text,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return status


def _enqueue_due_escalations(conn: sqlite3.Connection, now: datetime) -> int:
    """Create one escalation delivery for delivered, unacknowledged breaches."""
    now_text = _timestamp(now)
    rows = conn.execute(
        """
        SELECT * FROM slo_alert_delivery
        WHERE event_kind='transition' AND transition='breach'
          AND status='delivered' AND acknowledged_at IS NULL
          AND escalation_due_at IS NOT NULL AND escalation_due_at <= ?
          AND escalation_enqueued_at IS NULL
        ORDER BY delivery_id
        """,
        (now_text,),
    ).fetchall()
    if not rows:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            escalation_id = f"slo-{int(row['alert_event_id'])}-escalation"
            conn.execute(
                """
                INSERT OR IGNORE INTO slo_alert_delivery
                    (delivery_id, alert_event_id, event_kind, objective, transition,
                     actual, threshold, window_hours, status, next_attempt_at,
                     created_at, updated_at)
                VALUES (?, ?, 'escalation', ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    escalation_id,
                    int(row["alert_event_id"]),
                    row["objective"],
                    row["transition"],
                    row["actual"],
                    row["threshold"],
                    row["window_hours"],
                    now_text,
                    now_text,
                    now_text,
                ),
            )
            conn.execute(
                """
                UPDATE slo_alert_delivery
                SET escalation_enqueued_at=?, updated_at=?
                WHERE delivery_id=? AND escalation_enqueued_at IS NULL
                """,
                (now_text, now_text, row["delivery_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def process_alert_delivery_outbox(
    db_path: str,
    *,
    transport: WebhookTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deliver a bounded batch and return safe aggregate counters only.

    Exceptions from the network are converted to stable error codes and the
    outbox remains retryable.  The endpoint, response body and exception text
    are deliberately never persisted or logged.
    """
    current = now or utc_now()
    enabled, endpoint, config_error = _safe_webhook_configuration()
    report: dict[str, Any] = {
        "schema_version": "slo-alert-delivery/v1",
        "enabled": enabled,
        "configuration": "ready" if enabled else str(config_error),
        "attempted": 0,
        "delivered": 0,
        "retry_wait": 0,
        "delivery_failed": 0,
        "escalations_enqueued": 0,
    }
    if not enabled or endpoint is None:
        return report
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        initialize_alert_delivery_schema(conn)
        report["escalations_enqueued"] = _enqueue_due_escalations(conn, current)
        sender = transport or _send_https_webhook
        for _ in range(max(min(int(settings.ALERT_WEBHOOK_BATCH_SIZE), _MAX_BATCH_SIZE), 1)):
            row = _claim_due_delivery(conn, current)
            if row is None:
                break
            report["attempted"] += 1
            status_code: int | None = None
            error_code: str | None = None
            try:
                timestamp = _timestamp(current)
                body = _payload(row)
                status_code = int(
                    sender(
                        endpoint,
                        body,
                        signed_webhook_headers(
                            delivery_id=str(row["delivery_id"]),
                            body=body,
                            timestamp=timestamp,
                        ),
                        max(float(settings.ALERT_WEBHOOK_TIMEOUT_SECONDS), 1.0),
                    )
                )
            except TimeoutError:
                error_code = "timeout"
            except urllib.error.HTTPError as exc:
                status_code = int(exc.code)
            except (OSError, urllib.error.URLError, ValueError):
                error_code = "network_error"
            status = _record_delivery_result(
                conn,
                row,
                now=current,
                http_status=status_code,
                error_code=error_code,
            )
            if status in report:
                report[status] += 1
    finally:
        conn.close()
    return report


def alert_delivery_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read aggregate state without exposing delivery IDs or configuration."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM slo_alert_delivery GROUP BY status"
    ).fetchall()
    statuses = {status: 0 for status in sorted(_STATUSES)}
    statuses.update({str(row["status"]): int(row["count"]) for row in rows})
    unacknowledged = conn.execute(
        """
        SELECT COUNT(*) AS count FROM slo_alert_delivery
        WHERE event_kind='transition' AND transition='breach'
          AND status='delivered' AND acknowledged_at IS NULL
        """
    ).fetchone()
    return {
        "schema_version": "slo-alert-delivery/v1",
        "enabled": bool(settings.ALERT_WEBHOOK_ENABLED),
        "statuses": statuses,
        "unacknowledged_breaches": int(unacknowledged["count"] or 0),
        "acknowledgement_escalation_seconds": max(
            int(settings.ALERT_WEBHOOK_ACK_ESCALATION_SECONDS), 60
        ),
    }


def acknowledge_alert_delivery(
    db_path: str, delivery_id: str, *, now: datetime | None = None
) -> bool:
    """Record a local administrator acknowledgement for a delivered breach."""
    if not delivery_id.startswith("slo-") or len(delivery_id) > 96:
        return False
    current_text = _timestamp(now or utc_now())
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            UPDATE slo_alert_delivery
            SET status='acknowledged', acknowledged_at=?, updated_at=?
            WHERE delivery_id=? AND event_kind='transition'
              AND transition='breach' AND status='delivered'
              AND acknowledged_at IS NULL
            """,
            (current_text, current_text, delivery_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
