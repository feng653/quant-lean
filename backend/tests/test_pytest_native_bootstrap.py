"""Regression tests for platform-specific native runtime initialization."""

from __future__ import annotations

from types import ModuleType

import pytest

from conftest import _preload_apple_silicon_test_runtime


def test_apple_silicon_pytest_preloads_torch_first() -> None:
    events: list[str] = []
    torch_module = ModuleType("torch")

    def importer(name: str) -> ModuleType:
        events.append(name)
        return torch_module

    loaded = _preload_apple_silicon_test_runtime(
        platform_name="darwin",
        machine="arm64",
        importer=importer,
    )

    assert loaded is torch_module
    assert events == ["torch"]


@pytest.mark.parametrize(
    ("platform_name", "machine"),
    [
        ("win32", "AMD64"),
        ("linux", "x86_64"),
        ("darwin", "x86_64"),
    ],
)
def test_native_test_preload_is_apple_silicon_only(
    platform_name: str,
    machine: str,
) -> None:
    events: list[str] = []

    def importer(name: str) -> ModuleType:
        events.append(name)
        return ModuleType(name)

    assert (
        _preload_apple_silicon_test_runtime(
            platform_name=platform_name,
            machine=machine,
            importer=importer,
        )
        is None
    )
    assert events == []
