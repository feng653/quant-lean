"""数据源抽象基类 —— 定义统一的数据获取接口，供所有数据源实现."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class DataSource(ABC):
    """量化数据源抽象基类。

    所有数据源（AKShare、BaoStock、Tushare 等）必须实现此接口。
    方法均为协程，允许实现方在获取过程中执行异步 HTTP 请求。
    """

    @abstractmethod
    async def fetch_daily(
        self, codes: list[str], start: str, end: str
    ) -> pd.DataFrame:
        """获取多只股票的日线数据，返回 OHLCV panel。

        Args:
            codes: 股票代码列表，如 ["000001", "000002"]。
            start: 起始日期 "YYYY-MM-DD"。
            end:   结束日期 "YYYY-MM-DD"。

        Returns:
            DataFrame:
                index  = date (datetime64[ns])
                columns = MultiIndex(stock code, field)
                fields 至少包含 open、close（前复权）。
        """
        ...

    async def fetch_index_daily(
        self,
        index_code: str,
        start: str,
        end: str,
    ) -> pd.Series:
        """获取指数日收盘序列。

        这是可选能力，故意不标记为 abstract，避免破坏尚未支持指数行情的
        测试数据源和第三方数据源。调用方应显式处理 ``NotImplementedError``。

        Returns:
            名为 ``close``、DatetimeIndex 升序的数值 Series。
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support index daily data"
        )

    @abstractmethod
    async def fetch_index_components(
        self, index_code: str, date: str | None = None
    ) -> list[str]:
        """获取某日指数成分股列表。

        Args:
            index_code: 指数代码，如 "000300"（沪深300）、"000905"（中证500）。
            date:       目标日期（YYYY-MM-DD）。不支持历史成分的数据源忽略此参数，
                        仅能返回最新成分股（已知局限：幸存者偏差）。

        Returns:
            成分股代码列表。
        """
        ...

    @abstractmethod
    async def fetch_trading_calendar(
        self, start: str, end: str
    ) -> list[str]:
        """获取交易日历。

        Args:
            start: 起始日期 "YYYY-MM-DD"。
            end:   结束日期 "YYYY-MM-DD"。

        Returns:
            日期字符串列表，按时间升序排列。
        """
        ...

    @abstractmethod
    async def fetch_industry_list(self) -> list[dict]:
        """获取行业分类列表。

        Returns:
            每个元素为 {"code": 行业代码, "name": 行业名称} 的字典列表。
            例如: [{"code": "BK0477", "name": "银行"}, ...]
        """
        ...
