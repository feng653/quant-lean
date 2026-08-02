"""Build the project's 429-member CSI 500 OHLCV research cache.

The component snapshot in ``data/cache/pool_csi500.json`` is authoritative for
this project.  Downloads are checkpointed under a separate cache key and the
production ``csi500`` cache is replaced only after validation succeeds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import settings  # noqa: E402
from backend.data.cache import DataCache, has_price_field  # noqa: E402
from backend.data.sources.akshare_source import AKShareSource  # noqa: E402

FIELDS = ("open", "close", "high", "low", "volume", "amount")


def _merge(left: pd.DataFrame | None, right: pd.DataFrame) -> pd.DataFrame:
    if left is None or left.empty:
        merged = right.copy()
    elif right.empty:
        merged = left.copy()
    else:
        merged = left.combine_first(right)
        merged.update(right)
    merged.sort_index(inplace=True)
    merged.sort_index(axis=1, inplace=True)
    return merged


async def build(start: str, end: str, attempts: int) -> None:
    component_path = (
        settings.abs_path(settings.DATA_CACHE_DIR) / "pool_csi500.json"
    )
    payload = json.loads(component_path.read_text(encoding="utf-8"))
    codes = sorted({str(code).strip() for code in payload["codes"] if str(code).strip()})
    if len(codes) != 429:
        raise RuntimeError(f"项目中证500成员应为429只，实际为 {len(codes)}")

    cache = DataCache()
    source = AKShareSource(preferred_provider="sina")
    checkpoint_key = "csi500_ohlcv_build"
    panel = await cache.load_pivot(checkpoint_key)
    if panel is not None and not has_price_field(panel, "open"):
        panel = None

    for attempt in range(1, attempts + 1):
        ready_codes = (
            {
                str(code)
                for code in panel.columns.get_level_values(0)
                if panel[(code, "open")].notna().any()
            }
            if panel is not None and isinstance(panel.columns, pd.MultiIndex)
            else set()
        )
        missing = [code for code in codes if code not in ready_codes]
        if not missing:
            break
        print(
            f"download pass {attempt}/{attempts}: "
            f"{len(ready_codes)} ready, {len(missing)} missing",
            flush=True,
        )
        fetched = await source.fetch_daily(missing, start, end)
        panel = _merge(panel, fetched)
        if panel is not None and not panel.empty:
            await cache.save_pivot(checkpoint_key, panel)

    if panel is None or panel.empty:
        raise RuntimeError("未获取到任何中证500行情")
    if not has_price_field(panel, "open") or not has_price_field(panel, "close"):
        raise RuntimeError("行情缺少 open/close，不能执行 T+1 回测")

    complete_columns = pd.MultiIndex.from_product(
        [codes, FIELDS],
        names=["code", "field"],
    )
    panel = panel.reindex(columns=complete_columns)
    ready_codes = [
        code
        for code in codes
        if panel[(code, "open")].notna().any()
        and panel[(code, "close")].notna().any()
    ]
    missing = sorted(set(codes) - set(ready_codes))
    if len(ready_codes) < 420:
        raise RuntimeError(
            f"有效行情仅覆盖 {len(ready_codes)}/429，只保留检查点，不发布正式缓存"
        )
    await cache.save_pivot("csi500", panel)
    report = {
        "pool_id": "csi500",
        "members": len(codes),
        "members_with_ohlcv": len(ready_codes),
        "members_without_ohlcv": missing,
        "date_start": str(panel.index.min().date()),
        "date_end": str(panel.index.max().date()),
        "fields": list(FIELDS),
    }
    report_path = settings.abs_path(settings.DATA_CACHE_DIR) / "csi500_ohlcv_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2019-01-02")
    parser.add_argument("--end", default="2026-07-24")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(build(args.start, args.end, args.attempts))


if __name__ == "__main__":
    main()
