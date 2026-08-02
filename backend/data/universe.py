"""现时/静态股票池辅助工具 —— 行业筛选 + 代码↔行业映射.

预设池配置:
    - csi300:  沪深300 (000300)
    - csi500:  中证500 (000905)
    - csi800:  CSI 800  (000906) = csi300 + csi500
    - csi1000: 中证1000 (000852)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd

from backend.data.lineage import (
    STATIC_UNIVERSE,
    UniverseSnapshot,
    build_universe_snapshot,
)

logger = logging.getLogger("quant_platform.data.universe")

# ═══════════════════════════════════════════════════════════════════════════════
# 预设股票池
# ═══════════════════════════════════════════════════════════════════════════════

PRESET_POOLS: dict[str, dict[str, Any]] = {
    "csi300": {
        "name": "沪深300",
        "index_code": "000300",
        "description": "沪深300指数成分股",
        "expected_count": 300,
    },
    "csi500": {
        "name": "中证500",
        "index_code": "000905",
        "description": "中证500指数成分股",
        "expected_count": 500,
    },
    "csi800": {
        "name": "CSI 800",
        "index_code": "000906",
        "description": "中证800指数成分股（沪深300+中证500）",
        "expected_count": 800,
    },
    "csi1000": {
        "name": "中证1000",
        "index_code": "000852",
        "description": "中证1000指数成分股",
        "expected_count": 1000,
    },
    "all_a": {
        "name": "全部A股",
        "index_code": None,
        "description": "剔除ST、新股的全部A股",
        "expected_count": None,
    },
}

# ── 前端 → 内部 pool ID 映射 ────────────────────────────────────────────────
POOL_NAME_ALIASES: dict[str, str] = {
    "hs300": "csi300",
    "zz500": "csi500",
    "zz800": "csi800",
    "zz1000": "csi1000",
}

# ── 缓存文件路径（相对于 DATA_CACHE_DIR）────────────────────────────────────
_POOL_CACHE = "pool_{pool_id}.json"
_INDUSTRY_MAP_CACHE = "industry_map.json"
INDUSTRY_MAP_SCHEMA = "industry-map/v3"
INDUSTRY_CLASSIFICATION = "cninfo_008001"
INDUSTRY_SOURCE = "akshare:cninfo"
MIN_INDUSTRY_MAP_COVERAGE = 0.95
INDUSTRY_CACHE_TTL_DAYS = 7
_INDUSTRY_CODE_PATTERN = re.compile(
    r"^(?P<code>\d{6})(?:\.(?:SH|SZ|BJ))?$",
    re.IGNORECASE,
)


class IndustryClassificationUnavailableError(RuntimeError):
    """Industry filtering is unsafe because classification evidence is absent."""


def _industry_map_hash(mapping: dict[str, str]) -> str:
    canonical = json.dumps(
        dict(sorted(mapping.items())),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_pool_id(pool_id: str) -> str:
    if not isinstance(pool_id, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", pool_id):
        raise ValueError("Invalid pool_id")
    return pool_id


def normalize_industry_codes(
    codes: list[str] | tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Return canonical six-digit industry lookup codes and invalid inputs.

    ``.SH``/``.SZ``/``.BJ`` suffixes are an unambiguous presentation detail
    and may be stripped.  Every other shape fails closed instead of being
    guessed or silently excluded from the requested coverage denominator.
    """

    normalized: set[str] = set()
    invalid: set[str] = set()
    for raw_code in codes:
        code = str(raw_code).strip()
        match = _INDUSTRY_CODE_PATTERN.fullmatch(code)
        if match is None:
            invalid.add(code or "<empty>")
            continue
        normalized.add(match.group("code"))
    return sorted(normalized), sorted(invalid)


class UniverseManager:
    """现时/静态股票池管理器。

    This class is intentionally *not* a PIT resolver.  Preset constituents
    come from a current provider and custom pools come from static local
    cache, so either result is exploratory-only.  Governed CSI API display,
    experiment and simulation paths must use ``PointInTimeMasterStore``.

    职责:
    - 根据 pool_id 获取现时/静态代码列表（预设池走 AKShare，自定义池走缓存）
    - 行业筛选
    - 代码→行业映射（延迟构建 + 缓存）
    """

    def __init__(self, source, cache) -> None:
        """初始化股票池管理器。

        Args:
            source: DataSource 实例（用于获取成分股/行业数据）。
            cache:  DataCache 实例（用于读写缓存文件）。
        """
        self._source = source
        self._cache = cache
        self._industry_map: dict[str, str] | None = None  # code → industry_name

    # ── 股票池 ───────────────────────────────────────────────────────────────

    async def get_pool_codes(self, pool_id: str, date: str | None = None) -> list[str]:
        """获取股票池代码列表。

        Args:
            pool_id: 池标识（预设池 ID 或自定义池名称）。
            date:    目标日期（YYYY-MM-DD）。仅用于日志/元数据，不保证历史成分。

        Returns:
            股票代码列表（去重、排序）。
        """
        snapshot = await self.get_pool_snapshot(
            pool_id,
            date,
            include_industry_quality=False,
        )
        return list(snapshot.codes)

    async def get_pool_snapshot(
        self,
        pool_id: str,
        date: str | None = None,
        *,
        include_industry_quality: bool = True,
    ) -> UniverseSnapshot:
        """Resolve membership with explicit source date and PIT limitations.

        AKShare's preset-index endpoint returns current constituents even when
        ``date`` is provided. Custom pools are user-maintained static snapshots,
        not historical index membership.
        """
        pool_id = _validate_pool_id(pool_id)
        normalized_pool = pool_id.lower()
        preset = PRESET_POOLS.get(normalized_pool)
        if preset is not None:
            record = await self._fetch_preset_record(normalized_pool, preset)
            expected_count = preset.get("expected_count")
            extra_risks: tuple[str, ...] = ()
        else:
            record = self._load_pool_cache_record(pool_id)
            expected_count = None
            extra_risks = (STATIC_UNIVERSE,)
            if not record["codes"]:
                logger.warning("Unknown pool '%s' — returning empty snapshot", pool_id)

        industry_map = None
        if include_industry_quality and record["codes"]:
            industry_map = await self.get_industry_map()

        return build_universe_snapshot(
            normalized_pool if preset is not None else pool_id,
            record["codes"],
            requested_as_of=date,
            source_as_of=record.get("source_as_of"),
            point_in_time=False,
            source_requested_count=record.get("count"),
            expected_count=expected_count,
            industry_map=industry_map,
            risk_warnings=extra_risks,
        )

    async def get_pool_info(
        self,
        pool_id: str,
        date: str | None = None,
    ) -> dict:
        """获取池的汇总信息。

        Returns:
            {"pool_id", "name", "description", "index_code", "n_stocks",
             "industries": [{"industry": ..., "count": ..., "pct": ...}, ...]}
        """
        pool_id = _validate_pool_id(pool_id)
        pool_id_lower = pool_id.lower()
        preset = PRESET_POOLS.get(pool_id_lower, {})

        snapshot = await self.get_pool_snapshot(pool_id, date)
        codes = list(snapshot.codes)

        info: dict = {
            "pool_id": pool_id,
            "name": preset.get("name", pool_id),
            "description": preset.get("description", ""),
            "index_code": preset.get("index_code"),
            "n_stocks": len(codes),
            "industries": [],
            "lineage": {
                "schema_version": snapshot.schema_version,
                "requested_as_of": snapshot.requested_as_of,
                "source_as_of": snapshot.source_as_of,
                "point_in_time": snapshot.point_in_time,
                "snapshot_hash": snapshot.snapshot_hash,
            },
            "quality": snapshot.quality.to_dict(),
            "risk_warnings": list(snapshot.risk_warnings),
        }

        # 行业分布
        if codes:
            industry_map = await self.get_industry_map()
            industry_counts: dict[str, int] = {}
            for code in codes:
                ind = industry_map.get(code, "未知")
                industry_counts[ind] = industry_counts.get(ind, 0) + 1

            total = len(codes) or 1
            sorted_industries = sorted(industry_counts.items(), key=lambda x: x[1], reverse=True)
            info["industries"] = [
                {"industry": name, "count": cnt, "pct": round(cnt / total * 100, 1)}
                for name, cnt in sorted_industries[:20]  # top 20
            ]

        return info

    async def pool_exists(self, pool_id: str) -> bool:
        """检查股票池是否存在。"""
        pool_id = _validate_pool_id(pool_id)
        if pool_id.lower() in PRESET_POOLS:
            return True
        codes = self._load_pool_cache(pool_id)
        return bool(codes)

    # ── 行业筛选 ─────────────────────────────────────────────────────────────

    async def filter_by_industry(self, codes: list[str], industries: list[str]) -> list[str]:
        """按行业筛选股票代码。

        Args:
            codes:      股票代码列表。
            industries: 目标行业名称列表。

        Returns:
            属于指定行业的股票代码子集。
        """
        if not industries:
            return codes
        if not codes:
            return []

        normalized_codes, invalid_codes = normalize_industry_codes(codes)
        if invalid_codes:
            raise IndustryClassificationUnavailableError(
                "industry_scope_invalid_codes:" + ",".join(invalid_codes[:20])
            )
        await self.get_industry_readiness(normalized_codes)
        industry_map = await self.get_industry_map(strict=True)
        mapped = sum(code in industry_map for code in normalized_codes)
        coverage = mapped / len(normalized_codes)
        if coverage < MIN_INDUSTRY_MAP_COVERAGE:
            raise IndustryClassificationUnavailableError(
                "industry_map_coverage_insufficient:"
                f"{mapped}/{len(normalized_codes)}={coverage:.4f}"
            )
        industry_set = {str(industry).strip() for industry in industries if str(industry).strip()}

        result = [
            code
            for code in codes
            if (
                (match := _INDUSTRY_CODE_PATTERN.fullmatch(str(code).strip()))
                and industry_map.get(match.group("code"), "") in industry_set
            )
        ]
        return result

    async def get_industry_map(
        self,
        *,
        strict: bool = False,
        refresh: bool = False,
    ) -> dict[str, str]:
        """获取代码 → 行业名称的映射。

        首次调用时从数据源构建并缓存，后续直接返回缓存。

        Returns:
            {股票代码: 行业名称} 字典。
        """
        if self._industry_map is not None:
            if strict and not self._industry_map:
                raise IndustryClassificationUnavailableError(
                    "industry_map_empty_or_source_unavailable"
                )
            return self._industry_map

        # 1. 尝试本地缓存
        cached = self._load_industry_map_cache()
        if cached:
            self._industry_map = cached
            return cached

        # 2. 外部拉取必须由显式刷新路径触发，读取接口不得隐式联网或写缓存。
        if refresh:
            logger.info("Building industry map from source (this may take a while)...")
            self._industry_map = await self._build_industry_map()
            if self._industry_map:
                self._save_industry_map_cache(self._industry_map)

        if strict and not self._industry_map:
            raise IndustryClassificationUnavailableError("industry_map_empty_or_source_unavailable")
        return self._industry_map or {}

    async def get_industry_readiness(
        self,
        codes: list[str] | None = None,
        *,
        refresh_missing: bool = False,
    ) -> dict[str, Any]:
        requested, invalid_requested = normalize_industry_codes(codes or [])
        mapping = await self.get_industry_map(refresh=refresh_missing)
        missing = [code for code in requested if code not in mapping]
        fetch_map = getattr(self._source, "fetch_industry_map", None)
        if refresh_missing and missing and callable(fetch_map):
            fetched = await fetch_map(missing)
            if fetched:
                mapping.update(fetched)
                self._industry_map = mapping
                self._save_industry_map_cache(mapping)
        mapped = sum(code in mapping for code in requested)
        coverage = mapped / len(requested) if requested else None
        filterable = (
            not invalid_requested
            and bool(mapping)
            and bool(requested)
            and (coverage is not None and coverage >= MIN_INDUSTRY_MAP_COVERAGE)
        )
        reason = None
        if invalid_requested:
            reason = "industry_scope_invalid_codes"
        elif not mapping:
            reason = "industry_map_empty_or_source_unavailable"
        elif not requested:
            reason = "coverage_not_evaluated"
        elif requested and not filterable:
            reason = "industry_map_coverage_insufficient"
        return {
            "filterable": filterable,
            "reason": reason,
            "source": INDUSTRY_SOURCE,
            "classification": INDUSTRY_CLASSIFICATION,
            "mapped_stocks": len(mapping),
            "requested_stocks": len(requested),
            "requested_mapped_stocks": mapped,
            "invalid_requested_codes": invalid_requested,
            "map_coverage": coverage,
            "coverage_scope": ("requested_codes" if requested else "not_evaluated"),
            "minimum_coverage": MIN_INDUSTRY_MAP_COVERAGE,
        }

    # ── 内部实现 ─────────────────────────────────────────────────────────────

    async def _fetch_preset_codes(self, pool_id: str, preset: dict) -> list[str]:
        """Compatibility wrapper returning normalized current membership."""
        record = await self._fetch_preset_record(pool_id, preset)
        return sorted(
            {
                str(code).strip()
                for code in record["codes"]
                if code is not None and str(code).strip()
            }
        )

    async def _fetch_preset_record(
        self,
        pool_id: str,
        preset: dict[str, Any],
    ) -> dict[str, Any]:
        """Get raw current membership and its cache/source timestamp."""
        index_code = preset.get("index_code")
        if index_code is None:
            # "all_a" — 需通过完整数据覆盖获取
            return self._load_pool_cache_record(pool_id)

        # 尝试从缓存加载
        cached = self._load_pool_cache_record(pool_id)
        if cached["codes"]:
            return cached

        # 从 AKShare 获取
        try:
            codes = await self._source.fetch_index_components(index_code)
            if codes:
                self._save_pool_cache(pool_id, codes)
                logger.info(
                    "Fetched %d components for %s (%s)",
                    len(codes),
                    preset["name"],
                    index_code,
                )
                return self._load_pool_cache_record(pool_id)
        except Exception:
            logger.exception(
                "Failed to fetch index components for %s (%s)",
                preset["name"],
                index_code,
            )

        return self._empty_pool_record(pool_id)

    async def _build_industry_map(self) -> dict[str, str]:
        """通过支持的行业成分端点构建代码→行业映射。

        注意: 兼容端点可能发起较多 API 请求；CNInfo 映射通过显式代码集合获取。
        """
        import asyncio

        try:
            industries = await self._source.fetch_industry_list()
        except Exception:
            logger.exception("Failed to fetch industry list")
            return {}

        if not industries:
            return {}

        fetch_components = getattr(
            self._source,
            "fetch_industry_components",
            None,
        )
        if not callable(fetch_components):
            # CNInfo resolves membership safely for an explicit requested pool
            # via fetch_industry_map; a global all-A snapshot is not inferred.
            return {}

        semaphore = asyncio.Semaphore(5)  # 并发限制

        async def _fetch_industry_stocks(
            ind: dict,
        ) -> tuple[str, list[str]]:
            async with semaphore:
                try:
                    stock_codes = await fetch_components(ind["name"])
                except Exception:
                    logger.debug("Failed to fetch stocks for industry '%s'", ind["name"])
                    return ind["name"], []

                if not stock_codes:
                    return ind["name"], []
                return ind["name"], list(stock_codes)

        results = await asyncio.gather(*[_fetch_industry_stocks(ind) for ind in industries])
        code_to_industry: dict[str, str] = {}
        conflicted_codes: set[str] = set()
        for industry_name, stock_codes in sorted(results):
            for stock_code in stock_codes:
                if stock_code in conflicted_codes:
                    continue
                existing = code_to_industry.get(stock_code)
                if existing is not None and existing != industry_name:
                    logger.error(
                        "Industry classification conflict for %s: %s vs %s",
                        stock_code,
                        existing,
                        industry_name,
                    )
                    code_to_industry.pop(stock_code, None)
                    conflicted_codes.add(stock_code)
                    continue
                code_to_industry[stock_code] = industry_name

        logger.info(
            "Built industry map: %d stocks in %d industries; %d conflicts excluded",
            len(code_to_industry),
            len(industries),
            len(conflicted_codes),
        )
        return code_to_industry

    # ── 缓存读写 ─────────────────────────────────────────────────────────────

    def _pool_cache_path(self, pool_id: str) -> str:
        """获取池缓存的绝对路径。"""
        from backend.config import settings

        pool_id = _validate_pool_id(pool_id)
        cache_dir = settings.abs_path(settings.DATA_CACHE_DIR)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return str(cache_dir / _POOL_CACHE.format(pool_id=pool_id))

    def _load_pool_cache(self, pool_id: str) -> list[str]:
        """Load raw cached codes, preserving duplicates for quality analysis."""
        return list(self._load_pool_cache_record(pool_id)["codes"])

    @staticmethod
    def _empty_pool_record(pool_id: str) -> dict[str, Any]:
        return {
            "pool_id": pool_id,
            "codes": [],
            "count": 0,
            "unique_count": 0,
            "updated_at": None,
            "source_as_of": None,
        }

    def _load_pool_cache_record(self, pool_id: str) -> dict[str, Any]:
        """Load cache metadata without discarding raw count or duplicates."""
        path = self._pool_cache_path(pool_id)
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            raw_codes = data.get("codes", [])
            if not isinstance(raw_codes, list):
                return self._empty_pool_record(pool_id)
            updated_at = data.get("updated_at")
            source_as_of = data.get("source_as_of")
            if not source_as_of and updated_at:
                try:
                    source_as_of = pd.Timestamp(updated_at).date().isoformat()
                except (TypeError, ValueError):
                    source_as_of = None
            unique_count = len(
                {str(code).strip() for code in raw_codes if code is not None and str(code).strip()}
            )
            return {
                "pool_id": str(data.get("pool_id") or pool_id),
                "codes": raw_codes,
                "count": int(data.get("count", len(raw_codes))),
                "unique_count": int(data.get("unique_count", unique_count)),
                "updated_at": updated_at,
                "source_as_of": source_as_of,
            }
        except Exception:
            return self._empty_pool_record(pool_id)

    def _save_pool_cache(self, pool_id: str, codes: list[str]) -> None:
        """Save raw membership before normalization so quality loss is visible."""
        path = self._pool_cache_path(pool_id)
        raw_codes = [None if code is None else str(code).strip() for code in codes]
        unique_count = len({code for code in raw_codes if code})
        updated_at = pd.Timestamp.now()
        data = {
            "pool_id": pool_id,
            "codes": raw_codes,
            "count": len(raw_codes),
            "unique_count": unique_count,
            "updated_at": updated_at.isoformat(),
            "source_as_of": updated_at.date().isoformat(),
        }
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _industry_map_cache_path(self) -> Path:
        """获取行业映射缓存文件路径。"""
        from backend.config import settings

        cache_dir = settings.abs_path(settings.DATA_CACHE_DIR)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / _INDUSTRY_MAP_CACHE

    def _load_industry_map_cache(self) -> dict[str, str] | None:
        """从缓存加载行业映射。"""
        path = self._industry_map_cache_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                data.get("schema_version") != INDUSTRY_MAP_SCHEMA
                or data.get("classification") != INDUSTRY_CLASSIFICATION
                or data.get("source") != INDUSTRY_SOURCE
                or not data.get("filterable")
            ):
                return None
            updated_at = pd.Timestamp(data.get("updated_at"))
            if pd.isna(updated_at):
                return None
            now = pd.Timestamp.now(tz="UTC")
            if updated_at.tzinfo is None:
                updated_at = updated_at.tz_localize("UTC")
            else:
                updated_at = updated_at.tz_convert("UTC")
            if updated_at > now or now - updated_at > pd.Timedelta(days=INDUSTRY_CACHE_TTL_DAYS):
                return None
            mapping = data.get("map")
            if not isinstance(mapping, dict) or not mapping:
                return None
            if any(
                not str(code).strip() or not str(industry).strip()
                for code, industry in mapping.items()
            ):
                return None
            normalized = {
                str(code).strip(): str(industry).strip() for code, industry in mapping.items()
            }
            if data.get("content_sha256") != _industry_map_hash(normalized):
                return None
            return normalized
        except Exception:
            return None

    def _save_industry_map_cache(self, mapping: dict[str, str]) -> None:
        """保存行业映射到缓存。"""
        path = self._industry_map_cache_path()
        data = {
            "schema_version": INDUSTRY_MAP_SCHEMA,
            "classification": INDUSTRY_CLASSIFICATION,
            "source": INDUSTRY_SOURCE,
            "filterable": bool(mapping),
            "map": mapping,
            "n_stocks": len(mapping),
            "content_sha256": _industry_map_hash(mapping),
            "updated_at": pd.Timestamp.now().isoformat(),
        }
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
