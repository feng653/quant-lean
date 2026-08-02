from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import Response

from backend import main
from backend import version as runtime_version
from backend.services import research_manifest


def test_health_never_claims_changed_disk_head(
    monkeypatch,
) -> None:
    startup_identity = runtime_version.runtime_code_identity()

    def changed_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return "f" * 40
        if args[0] == "status":
            return " M backend/version.py"
        if args[0] == "ls-files":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(runtime_version, "_run_git", changed_git)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    response = Response()

    payload = asyncio.run(main.health_check(request, response))

    assert response.status_code == 200
    assert payload["commit"] == startup_identity["sha"]
    assert payload["code_identity"] == startup_identity
    assert payload["code_version"] == runtime_version.runtime_code_version()
    assert research_manifest.capture_git_state() == payload["code_identity"]
    assert (
        research_manifest.code_version(research_manifest.capture_git_state())
        == payload["code_version"]
    )
    assert payload["observed_worktree_drift"]["detected"] is True
    assert payload["observed_worktree_drift"]["observed"]["sha"] == "f" * 40
