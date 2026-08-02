"""AKShare 数据源实现 —— 通过 akshare 库获取 A 股行情、成分股、交易日历和行业分类.

关键 AKShare 函数:
    - stock_zh_a_hist(symbol, period, start_date, end_date, adjust=...)
    - stock_zh_a_hist_tx(symbol, start_date, end_date, adjust=...)
    - index_stock_cons(symbol)
    - tool_trade_date_hist_sina()
    - stock_industry_category_cninfo(symbol="证监会行业分类标准")
    - stock_industry_change_cninfo(symbol)
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import threading
from typing import Any, Awaitable, Callable

import pandas as pd

from backend.data.source_validation import (
    DailyFetchResult,
    build_daily_fetch_evidence,
)

from .base import DataSource

logger = logging.getLogger("quant_platform.data.akshare")
AKShareProgress = Callable[[dict[str, Any]], Awaitable[None]]

# ── 并发控制 ─────────────────────────────────────────────────────────────────
# mini_racer is used by some AKShare endpoints and can terminate the entire
# interpreter when two native calls initialise or execute concurrently.  A
# process-wide lock protects callers from different source instances and jobs;
# the per-request loop below is also deliberately sequential so it does not
# fill the default executor with threads waiting on this lock.
_AKSHARE_NATIVE_LOCK = threading.Lock()

# ── 重试配置 ─────────────────────────────────────────────────────────────────
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.5  # 指数退避基数（秒）

_SH_INDEX_CODES = {"000300", "000905", "000906", "000852"}
_DAILY_PROVIDERS = {"eastmoney", "sina", "tencent"}
_DAILY_ENDPOINTS = {
    "eastmoney": "akshare.stock_zh_a_hist/eastmoney",
    "sina": "akshare.stock_zh_a_daily/sina",
    "tencent": "akshare.stock_zh_a_hist_tx/tencent",
}


class AKShareCallError(RuntimeError):
    """An AKShare call exhausted its retry budget."""

    def __init__(self, function_name: str, attempts: int, last_error: Exception):
        self.function_name = function_name
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"AKShare call {function_name} failed after {attempts} retries. "
            f"Last error: {last_error}"
        )


class ProviderOutageError(RuntimeError):
    """A provider transport path is unavailable and its circuit is open."""


_TRANSPORT_ERROR_NAMES = {
    "connecttimeout",
    "connectionerror",
    "maxretryerror",
    "newconnectionerror",
    "protocolerror",
    "proxyerror",
    "readtimeout",
    "remotedisconnected",
    "sslcertverificationerror",
    "ssleoferror",
    "sslerror",
    "timeout",
}
_TRANSPORT_ERROR_TEXT = (
    "connection aborted",
    "connection refused",
    "connection reset",
    "empty reply from server",
    "failed to establish a new connection",
    "proxy error",
    "remote end closed connection",
    "timed out",
    "unable to connect",
)


def _is_transport_failure(exc: BaseException) -> bool:
    """Recognise provider/network outages without treating empty symbols as one."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__.lower()
        message = str(current).lower()
        if (
            isinstance(current, (ConnectionError, TimeoutError, ssl.SSLError))
            or name in _TRANSPORT_ERROR_NAMES
            or any(marker in message for marker in _TRANSPORT_ERROR_TEXT)
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _to_compact_date(date_str: str) -> str:
    """将 "YYYY-MM-DD" 转为 AKShare 使用的 "YYYYMMDD" 格式."""
    return date_str.replace("-", "")


def _from_compact_date(compact: str) -> str:
    """将 "YYYYMMDD" 转为 "YYYY-MM-DD"."""
    if len(compact) == 8:
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    return compact


def _empty_close_series() -> pd.Series:
    return pd.Series(
        index=pd.DatetimeIndex([], name="date"),
        dtype="float64",
        name="close",
    )


def _parse_eastmoney_industries(df: pd.DataFrame) -> list[dict[str, str]]:
    """Resolve industry columns by exact semantics and reject code-as-name."""

    aliases = {str(column).strip().lower(): column for column in df.columns}
    code_col = next(
        (
            aliases[name]
            for name in ("板块代码", "代码", "board_code", "code")
            if name in aliases
        ),
        None,
    )
    name_col = next(
        (
            aliases[name]
            for name in ("板块名称", "名称", "board_name", "name")
            if name in aliases
        ),
        None,
    )
    if code_col is None or name_col is None or code_col == name_col:
        raise ValueError(
            f"EastMoney industry columns are unsupported: {list(df.columns)}"
        )

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        name = str(row[name_col]).strip()
        if not re.fullmatch(r"BK\d{4,}", code, flags=re.IGNORECASE):
            raise ValueError(f"invalid EastMoney industry code: {code!r}")
        if (
            not name
            or name == code
            or re.fullmatch(r"BK\d{4,}", name, flags=re.IGNORECASE)
        ):
            raise ValueError(
                f"invalid EastMoney industry name for {code}: {name!r}"
            )
        if code in seen:
            raise ValueError(f"duplicate EastMoney industry code: {code}")
        seen.add(code)
        result.append({"code": code, "name": name})
    if not result:
        raise ValueError("EastMoney industry list is empty")
    return result


async def _run_sync(func, *args, **kwargs) -> Any:
    """在线程池中串行执行同步阻塞的 AKShare 原生调用."""

    def _locked_call() -> Any:
        with _AKSHARE_NATIVE_LOCK:
            return func(*args, **kwargs)

    return await asyncio.to_thread(_locked_call)


async def _retry_call(func, *args, max_retries: int = _MAX_RETRIES, **kwargs) -> Any:
    """带指数退避重试的同步调用包装器.

    Args:
        func:       同步 callable（通常是 akshare 函数）。
        max_retries: 最大重试次数（含首次调用）。
        *args/**kwargs: 传给 func 的参数。

    Returns:
        func 的返回值。

    Raises:
        RuntimeError: 所有重试均失败。
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await _run_sync(func, *args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = _BASE_BACKOFF ** (attempt + 1)
                logger.warning(
                    "AKShare call %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    getattr(func, "__name__", func),
                    attempt + 1,
                    max_retries,
                    wait,
                    exc,
                )
                await asyncio.sleep(wait)
    assert last_exc is not None
    raise AKShareCallError(
        str(getattr(func, "__name__", func)),
        max_retries,
        last_exc,
    )


class AKShareSource(DataSource):
    """基于 AKShare 的 A 股数据源。

    所有 AKShare 调用均在 asyncio 线程池中执行（因为 akshare 是同步库）。
    内置指数退避重试与并发限流。
    """

    def __init__(
        self,
        preferred_provider: str = "eastmoney",
        *,
        price_adjustment: str = "qfq",
    ) -> None:
        if preferred_provider not in _DAILY_PROVIDERS:
            raise ValueError(
                "preferred_provider must be eastmoney, sina or tencent"
            )
        if price_adjustment not in {"raw", "qfq", "hfq"}:
            raise ValueError("price_adjustment must be raw, qfq or hfq")
        self.preferred_provider = preferred_provider
        self.price_adjustment = price_adjustment
        self._daily_outage: ProviderOutageError | None = None

    # ── DataSource 接口实现 ─────────────────────────────────────────────────

    async def fetch_daily(
        self, codes: list[str], start: str, end: str
    ) -> pd.DataFrame:
        return (await self.fetch_daily_result(codes, start, end)).frame

    async def fetch_daily_result(
        self,
        codes: list[str],
        start: str,
        end: str,
    ) -> DailyFetchResult:
        """Fetch one selected provider and retain its request identity."""
        frame = await self._fetch_daily_frame(codes, start, end)
        evidence = build_daily_fetch_evidence(
            frame,
            requested_codes=codes,
            start=start,
            end=end,
            provider=f"akshare:{self.preferred_provider}",
            endpoint=_DAILY_ENDPOINTS[self.preferred_provider],
            adjustment=self.price_adjustment,
            evidence_level="public_aggregator",
        )
        return DailyFetchResult(frame, evidence)

    async def fetch_daily_result_with_progress(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        progress: AKShareProgress | None,
        source_role: str,
    ) -> DailyFetchResult:
        """Fetch one provider while exposing bounded per-code progress."""

        frame = await self._fetch_daily_frame(
            codes,
            start,
            end,
            progress=progress,
            source_role=source_role,
        )
        evidence = build_daily_fetch_evidence(
            frame,
            requested_codes=codes,
            start=start,
            end=end,
            provider=f"akshare:{self.preferred_provider}",
            endpoint=_DAILY_ENDPOINTS[self.preferred_provider],
            adjustment=self.price_adjustment,
            evidence_level="public_aggregator",
        )
        return DailyFetchResult(frame, evidence)

    def staging_identity(self) -> dict[str, str]:
        return {
            "provider": f"akshare:{self.preferred_provider}",
            "endpoint": _DAILY_ENDPOINTS[self.preferred_provider],
            "adjustment": self.price_adjustment,
            "adapter_id": (
                f"quant-platform/akshare-{self.preferred_provider}/v1"
            ),
        }

    async def _fetch_daily_frame(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        progress: AKShareProgress | None = None,
        source_role: str = "primary",
    ) -> pd.DataFrame:
        """Fetch one explicitly declared price basis as an OHLCV panel."""
        if not codes:
            return pd.DataFrame()
        if self._daily_outage is not None:
            raise ProviderOutageError(str(self._daily_outage))

        start_compact = _to_compact_date(start)
        end_compact = _to_compact_date(end)

        async def _fetch_one(code: str) -> pd.DataFrame | None:
            try:
                import akshare as ak

                if code.startswith("689"):
                    logger.error(
                        "Refusing CDR %s: stock_zh_a_cdr_daily exposes no "
                        "a compatible adjustment contract",
                        code,
                    )
                    return None
                elif self.preferred_provider in {"sina", "tencent"}:
                    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
                    function = (
                        ak.stock_zh_a_daily
                        if self.preferred_provider == "sina"
                        else ak.stock_zh_a_hist_tx
                    )
                    df_raw: pd.DataFrame = await _retry_call(
                        function,
                        symbol=f"{prefix}{code}",
                        start_date=start_compact,
                        end_date=end_compact,
                        adjust=(
                            ""
                            if self.price_adjustment == "raw"
                            else self.price_adjustment
                        ),
                    )
                else:
                    df_raw = await _retry_call(
                        ak.stock_zh_a_hist,
                        symbol=code,
                        period="daily",
                        start_date=start_compact,
                        end_date=end_compact,
                        adjust=(
                            ""
                            if self.price_adjustment == "raw"
                            else self.price_adjustment
                        ),
                    )
            except AKShareCallError as exc:
                if _is_transport_failure(exc.last_error):
                    outage = ProviderOutageError(
                        f"akshare:{self.preferred_provider} daily provider "
                        f"outage after {exc.attempts} failed attempts for "
                        f"{code}: {type(exc.last_error).__name__}"
                    )
                    self._daily_outage = outage
                    logger.error("%s; opening provider circuit", outage)
                    raise outage from exc
                logger.exception("Failed to fetch daily data for %s", code)
                return None
            except Exception:
                logger.exception("Failed to fetch daily data for %s", code)
                return None

            if df_raw is None or df_raw.empty:
                logger.debug("No daily data returned for %s [%s, %s]", code, start, end)
                return None

            # AKShare 返回的中文列名
            # ['日期','开盘','收盘','最高','最低','成交量','成交额','振幅','涨跌幅','涨跌额','换手率']
            try:
                df = df_raw.rename(
                    columns={
                        "日期": "date",
                        "收盘": "close",
                        "开盘": "open",
                        "最高": "high",
                        "最低": "low",
                        "成交量": "volume",
                        "成交额": "amount",
                    }
                )
            except Exception:
                # 兼容列名可能已是英文的情况
                df = df_raw.copy()

            if "date" not in df.columns and "日期" in df_raw.columns:
                df["date"] = df_raw["日期"]
            if "close" not in df.columns and "收盘" in df_raw.columns:
                df["close"] = df_raw["收盘"]

            if "date" not in df.columns or "close" not in df.columns or "open" not in df.columns:
                logger.warning(
                    "Unexpected column names for %s: %s", code, list(df_raw.columns)
                )
                return None

            # 统一日期格式
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date", "open", "close"])
            if df.empty:
                return None

            df["code"] = code
            fields = [
                field
                for field in ("open", "close", "high", "low", "volume", "amount")
                if field in df.columns
            ]
            for field in fields:
                df[field] = pd.to_numeric(df[field], errors="coerce")
            df = df[["date", "code", *fields]].dropna(subset=["open", "close"])
            return df

        # Keep native AKShare calls sequential.  mini_racer can abort the
        # process under concurrent access, which cannot be caught as a Python
        # exception and can leave a data-update job without a terminal state.
        results = []
        for position, code in enumerate(codes, start=1):
            results.append(await _fetch_one(code))
            report_step = max(1, len(codes) // 50)
            if progress is not None and (
                position == len(codes) or position % report_step == 0
            ):
                await progress(
                    {
                        "source_role": source_role,
                        "provider": f"akshare:{self.preferred_provider}",
                        "completed_codes": position,
                        "total_codes": len(codes),
                        "reused_staging": False,
                    }
                )

        valid_dfs = [r for r in results if r is not None]
        if not valid_dfs:
            logger.warning(
                "No valid daily data fetched for any of %d codes [%s, %s]",
                len(codes),
                start,
                end,
            )
            return pd.DataFrame()

        combined = pd.concat(valid_dfs, ignore_index=True)
        if combined.empty:
            return combined

        fields = [
            field
            for field in ("open", "close", "high", "low", "volume", "amount")
            if field in combined.columns
        ]
        panel = combined.set_index(["date", "code"])[fields].unstack("code")
        panel.columns = panel.columns.swaplevel(0, 1)
        panel.columns.names = ["code", "field"]
        panel.sort_index(inplace=True)
        panel.sort_index(axis=1, inplace=True)
        return panel

    async def fetch_index_daily(
        self,
        index_code: str,
        start: str,
        end: str,
    ) -> pd.Series:
        """拉取指数日收盘价，归一化为严格裁剪的 ``close`` Series。"""
        import akshare as ak

        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if start_ts > end_ts:
            raise ValueError("start must be on or before end")
        prefix = (
            "sh"
            if index_code in _SH_INDEX_CODES or not index_code.startswith("399")
            else "sz"
        )
        symbol = f"{prefix}{index_code}"
        try:
            df_raw: pd.DataFrame = await _retry_call(
                ak.stock_zh_index_daily,
                symbol=symbol,
            )
        except Exception:
            logger.exception("Failed to fetch index daily data for %s", index_code)
            return _empty_close_series()

        if df_raw is None or df_raw.empty:
            logger.debug(
                "No index daily data returned for %s [%s, %s]",
                index_code,
                start,
                end,
            )
            return _empty_close_series()

        date_col = next(
            (
                candidate
                for candidate in ("date", "日期", "trade_date", "交易日期")
                if candidate in df_raw.columns
            ),
            None,
        )
        close_col = next(
            (
                candidate
                for candidate in ("close", "收盘")
                if candidate in df_raw.columns
            ),
            None,
        )
        if close_col is None or (
            date_col is None and not isinstance(df_raw.index, pd.DatetimeIndex)
        ):
            logger.warning(
                "Unexpected index daily columns for %s: %s",
                index_code,
                list(df_raw.columns),
            )
            return _empty_close_series()

        dates = (
            pd.Series(df_raw.index, index=df_raw.index)
            if date_col is None
            else df_raw[date_col]
        )
        normalized = pd.DataFrame(
            {
                "date": pd.to_datetime(dates.to_numpy(), errors="coerce"),
                "close": pd.to_numeric(
                    df_raw[close_col].to_numpy(),
                    errors="coerce",
                ),
            }
        ).dropna(subset=["date", "close"])
        if normalized.empty:
            return _empty_close_series()

        normalized = normalized[
            (normalized["date"] >= start_ts) & (normalized["date"] <= end_ts)
        ]
        if normalized.empty:
            return _empty_close_series()

        normalized = normalized.drop_duplicates(subset=["date"], keep="last")
        series = normalized.set_index("date")["close"].astype(float).sort_index()
        series.index = pd.DatetimeIndex(series.index, name="date")
        series.name = "close"
        return series

    async def fetch_index_components(
        self, index_code: str, date: str | None = None
    ) -> list[str]:
        """获取指数成分股列表。

        注意: AKShare 仅提供当前成分股，不支持历史成分查询，因此 date 参数会被忽略。
        所有使用此方法的历史回测均受幸存者偏差影响。
        """
        import akshare as ak

        try:
            df_raw: pd.DataFrame = await _retry_call(
                ak.index_stock_cons, symbol=index_code
            )
        except Exception:
            logger.exception("Failed to fetch index components for %s", index_code)
            return []

        if df_raw is None or df_raw.empty:
            return []

        # 列名可能是 '品种代码'、'成分券代码'、'stock_code' 等
        code_col: str | None = None
        for candidate in ("品种代码", "成分券代码", "stock_code", "code", "代码"):
            if candidate in df_raw.columns:
                code_col = candidate
                break

        if code_col is None:
            # 取第一列（通常就是代码列）
            code_col = df_raw.columns[0]
            logger.debug(
                "Guessing '%s' as stock code column for index %s",
                code_col,
                index_code,
            )

        codes: list[str] = df_raw[code_col].astype(str).str.strip().tolist()
        return [c for c in codes if c]

    async def fetch_trading_calendar(
        self, start: str, end: str
    ) -> list[str]:
        """获取 A 股交易日历（新浪来源）。"""
        import akshare as ak

        try:
            df_raw: pd.DataFrame = await _retry_call(
                ak.tool_trade_date_hist_sina
            )
        except Exception:
            logger.exception("Failed to fetch trading calendar")
            return self._fallback_calendar(start, end)

        if df_raw is None or df_raw.empty:
            return self._fallback_calendar(start, end)

        # 列名通常是 'trade_date'
        date_col: str | None = None
        for candidate in ("trade_date", "tradeDate", "日期", "date"):
            if candidate in df_raw.columns:
                date_col = candidate
                break

        if date_col is None:
            date_col = df_raw.columns[0]

        all_dates = pd.to_datetime(
            df_raw[date_col], errors="coerce"
        ).dropna()

        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        mask = (all_dates >= start_ts) & (all_dates <= end_ts)
        filtered = all_dates[mask].sort_values()

        return [d.strftime("%Y-%m-%d") for d in filtered]

    # ── 辅助方法 ────────────────────────────────────────────────────────────

    @staticmethod
    def _fallback_calendar(start: str, end: str) -> list[str]:
        """网络不可用时生成工作日（排除周六日）作为降级日历。"""
        dates = pd.date_range(start, end, freq="B")
        return [d.strftime("%Y-%m-%d") for d in dates]

    async def fetch_industry_list(self) -> list[dict]:
        """获取巨潮 008001 行业分类目录。

        返回格式: [{"code": "J66", "name": "货币金融服务"}, ...]
        """
        import akshare as ak

        try:
            df: pd.DataFrame = await _retry_call(
                ak.stock_industry_category_cninfo,
                symbol="证监会行业分类标准",
            )
        except Exception:
            logger.exception("Failed to fetch industry list via CNInfo")
            return []

        if df is None or df.empty:
            return []
        current = df[
            df["终止日期"].isna()
            & (pd.to_numeric(df["分级"], errors="coerce") == 2)
        ]
        return [
            {"code": str(row["类目编码"]).strip(), "name": str(row["类目名称"]).strip()}
            for _, row in current.iterrows()
            if str(row["类目编码"]).strip() and str(row["类目名称"]).strip()
        ]

    async def fetch_industry_map(self, codes: list[str]) -> dict[str, str]:
        """Resolve current 008001 classification for explicitly requested codes."""

        import akshare as ak

        mapping: dict[str, str] = {}
        requested_codes = sorted(
            {
                str(code).strip()
                for code in codes
                if re.fullmatch(r"\d{6}", str(code).strip())
            }
        )
        for code in requested_codes:
            try:
                frame: pd.DataFrame = await _retry_call(
                    ak.stock_industry_change_cninfo,
                    symbol=code,
                    start_date="19900101",
                    end_date=pd.Timestamp.now().strftime("%Y%m%d"),
                )
            except Exception:
                logger.warning("Failed to fetch CNInfo industry for %s", code)
                continue
            if frame is None or frame.empty:
                continue
            candidates = frame[
                (frame["分类标准编码"].astype(str) == "008001")
                & frame["行业大类"].notna()
            ].sort_values("变更日期")
            if not candidates.empty:
                mapping[code] = str(candidates.iloc[-1]["行业大类"]).strip()
        return mapping
