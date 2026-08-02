"""数据层 —— 行情获取、缓存、交易日历、股票池管理.

主要组件:
    - DataSource / AKShareSource:  数据源抽象与实现
    - DataCache:                    Parquet 行情缓存
    - TradingCalendar:              A 股交易日历
    - UniverseManager:              股票池与行业筛选
    - compute_data_version:         数据版本指纹
"""

from backend.data.sources.base import DataSource
from backend.data.sources.akshare_source import AKShareSource
from backend.data.cache import DataCache
from backend.data.calendar import TradingCalendar
from backend.data.universe import UniverseManager, PRESET_POOLS
from backend.data.versioning import compute_data_version

__all__ = [
    "DataSource",
    "AKShareSource",
    "DataCache",
    "TradingCalendar",
    "UniverseManager",
    "PRESET_POOLS",
    "compute_data_version",
]
