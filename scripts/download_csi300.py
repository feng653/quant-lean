"""Download the project CSI 300 component snapshot as an OHLCV panel."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import settings  # noqa: E402
from backend.data.cache import DataCache  # noqa: E402
from backend.data.sources.akshare_source import AKShareSource  # noqa: E402


async def download() -> None:
    component_path = (
        settings.abs_path(settings.DATA_CACHE_DIR) / "pool_csi300.json"
    )
    payload = json.loads(component_path.read_text(encoding="utf-8"))
    codes = sorted({str(code) for code in payload["codes"]})
    panel = await AKShareSource(preferred_provider="sina").fetch_daily(
        codes,
        "2019-01-02",
        "2026-07-24",
    )
    if panel.empty:
        raise RuntimeError("未获取到 CSI 300 OHLCV 行情")
    await DataCache().save_pivot("csi300", panel)
    print(
        f"saved csi300: {len(codes)} members, "
        f"{len(panel)} dates, {len(panel.columns)} columns"
    )


if __name__ == "__main__":
    asyncio.run(download())
