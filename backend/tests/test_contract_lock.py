"""v0.3.0 契约锁定层 CI 门禁。

机器保证"端点不出大问题"：
1. OpenAPI 全量 golden（锁路径/方法/参数/状态码/response_model）
2. 全部端点响应 golden（隔离测试库探测 + 规范化后锁定结构）
3. 零覆盖端点可达性冒烟（状态码 + data 包装存在）

逃生口：`--update-snapshots` 重新生成 backend/tests/snapshots/ 下的 golden 文件。
仅允许在"有意变更端点/响应结构"的 PR 使用；行为不变的重构必须保持零 diff。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contract_snapshot import (  # noqa: E402
    ENDPOINTS_GOLDEN,
    OPENAPI_GOLDEN,
    ZERO_COVERAGE_GOLDEN,
    build_isolated_client,
    compute_zero_coverage,
    load_json,
    normalize_result,
    probe_all,
    register_admin,
    save_json,
)
from backend.main import app as test_app  # noqa: E402

_UPDATED = "--update-snapshots"


def _update_mode(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption(_UPDATED))


def _dump_openapi() -> str:
    return json.dumps(
        test_app.openapi(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


# ── 隔离探测 fixture（模块级，只探测一次） ───────────────────────────────


@pytest.fixture(scope="module")
def isolated_probe(tmp_path_factory) -> dict[str, object]:
    """Boot the app against isolated temp state and probe every endpoint once.

    返回 {"results": ..., "client": ..., "patcher": ...}；模块结束后自动撤销 patch。
    """
    runtime_root = tmp_path_factory.mktemp("contract-lock")
    client, patcher = build_isolated_client(runtime_root)
    results: dict[str, object] = {}
    try:
        with client:
            headers = register_admin(client)
            results["results"] = probe_all(client, headers)
    finally:
        patcher.undo()
    return results


# ══════════════════════════════════════════════════════════════════════════
# 1. OpenAPI 全量 golden
# ══════════════════════════════════════════════════════════════════════════


def test_openapi_contract_snapshot(request: pytest.FixtureRequest) -> None:
    current = _dump_openapi()
    if _update_mode(request):
        save_json(OPENAPI_GOLDEN, json.loads(current))
        return
    if not OPENAPI_GOLDEN.exists():
        save_json(OPENAPI_GOLDEN, json.loads(current))
        pytest.fail(
            f"OpenAPI golden 不存在，已生成 {OPENAPI_GOLDEN}；请确认是有意变更后重跑"
        )
    golden = OPENAPI_GOLDEN.read_text(encoding="utf-8")
    if current != golden:
        import difflib

        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=str(OPENAPI_GOLDEN),
                tofile="<current openapi.json>",
                n=2,
            )
        )
        pytest.fail(
            f"OpenAPI 契约漂移：{OPENAPI_GOLDEN}\n"
            f"（若为有意变更，请运行 scripts/update_contract_snapshots.py 并附变更清单）\n\n{diff[:4000]}"
        )


# ══════════════════════════════════════════════════════════════════════════
# 2. 全部端点响应 golden
# ══════════════════════════════════════════════════════════════════════════


def test_endpoint_response_snapshots(
    request: pytest.FixtureRequest,
    isolated_probe: dict[str, object],
) -> None:
    current = {
        key: normalize_result(key, result)
        for key, result in isolated_probe["results"].items()
    }
    if _update_mode(request):
        save_json(ENDPOINTS_GOLDEN, current)
        return
    if not ENDPOINTS_GOLDEN.exists():
        save_json(ENDPOINTS_GOLDEN, current)
        pytest.fail(
            f"端点响应 golden 不存在，已生成 {ENDPOINTS_GOLDEN}；请确认是有意变更后重跑"
        )
    golden = load_json(ENDPOINTS_GOLDEN)
    if golden != current:
        changed = sorted(
            key
            for key in set(golden) | set(current)
            if golden.get(key) != current.get(key)
        )
        detail_lines = []
        for key in changed[:40]:
            old = golden.get(key, "<missing>")
            new = current.get(key, "<missing>")
            detail_lines.append(f"  {key}\n    golden: {json.dumps(old, ensure_ascii=False)[:300]}\n    current: {json.dumps(new, ensure_ascii=False)[:300]}")
        pytest.fail(
            f"端点响应契约漂移（{len(changed)} 个端点）：\n"
            + "\n".join(detail_lines)
            + "\n（若为有意变更，请运行 scripts/update_contract_snapshots.py 并附变更清单）"
        )


# ══════════════════════════════════════════════════════════════════════════
# 3. 零覆盖端点可达性冒烟
# ══════════════════════════════════════════════════════════════════════════


def test_zero_coverage_manifest_matches_golden(
    request: pytest.FixtureRequest,
) -> None:
    """零覆盖清单是快照的一部分：测试覆盖变化同样视为契约变更。"""
    current = compute_zero_coverage()
    if _update_mode(request):
        save_json(ZERO_COVERAGE_GOLDEN, current)
        return
    if not ZERO_COVERAGE_GOLDEN.exists():
        save_json(ZERO_COVERAGE_GOLDEN, current)
        pytest.fail(
            f"零覆盖清单 golden 不存在，已生成 {ZERO_COVERAGE_GOLDEN}；请确认后重跑"
        )
    golden = load_json(ZERO_COVERAGE_GOLDEN)
    if golden != current:
        pytest.fail(
            f"零覆盖端点清单漂移：\n"
            f"  golden ({len(golden)}): {golden}\n"
            f"  current ({len(current)}): {current}\n"
            f"（测试覆盖变化同样属于契约变更，请运行 scripts/update_contract_snapshots.py）"
        )


def test_zero_coverage_endpoints_reachable(
    isolated_probe: dict[str, object],
) -> None:
    """每个零覆盖端点必须可达：路由存在（非 404 route-miss）、非 500、
    2xx 响应带 data 包装。"""
    zero_coverage = compute_zero_coverage()
    results = isolated_probe["results"]
    failures: list[str] = []
    for key in zero_coverage:
        result = results.get(key)
        if result is None:
            failures.append(f"{key}: 未在探测结果中")
            continue
        status = result.get("status")
        body = result.get("body") or {}
        if status == 404 and body == {"detail": "Not Found"}:
            failures.append(f"{key}: 路由不存在（route miss 404）")
            continue
        if status is None or status >= 500:
            failures.append(f"{key}: 不可达（status={status}）")
            continue
        if 200 <= status < 300 and "data" not in body:
            failures.append(f"{key}: 2xx 响应缺少 data 包装（status={status}）")
    assert not failures, "零覆盖端点可达性冒烟失败：\n" + "\n".join(failures)
