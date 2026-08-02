from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from backend.api import jobs as jobs_api
from backend.config import settings
from backend.jobs.alert_delivery import (
    acknowledge_alert_delivery,
    initialize_alert_delivery_schema,
    process_alert_delivery_outbox,
    queue_slo_alert_delivery,
)
from backend.jobs.broker import JobBroker


def _now() -> datetime:
    return datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)


def _configure_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_ENABLED", True)
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://hooks.example.test/slo")
    monkeypatch.setattr(
        settings,
        "ALERT_WEBHOOK_SIGNING_SECRET",
        SecretStr("test-signing-secret-at-least-16"),
    )
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_RETRY_BASE_SECONDS", 5)
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_BATCH_SIZE", 5)
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_ACK_ESCALATION_SECONDS", 60)
    monkeypatch.setattr(
        "backend.jobs.alert_delivery.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
    )


def _queue(db_path: str, *, event_id: int = 1, now: datetime | None = None) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    initialize_alert_delivery_schema(conn)
    delivery_id = queue_slo_alert_delivery(
        conn,
        alert_event_id=event_id,
        objective="sqlite_contention_events",
        transition="breach",
        actual=6.0,
        threshold=5.0,
        window_hours=24,
        now=now or _now(),
    )
    conn.commit()
    conn.close()
    return delivery_id


def _rows(db_path: str, table: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    conn.close()
    return rows


def test_delivery_is_disabled_by_default_and_never_calls_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_ENABLED", False)
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://hooks.example.test/slo")
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_SIGNING_SECRET", SecretStr("private-token"))
    db_path = str(tmp_path / "jobs.db")
    _queue(db_path)

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("disabled outbox must not send")

    report = process_alert_delivery_outbox(db_path, transport=forbidden, now=_now())
    assert report["enabled"] is False
    assert report["configuration"] == "disabled"
    assert report["attempted"] == 0
    rows = _rows(db_path, "slo_alert_delivery")
    assert rows[0]["status"] == "disabled"
    assert "private-token" not in str(rows[0])
    assert "hooks.example" not in str(rows[0])


def test_retry_is_idempotent_and_payload_is_signed_and_redacted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_enabled(monkeypatch)
    db_path = str(tmp_path / "jobs.db")
    delivery_id = _queue(db_path)
    # The same source SLO event cannot create a duplicate delivery.
    assert _queue(db_path) == delivery_id

    first = process_alert_delivery_outbox(
        db_path,
        transport=lambda *args: (_ for _ in ()).throw(TimeoutError()),
        now=_now(),
    )
    assert first["attempted"] == 1
    assert first["retry_wait"] == 1
    assert _rows(db_path, "slo_alert_delivery")[0]["attempt_count"] == 1

    captured: dict[str, object] = {}

    def delivered(endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        captured.update(endpoint=endpoint, body=body, headers=headers, timeout=timeout)
        return 204

    second = process_alert_delivery_outbox(
        db_path,
        transport=delivered,
        now=_now() + timedelta(seconds=5),
    )
    assert second["delivered"] == 1
    assert captured["endpoint"] == "https://hooks.example.test/slo"
    body = captured["body"]
    headers = captured["headers"]
    assert isinstance(body, bytes)
    assert isinstance(headers, dict)
    payload = json.loads(body)
    assert payload == {
        "actual": 6.0,
        "alert_id": delivery_id,
        "event_kind": "transition",
        "objective": "sqlite_contention_events",
        "occurred_at": "2026-08-02T10:00:00Z",
        "schema_version": "slo-webhook-alert/v1",
        "severity": "critical",
        "threshold": 5.0,
        "transition": "breach",
        "window_hours": 24,
    }
    material = b"\n".join(
        [headers["X-Quant-Alert-Timestamp"].encode(), delivery_id.encode(), body]
    )
    expected = hmac.new(
        b"test-signing-secret-at-least-16", material, hashlib.sha256
    ).hexdigest()
    assert headers["X-Quant-Alert-Signature"] == f"sha256={expected}"
    assert "secret" not in body.decode().lower()
    assert "hooks.example" not in str(_rows(db_path, "slo_alert_delivery"))
    attempts = _rows(db_path, "slo_alert_delivery_attempts")
    assert [row["outcome"] for row in attempts] == ["retry_wait", "delivered"]


def test_delivery_acknowledgement_prevents_escalation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_enabled(monkeypatch)
    db_path = str(tmp_path / "jobs.db")
    delivery_id = _queue(db_path)
    assert process_alert_delivery_outbox(
        db_path, transport=lambda *args: 202, now=_now()
    )["delivered"] == 1
    assert acknowledge_alert_delivery(db_path, delivery_id, now=_now() + timedelta(seconds=1))
    later = process_alert_delivery_outbox(
        db_path,
        transport=lambda *args: (_ for _ in ()).throw(AssertionError("must not escalate")),
        now=_now() + timedelta(seconds=61),
    )
    assert later["escalations_enqueued"] == 0
    assert _rows(db_path, "slo_alert_delivery")[0]["status"] == "acknowledged"


def test_unacknowledged_delivery_escalates_once_and_unsafe_endpoint_is_refused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_enabled(monkeypatch)
    db_path = str(tmp_path / "jobs.db")
    _queue(db_path)
    assert process_alert_delivery_outbox(
        db_path, transport=lambda *args: 200, now=_now()
    )["delivered"] == 1
    escalation = process_alert_delivery_outbox(
        db_path, transport=lambda *args: 200, now=_now() + timedelta(seconds=61)
    )
    assert escalation["escalations_enqueued"] == 1
    assert escalation["delivered"] == 1
    repeated = process_alert_delivery_outbox(
        db_path, transport=lambda *args: 200, now=_now() + timedelta(seconds=62)
    )
    assert repeated["escalations_enqueued"] == 0
    assert repeated["attempted"] == 0
    assert len(_rows(db_path, "slo_alert_delivery")) == 2

    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://127.0.0.1/hook")
    rejected = process_alert_delivery_outbox(db_path, transport=lambda *args: 200)
    assert rejected["enabled"] is False
    assert rejected["configuration"] == "unsafe_endpoint"
    monkeypatch.setattr(
        settings, "ALERT_WEBHOOK_URL", "https://hooks.example.test/slo?token=forbidden"
    )
    assert process_alert_delivery_outbox(db_path)["configuration"] == "unsafe_endpoint"


def test_broker_slo_transition_creates_disabled_outbox_record(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(settings, "ALERT_WEBHOOK_ENABLED", False)
        monkeypatch.setattr(settings, "JOB_SLO_CONFIRMATIONS_REQUIRED", 2)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        await broker.record_operational_event("sqlite_contention", "storage", value=6)
        await broker.evaluate_slo_alerts(window_hours=24)
        await broker.evaluate_slo_alerts(window_hours=24)
        payload = await broker.get_observability(window_hours=24)
        delivery = payload["slo"]["alerting"]["external_delivery"]
        assert delivery["enabled"] is False
        assert delivery["statuses"]["disabled"] == 1
        assert delivery["unacknowledged_breaches"] == 0

    asyncio.run(scenario())


def test_admin_acknowledgement_endpoint_is_authorized_and_one_time(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        _configure_enabled(monkeypatch)
        broker = JobBroker(str(tmp_path / "jobs.db"))
        delivery_id = _queue(str(tmp_path / "jobs.db"))
        assert process_alert_delivery_outbox(
            str(tmp_path / "jobs.db"), transport=lambda *args: 200, now=_now()
        )["delivered"] == 1
        monkeypatch.setattr(jobs_api, "get_job_broker", lambda: broker)
        with pytest.raises(HTTPException) as denied:
            await jobs_api.acknowledge_job_observability_alert(
                delivery_id, user={"id": 2, "is_admin": False}
            )
        assert denied.value.status_code == 403
        response = await jobs_api.acknowledge_job_observability_alert(
            delivery_id, user={"id": 1, "is_admin": True}
        )
        assert response == {"data": {"acknowledged": True}}
        with pytest.raises(HTTPException) as absent:
            await jobs_api.acknowledge_job_observability_alert(
                delivery_id, user={"id": 1, "is_admin": True}
            )
        assert absent.value.status_code == 404

    asyncio.run(scenario())
