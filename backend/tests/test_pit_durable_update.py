from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from backend.services.pit_durable_update import (
    GovernedPitAutomationActions,
    PitAutomationIdentityError,
    PitAutomationPolicy,
    PitAutomationPolicyError,
    PitDurableUpdateMachine,
    PitDurableUpdateStore,
    require_automation_service_identity,
)


def test_governed_durable_collection_uses_governance_refresh_not_market_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def fake_refresh(*, actor_user_id: int, **_kwargs: Any) -> dict[str, Any]:
        calls.append(actor_user_id)
        return {"status": "pending_review", "package_id": "pitpkg_" + "a" * 32}

    async def forbidden_market_update(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        pytest.fail("durable collection must not attempt market data update")

    monkeypatch.setattr(
        "backend.services.maintenance.run_pit_governance_refresh", fake_refresh
    )
    monkeypatch.setattr(
        "backend.services.maintenance.run_data_update", forbidden_market_update
    )

    result = asyncio.run(
        GovernedPitAutomationActions().collect(
            idempotency_key="k" * 32,
            actor_user_id=17,
        )
    )
    assert result["status"] == "pending_review"
    assert calls == [17]


GREEN = {
    "classification": "green",
    "same_source": True,
    "same_schema": True,
    "append_only": True,
    "evidence_valid": True,
    "provider": "csindex_official",
    "import_schema": "point-in-time-master-import/v2",
}


class FakeActions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.effects: dict[str, dict[str, Any]] = {}
        self.active = "batch-old"
        self.fail_once: str | None = None
        self.crash_once: str | None = None
        self.classification = dict(GREEN)

    async def _effect(self, stage: str, key: str, result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((stage, key))
        if self.fail_once == stage:
            self.fail_once = None
            raise RuntimeError(f"transient {stage}")
        existing = self.effects.setdefault(key, result)
        if self.crash_once == stage:
            self.crash_once = None
            raise KeyboardInterrupt(f"crash after {stage} effect")
        return existing

    async def collect(self, *, idempotency_key: str, actor_user_id: int) -> dict[str, Any]:
        return await self._effect(
            "collect", idempotency_key, {"candidate": "batch-new", "actor": actor_user_id}
        )

    async def validate(self, candidate: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return await self._effect(
            "validate", idempotency_key, {**candidate, "evidence_valid": True}
        )

    async def classify(self, validated: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        del validated
        return await self._effect("classify", idempotency_key, dict(self.classification))

    async def import_candidate(
        self,
        validated: dict[str, Any],
        classification: dict[str, Any],
        *,
        idempotency_key: str,
        actor_user_id: int,
        policy_sha256: str,
    ) -> dict[str, Any]:
        del classification, actor_user_id
        return await self._effect(
            "import", idempotency_key, {"batch": validated["candidate"], "policy": policy_sha256}
        )

    async def activate(
        self,
        imported: dict[str, Any],
        *,
        idempotency_key: str,
        actor_user_id: int,
        policy_sha256: str,
    ) -> dict[str, Any]:
        del actor_user_id, policy_sha256
        result = await self._effect(
            "activate", idempotency_key, {"activated": True, "batch": imported["batch"]}
        )
        self.active = result["batch"]
        return result

    async def canary(self, activated: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        del activated
        return await self._effect("canary", idempotency_key, {"healthy": True})

    async def monitor(self, activated: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        del activated
        return await self._effect("monitor", idempotency_key, {"healthy": True})


def _machine(
    tmp_path: Path,
    actions: FakeActions,
    *,
    enabled: bool = True,
    stage_timeout_seconds: float = 300,
) -> PitDurableUpdateMachine:
    return PitDurableUpdateMachine(
        store=PitDurableUpdateStore(tmp_path / "automation.db"),
        actions=actions,
        policy=PitAutomationPolicy(personal_mode=enabled, auto_activate_green=enabled),
        actor_user_id=17,
        lease_seconds=30,
        stage_timeout_seconds=stage_timeout_seconds,
        retry_base_seconds=1,
    )


def test_green_candidate_completes_all_durable_stages_once(tmp_path: Path) -> None:
    actions = FakeActions()
    machine = _machine(tmp_path, actions)

    completed = asyncio.run(machine.run("cycle:2026-08-01:1"))
    duplicate = asyncio.run(machine.run("cycle:2026-08-01:1"))

    assert completed["status"] == "completed"
    assert completed["stage"] == "completed"
    assert duplicate["run_id"] == completed["run_id"]
    assert [stage for stage, _ in actions.calls] == [
        "collect",
        "validate",
        "classify",
        "import",
        "activate",
        "canary",
        "monitor",
    ]
    assert actions.active == "batch-new"


def test_transient_failure_retries_same_stage_not_whole_day(tmp_path: Path) -> None:
    actions = FakeActions()
    actions.fail_once = "validate"
    machine = _machine(tmp_path, actions)

    failed = asyncio.run(machine.run("cycle:retry"))
    assert machine.store.due_retry_keys() == []
    with sqlite3.connect(tmp_path / "automation.db") as connection:
        connection.execute("UPDATE pit_automation_runs SET next_retry_at='2000-01-01T00:00:00Z'")
    assert machine.store.due_retry_keys() == ["cycle:retry"]
    recovered = asyncio.run(machine.run("cycle:retry"))

    assert failed["status"] == "retry_wait"
    assert failed["stage"] == "validate"
    assert failed["next_retry_at"] is not None
    assert recovered["status"] == "completed"
    assert [stage for stage, _ in actions.calls].count("collect") == 1
    assert [stage for stage, _ in actions.calls].count("validate") == 2


def test_crash_after_effect_recovers_with_stable_idempotency_key(tmp_path: Path) -> None:
    """A post-effect crash is re-raised once, without a leaked task warning."""

    actions = FakeActions()
    actions.crash_once = "import"
    machine = _machine(tmp_path, actions)

    with pytest.raises(KeyboardInterrupt, match="crash after import"):
        asyncio.run(machine.run("cycle:crash"))
    with sqlite3.connect(tmp_path / "automation.db") as connection:
        connection.execute("UPDATE pit_automation_runs SET lease_expires_at='2000-01-01T00:00:00Z'")
    recovered = asyncio.run(machine.run("cycle:crash"))

    assert recovered["status"] == "completed"
    import_keys = [key for stage, key in actions.calls if stage == "import"]
    assert len(import_keys) == 2
    assert len(set(import_keys)) == 1
    assert len([key for key in actions.effects if key.endswith(":import")]) == 1


def test_cancelling_stage_waits_for_effect_task_cleanup(tmp_path: Path) -> None:
    machine = _machine(tmp_path, FakeActions())

    async def scenario() -> None:
        started = asyncio.Event()
        cleaned_up = asyncio.Event()

        async def blocking_effect() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        runner = asyncio.create_task(
            machine._invoke_with_lease(
                blocking_effect(),
                run_id="test-run",
                owner="test-owner",
                generation=1,
                stage="collect",
            )
        )
        await started.wait()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert cleaned_up.is_set()

    asyncio.run(scenario())


def test_stage_timeout_cleans_child_and_retries_only_uncommitted_stage(tmp_path: Path) -> None:
    class TimeoutOnceActions(FakeActions):
        def __init__(self) -> None:
            super().__init__()
            self.timed_out = False
            self.cancelled = asyncio.Event()

        async def import_candidate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if not self.timed_out:
                self.timed_out = True
                self.calls.append(("import", str(kwargs["idempotency_key"])))
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()
            return await super().import_candidate(*args, **kwargs)

    actions = TimeoutOnceActions()
    machine = _machine(tmp_path, actions, stage_timeout_seconds=0.03)

    timed_out = asyncio.run(machine.run("cycle:stage-timeout"))

    assert timed_out["status"] == "retry_wait"
    assert timed_out["stage"] == "import"
    assert timed_out["last_error_code"] == "import_timeout"
    assert actions.cancelled.is_set()
    with sqlite3.connect(tmp_path / "automation.db") as connection:
        connection.execute("UPDATE pit_automation_runs SET next_retry_at='2000-01-01T00:00:00Z'")
    recovered = asyncio.run(machine.run("cycle:stage-timeout"))

    assert recovered["status"] == "completed"
    assert [stage for stage, _ in actions.calls].count("collect") == 1
    assert [stage for stage, _ in actions.calls].count("validate") == 1
    assert [stage for stage, _ in actions.calls].count("classify") == 1
    assert [stage for stage, _ in actions.calls].count("import") == 2


def test_expired_running_lease_becomes_due_and_reclaims_same_stage(tmp_path: Path) -> None:
    actions = FakeActions()
    actions.crash_once = "import"
    machine = _machine(tmp_path, actions)

    with pytest.raises(KeyboardInterrupt, match="crash after import"):
        asyncio.run(machine.run("cycle:expired-lease"))
    with sqlite3.connect(tmp_path / "automation.db") as connection:
        connection.execute("UPDATE pit_automation_runs SET lease_expires_at='2000-01-01T00:00:00Z'")

    assert machine.store.due_retry_keys() == ["cycle:expired-lease"]
    recovered = asyncio.run(machine.run("cycle:expired-lease"))

    assert recovered["status"] == "completed"
    assert [stage for stage, _ in actions.calls].count("collect") == 1
    assert [stage for stage, _ in actions.calls].count("validate") == 1
    assert [stage for stage, _ in actions.calls].count("classify") == 1
    assert [stage for stage, _ in actions.calls].count("import") == 2
    with sqlite3.connect(tmp_path / "automation.db") as connection:
        events = connection.execute(
            "SELECT event_type FROM pit_automation_events WHERE run_id=?",
            (recovered["run_id"],),
        ).fetchall()
    assert ("lease_expired_recovered",) in events


def test_failure_before_activation_does_not_change_active_batch(tmp_path: Path) -> None:
    actions = FakeActions()
    actions.fail_once = "import"
    state = asyncio.run(_machine(tmp_path, actions).run("cycle:no-active-change"))

    assert state["status"] == "retry_wait"
    assert state["stage"] == "import"
    assert actions.active == "batch-old"


@pytest.mark.parametrize(
    ("classification", "expected"),
    [("yellow", "hold"), ("red", "quarantined")],
)
def test_non_green_never_imports_or_activates(
    tmp_path: Path, classification: str, expected: str
) -> None:
    actions = FakeActions()
    actions.classification = {**GREEN, "classification": classification}
    result = asyncio.run(_machine(tmp_path, actions).run(f"cycle:{classification}"))

    assert result["status"] == expected
    assert actions.active == "batch-old"
    assert not ({"import", "activate"} & {stage for stage, _ in actions.calls})


def test_green_is_held_when_personal_preauthorization_is_disabled(tmp_path: Path) -> None:
    actions = FakeActions()
    result = asyncio.run(_machine(tmp_path, actions, enabled=False).run("cycle:policy-off"))

    assert result["status"] == "hold"
    assert actions.active == "batch-old"


def test_legacy_v1_never_auto_activates_and_cannot_reconfigure_policy(tmp_path: Path) -> None:
    actions = FakeActions()
    actions.classification = {
        **GREEN,
        "import_schema": "point-in-time-master-import/v1",
    }
    machine = _machine(tmp_path, actions)

    result = asyncio.run(machine.run("cycle:legacy-v1"))

    assert result["status"] == "hold"
    assert actions.active == "batch-old"
    assert not ({"import", "activate"} & {stage for stage, _ in actions.calls})
    with pytest.raises(PitAutomationPolicyError, match="bitemporal v2"):
        PitAutomationPolicy(
            personal_mode=True,
            auto_activate_green=True,
            allowed_import_schema="point-in-time-master-import/v1",
        )


def _users_db(path: Path, *, username: str, is_admin: int = 0, active: int = 1) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, is_admin INTEGER, is_active INTEGER);
            CREATE TABLE user_permissions (user_id INTEGER, permission TEXT);
            """
        )
        connection.execute("INSERT INTO users VALUES (17, ?, ?, ?)", (username, is_admin, active))
        connection.execute("INSERT INTO user_permissions VALUES (17, 'data:update')")


def test_service_identity_must_be_exact_active_and_non_admin(tmp_path: Path) -> None:
    valid = tmp_path / "valid.db"
    _users_db(valid, username="pit_automation")
    assert (
        require_automation_service_identity(user_id=17, username="pit_automation", users_db=valid)[
            "id"
        ]
        == 17
    )

    admin = tmp_path / "admin.db"
    _users_db(admin, username="pit_automation", is_admin=1)
    with pytest.raises(PitAutomationIdentityError):
        require_automation_service_identity(user_id=17, username="pit_automation", users_db=admin)

    wrong = tmp_path / "wrong.db"
    _users_db(wrong, username="actual_user")
    with pytest.raises(PitAutomationIdentityError):
        require_automation_service_identity(user_id=17, username="pit_automation", users_db=wrong)
