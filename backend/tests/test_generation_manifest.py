from __future__ import annotations

import threading
from pathlib import Path

import pytest

from backend.data.generation_manifest import (
    GenerationManifestError,
    GenerationManifestStore,
)


def _stage(root: Path, *, pivot: bytes, metadata: bytes) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for name, payload in {"pivot": pivot, "metadata": metadata}.items():
        path = root / f".{name}.staged"
        path.write_bytes(payload)
        result[name] = path
    return result


def _store(root: Path) -> GenerationManifestStore:
    return GenerationManifestStore(
        root,
        required_artifacts={"pivot", "metadata"},
    )


def test_interrupted_publish_keeps_previous_generation_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = store.publish_staged(
        "csi300", _stage(tmp_path, pivot=b"pivot-v1", metadata=b"meta-v1")
    )
    calls = 0
    original = store._install_staged_artifact

    def fail_after_first(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected storage interruption")
        original(source, target)

    monkeypatch.setattr(store, "_install_staged_artifact", fail_after_first)
    with pytest.raises(OSError, match="injected"):
        store.publish_staged(
            "csi300", _stage(tmp_path, pivot=b"pivot-v2", metadata=b"meta-v2")
        )

    active = store.load("csi300")
    assert active is not None
    assert active.generation_id == first.generation_id
    assert active.artifacts["pivot"].read_bytes() == b"pivot-v1"
    assert active.artifacts["metadata"].read_bytes() == b"meta-v1"


def test_concurrent_readers_observe_only_complete_old_or_new_generation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.publish_staged(
        "csi300", _stage(tmp_path, pivot=b"pivot-v1", metadata=b"meta-v1")
    )
    start = threading.Barrier(2)
    observed: list[tuple[bytes, bytes]] = []
    failures: list[BaseException] = []

    def reader() -> None:
        try:
            start.wait(timeout=5)
            for _ in range(100):
                active = store.load("csi300")
                assert active is not None
                observed.append(
                    (
                        active.artifacts["pivot"].read_bytes(),
                        active.artifacts["metadata"].read_bytes(),
                    )
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    start.wait(timeout=5)
    store.publish_staged(
        "csi300", _stage(tmp_path, pivot=b"pivot-v2", metadata=b"meta-v2")
    )
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert not failures
    assert observed
    assert set(observed) <= {
        (b"pivot-v1", b"meta-v1"),
        (b"pivot-v2", b"meta-v2"),
    }


def test_manifest_tampering_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish_staged(
        "csi300", _stage(tmp_path, pivot=b"pivot-v1", metadata=b"meta-v1")
    )
    active = store.load("csi300")
    assert active is not None
    active.artifacts["metadata"].write_bytes(b"tampered")

    with pytest.raises(GenerationManifestError, match="integrity"):
        store.load("csi300")
