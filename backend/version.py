"""Process-stable runtime build identity and worktree drift observations.

The identity is captured exactly once when this module is imported.  A running
process must never claim a newer on-disk ``HEAD`` as the code it has already
loaded.  Callers may separately expose a best-effort drift observation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping


APP_VERSION = "0.2.3"
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_SOURCE_SUFFIXES = {
    ".cfg",
    ".ini",
    ".js",
    ".jsx",
    ".json",
    ".lock",
    ".py",
    ".pyi",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
_RUNTIME_SOURCE_NAMES = {
    "dockerfile",
    "makefile",
    "requirements.txt",
}


def _run_git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _capture_worktree_state() -> dict[str, Any]:
    """Read the current repository state without applying build overrides."""
    sha = (_run_git("rev-parse", "HEAD") or "unknown").lower()
    if not _COMMIT_PATTERN.fullmatch(sha):
        sha = "unknown"
    tracked_status = _run_git(
        "status",
        "--porcelain",
        "--untracked-files=no",
    )
    untracked = _run_git("ls-files", "--others", "--exclude-standard")
    runtime_names: list[str] = []
    if untracked is not None:
        for relative_name in untracked.splitlines():
            candidate = Path(relative_name)
            if (
                candidate.suffix.lower() in _RUNTIME_SOURCE_SUFFIXES
                or candidate.name.lower() in _RUNTIME_SOURCE_NAMES
            ):
                runtime_names.append(relative_name)
    tracked_dirty = tracked_status is None or bool(tracked_status)
    unavailable = tracked_status is None or untracked is None
    return {
        "sha": sha,
        "dirty": unavailable or tracked_dirty or bool(runtime_names),
        "tracked_dirty": tracked_dirty,
        "untracked_runtime_file_count": len(runtime_names),
        # Digests detect changes even when the dirty flags/counts stay equal,
        # without persisting local filenames in research evidence.
        "tracked_status_sha256": (
            _sha256_text(tracked_status) if tracked_status is not None else None
        ),
        "untracked_runtime_files_sha256": (
            _sha256_text("\n".join(sorted(runtime_names)))
            if untracked is not None
            else None
        ),
    }


def _capture_runtime_code_identity() -> dict[str, Any]:
    override = os.getenv("QUANT_PLATFORM_BUILD_COMMIT", "").strip().lower()
    if _COMMIT_PATTERN.fullmatch(override):
        return {
            "sha": override,
            "dirty": False,
            "tracked_dirty": False,
            "untracked_runtime_file_count": 0,
            "tracked_status_sha256": None,
            "untracked_runtime_files_sha256": None,
            "source": "build_override",
        }
    return {
        **_capture_worktree_state(),
        "source": "git_worktree_at_process_start",
    }


RUNTIME_STARTED_AT = datetime.now(timezone.utc).isoformat()
_RUNTIME_CODE_IDENTITY = _capture_runtime_code_identity()
APP_COMMIT = str(_RUNTIME_CODE_IDENTITY["sha"])


def runtime_code_identity() -> dict[str, Any]:
    """Return a copy of the immutable process-start code identity."""
    return dict(_RUNTIME_CODE_IDENTITY)


def runtime_code_version(identity: Mapping[str, Any] | None = None) -> str:
    state = identity if identity is not None else _RUNTIME_CODE_IDENTITY
    sha = str(state.get("sha") or "unknown")
    return f"{sha}-dirty" if state.get("dirty") else sha


def observed_worktree_drift() -> dict[str, Any]:
    """Report disk drift separately; never replace the runtime identity."""
    observed = _capture_worktree_state()
    runtime = _RUNTIME_CODE_IDENTITY
    comparable_fields = (
        "sha",
        "dirty",
        "tracked_dirty",
        "untracked_runtime_file_count",
        "tracked_status_sha256",
        "untracked_runtime_files_sha256",
    )
    differences = [
        field
        for field in comparable_fields
        if runtime.get(field) != observed.get(field)
    ]
    if runtime.get("source") == "build_override":
        # Build metadata has no startup worktree status to compare.  Validate
        # the commit and reject an observed dirty checkout, while treating a
        # deployment image without ``.git`` as unverifiable rather than drift.
        differences = []
        if observed["sha"] != "unknown" and observed["sha"] != runtime["sha"]:
            differences.append("sha")
        if observed["sha"] != "unknown" and observed["dirty"]:
            differences.append("dirty")
    return {
        "detected": bool(differences),
        "fields": differences,
        "observed": observed,
    }


def runtime_code_evidence() -> dict[str, Any]:
    """Portable evidence shared by health, jobs and research artifacts."""
    identity = runtime_code_identity()
    return {
        "identity": identity,
        "code_version": runtime_code_version(identity),
        "observed_worktree_drift": observed_worktree_drift(),
    }


def code_identities_match(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> bool:
    """Compare the fields that establish which source a process loaded."""
    fields = (
        "sha",
        "dirty",
        "tracked_dirty",
        "untracked_runtime_file_count",
        "tracked_status_sha256",
        "untracked_runtime_files_sha256",
    )
    return all(expected.get(field) == actual.get(field) for field in fields)
