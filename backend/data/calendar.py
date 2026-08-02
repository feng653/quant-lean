"""交易日历 —— A 股交易日判断、偏移、再平衡日计算.

数据来源: AKShare → tool_trade_date_hist_sina() 或降级为工作日（排除周末）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("quant_platform.data.calendar")


class TradingCalendar:
    """A 股交易日历管理器。

    特性:
    - 懒加载 + JSON 缓存（减少网络请求）
    - 降级：网络不可用时自动使用工作日（排除周末）
    - 再平衡日计算：月末 / 周末 / 季末 / 年末
    """

    def __init__(self, cache_dir: str | None = None) -> None:
        """初始化交易日历。

        Args:
            cache_dir: 缓存目录路径。None 则使用 backend.config 中的 DATA_CACHE_DIR。
        """
        from backend.config import settings

        if cache_dir is None:
            self._cache_dir = settings.abs_path(settings.DATA_CACHE_DIR)
        else:
            self._cache_dir = Path(cache_dir)

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / "calendar.json"
        self._dates: list[str] = []
        self._date_set: set[str] = set()

    # ── 加载 ─────────────────────────────────────────────────────────────────

    async def load(self, source, start: str, end: str) -> list[str]:
        """加载交易日历，优先从缓存读取，缓存未命中则从 source 拉取。

        Args:
            source: DataSource 实例。
            start:  起始日期 "YYYY-MM-DD"。
            end:    截止日期 "YYYY-MM-DD"。

        Returns:
            日期字符串列表（升序）。
        """
        # 1. 尝试从缓存加载
        cached = self._load_cache()
        if cached:
            self._dates = cached
            self._date_set = set(cached)
            # 检查覆盖范围
            if cached and cached[0] <= start and cached[-1] >= end:
                logger.debug(
                    "Calendar cache hit: %s → %s (%d days)",
                    cached[0], cached[-1], len(cached),
                )
                return [d for d in cached if start <= d <= end]

        # 2. 从数据源拉取
        logger.info("Fetching trading calendar from source: %s → %s", start, end)
        try:
            self._dates = await source.fetch_trading_calendar(start, end)
        except Exception:
            logger.exception("Failed to fetch trading calendar, using fallback")
            self._dates = self._fallback_range(start, end)

        if not self._dates:
            self._dates = self._fallback_range(start, end)

        self._date_set = set(self._dates)
        self._save_cache()
        return self._dates

    async def ensure_loaded(self, source, date: str) -> None:
        """确保包含指定日期的日历已加载。"""
        if date in self._date_set:
            return
        start = min(date, self._dates[0] if self._dates else "2010-01-01")
        end = max(date, self._dates[-1] if self._dates else date)
        await self.load(source, start, end)

    # ── 查询 ─────────────────────────────────────────────────────────────────

    def is_trading_day(self, date: str) -> bool:
        """判断某日是否为交易日。

        Args:
            date: "YYYY-MM-DD"

        Returns:
            True 如果是交易日。
        """
        if self._date_set:
            return date in self._date_set
        # 降级：排除周六日
        return self._is_weekday(date)

    def next_trading_day(self, date: str, inclusive: bool = False) -> str:
        """返回 date 之后最近的一个交易日。

        Args:
            date:      参考日期 "YYYY-MM-DD"。
            inclusive: True 时 date 本身若是交易日也返回。

        Returns:
            下一个交易日 "YYYY-MM-DD"。
        """
        if inclusive and self.is_trading_day(date):
            return date
        return self._offset(date, 1)

    def prev_trading_day(self, date: str, inclusive: bool = False) -> str:
        """返回 date 之前最近的一个交易日。

        Args:
            date:      参考日期 "YYYY-MM-DD"。
            inclusive: True 时 date 本身若是交易日也返回。

        Returns:
            上一个交易日 "YYYY-MM-DD"。
        """
        if inclusive and self.is_trading_day(date):
            return date
        return self._offset(date, -1)

    def trading_days_between(self, start: str, end: str) -> list[str]:
        """返回 [start, end] 区间内（含两端）的交易日列表。

        Args:
            start: 起始日期。
            end:   截止日期。

        Returns:
            日期字符串列表（升序）。
        """
        if self._dates:
            return [d for d in self._dates if start <= d <= end]
        # 降级：生成工作日范围
        dates = pd.bdate_range(start, end)
        return [d.strftime("%Y-%m-%d") for d in dates]

    def next_rebalance_day(self, date: str, frequency: str) -> str:
        """获取下一个再平衡日。

        Args:
            date:      当前日期 "YYYY-MM-DD"。
            frequency: 调仓频率。支持:
                       - "daily":     下一个交易日
                       - "weekly":    下个周五（非交易日则向前取）
                       - "monthly":   下个月最后一个交易日
                       - "quarterly": 下个季度最后一个交易日
                       - "yearly":    下个年度最后一个交易日

        Returns:
            下一个再平衡日 "YYYY-MM-DD"。若不存在（日历未加载），返回粗略估算。
        """
        freq = frequency.lower().strip()
        dt = pd.Timestamp(date)

        if freq == "daily":
            return self.next_trading_day(date, inclusive=False)

        if freq == "weekly":
            # 下个周五
            offset_days = (4 - dt.weekday()) % 7  # weekday: Mon=0, Fri=4
            if offset_days == 0:
                offset_days = 7  # 如果是周五，跳到下周五
            candidate = dt + pd.Timedelta(days=offset_days)
            return self._nearest_trading_day(candidate.strftime("%Y-%m-%d"))

        if freq == "monthly":
            # 下个月最后一天
            candidate = self._next_month_end(dt)
            return self._nearest_trading_day(candidate.strftime("%Y-%m-%d"))

        if freq == "quarterly":
            # 下个季度末（3/6/9/12月最后一天）
            candidate = self._next_quarter_end(dt)
            return self._nearest_trading_day(candidate.strftime("%Y-%m-%d"))

        if freq == "yearly":
            # 下年底
            year = dt.year
            candidate = pd.Timestamp(f"{year}-12-31")
            if candidate <= dt:
                candidate = pd.Timestamp(f"{year + 1}-12-31")
            return self._nearest_trading_day(candidate.strftime("%Y-%m-%d"))

        # 未知频率：退化到下一个交易日
        return self.next_trading_day(date)

    # ── 内部方法 ─────────────────────────────────────────────────────────────

    def _offset(self, date: str, direction: int) -> str:
        """在交易日历中偏移 direction 步（正数向前，负数向后）。"""
        dt = pd.Timestamp(date)

        if self._dates:
            # 在有序列表中查找
            if direction > 0:
                for d in self._dates:
                    if d > date:
                        return d
            else:
                for d in reversed(self._dates):
                    if d < date:
                        return d
            # 没找到：降级
            return (dt + pd.Timedelta(days=direction)).strftime("%Y-%m-%d")

        # 降级：跳过周末
        step = 1 if direction > 0 else -1
        max_loop = 30
        for _ in range(max_loop):
            dt = dt + pd.Timedelta(days=step)
            if self._is_weekday(dt.strftime("%Y-%m-%d")):
                return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")

    def _nearest_trading_day(self, date: str) -> str:
        """返回离 date 最近（向前取、不晚于 date）的交易日。"""
        dt = pd.Timestamp(date)
        max_loop = 10
        for _ in range(max_loop):
            ds = dt.strftime("%Y-%m-%d")
            if self.is_trading_day(ds):
                return ds
            dt = dt - pd.Timedelta(days=1)
        return date

    @staticmethod
    def _is_weekday(date_str: str) -> bool:
        """判断是否为工作日（周一~周五）。"""
        dt = pd.Timestamp(date_str)
        return dt.weekday() < 5  # Mon=0 ... Fri=4

    @staticmethod
    def _fallback_range(start: str, end: str) -> list[str]:
        """降级：生成工作日范围。"""
        dates = pd.bdate_range(start, end)
        return [d.strftime("%Y-%m-%d") for d in dates]

    @staticmethod
    def _next_month_end(dt: pd.Timestamp) -> pd.Timestamp:
        """返回 dt 所在月份之后下一个月的最后一天。"""
        year, month = dt.year, dt.month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        # 下个月第一天减一天 = 当月最后一天
        next_month_first = pd.Timestamp(year=year, month=month, day=1)
        return next_month_first + pd.offsets.MonthEnd(1)

    @staticmethod
    def _next_quarter_end(dt: pd.Timestamp) -> pd.Timestamp:
        """返回 dt 所在季度之后下一个季度的最后一天。"""
        year, month = dt.year, dt.month
        # 当前季度末月：3/6/9/12
        current_quarter_end_month = ((month - 1) // 3 + 1) * 3
        if month <= current_quarter_end_month:
            # 还在当前季度内，跳到下季度
            target_month = current_quarter_end_month + 3
        else:
            target_month = current_quarter_end_month

        target_year = year
        if target_month > 12:
            target_year += 1
            target_month -= 12
        elif target_month <= 0:
            target_year -= 1
            target_month += 12

        return pd.Timestamp(year=target_year, month=target_month, day=1) + pd.offsets.MonthEnd(1)

    # ── 缓存读写 ─────────────────────────────────────────────────────────────

    def _load_cache(self) -> list[str] | None:
        """从 JSON 文件加载缓存。"""
        if not self._cache_file.exists():
            return None
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            return data.get("dates", [])
        except Exception:
            logger.warning("Failed to load calendar cache, will re-fetch")
            return None

    def _save_cache(self) -> None:
        """将当前交易日历保存到 JSON 文件。"""
        if not self._dates:
            return
        data = {
            "dates": self._dates,
            "first_date": self._dates[0],
            "last_date": self._dates[-1],
            "count": len(self._dates),
            "updated_at": pd.Timestamp.now().isoformat(),
        }
        self._cache_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
