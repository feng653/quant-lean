"""v0.3.0 契约锁定层：注册 --update-snapshots 逃生口选项。

行为不变的重构禁止运行 --update-snapshots；有意变更端点/响应结构时，
运行 scripts/update_contract_snapshots.py 并在 PR 描述中附变更清单。
"""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="重新生成 backend/tests/snapshots/ 下的契约快照（有意变更时使用）",
    )
