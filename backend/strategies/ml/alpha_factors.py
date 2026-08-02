"""Alpha158 因子计算模块 —— 用纯 pandas 实现核心因子集.

为 Alpha158+LGB 和 Alpha158+XGB 策略提供共享的因子计算逻辑.
实现 50+ 个核心因子，覆盖动量、波动率、换手、相关性、价格形态等维度.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_alpha_factors(pivot: pd.DataFrame) -> pd.DataFrame:
    """从日线宽表计算 Alpha158 因子集.

    Args:
        pivot: 日线宽表 DataFrame.
            - index: 日期 (DatetimeIndex)
            - columns: MultiIndex (code, field)
            - 必须包含 field: close, volume (可选: high, low, open, amount)

    Returns:
        DataFrame with index=date, columns=MultiIndex (code, factor_name).
        因子值已 cross-sectionally 排名归一化（0~1）.
    """
    codes = _extract_codes(pivot)
    if not codes:
        raise ValueError("无法从 pivot 中提取股票代码")

    factor_dfs: dict[str, pd.DataFrame] = {}

    for code in codes:
        df = _compute_single_stock_factors(pivot, code)
        if df is not None and len(df) > 60:
            factor_dfs[code] = df

    if not factor_dfs:
        raise ValueError("没有股票有足够数据计算因子")

    # 构建 MultiIndex DataFrame: (code, factor)
    result: dict[tuple, pd.Series] = {}
    for code, df in factor_dfs.items():
        for col in df.columns:
            result[(code, col)] = df[col]

    factor_df = pd.DataFrame(result)
    factor_df.index.name = "date"

    # 截面排名归一化 (0~1)
    factor_df = _cross_sectional_rank(factor_df)

    return factor_df


def _extract_codes(pivot: pd.DataFrame) -> list[str]:
    """从 pivot 中提取股票代码列表."""
    if isinstance(pivot.columns, pd.MultiIndex):
        return list({c[0] for c in pivot.columns if isinstance(c, tuple)})
    # 简单列名 fallback: 列名就是股票代码
    codes = [str(c) for c in pivot.columns if c != "date"]
    if codes:
        return codes
    return []


def _get_field(pivot: pd.DataFrame, code: str, field: str) -> pd.Series | None:
    """获取单只股票的某个字段序列."""
    if isinstance(pivot.columns, pd.MultiIndex):
        if (code, field) in pivot.columns:
            return pivot[(code, field)]
        return None
    # 简单列名 fallback: field='close' 时返回 pivot[code]
    if field == "close" and code in pivot.columns:
        return pivot[code]
    return None


def _compute_single_stock_factors(
    pivot: pd.DataFrame, code: str
) -> pd.DataFrame | None:
    """计算单只股票的 50+ Alpha158 因子.

    因子分组:
        - K 线形态: KMID, KLEN, KMID2, KUP, KUP2, KLOW, KLOW2, KSFT, KSFT2
        - 收益率: ROC5, ROC10, ROC20, ROC60
        - 均线偏离: MA5, MA10, MA20, MA60 偏离比
        - 波动率: STD5, STD10, STD20, STD60
        - 换手率: VOL5, VOL10, VOL20
        - 量价: VWAP5, VWAP10, VWAP20, CORR, CORD, CORR20
        - 高阶矩: SKEW, KURT
        - 其他: RSV, RSV5, MAX5, MIN5, ...
    """
    close = _get_field(pivot, code, "close")
    if close is None or len(close) < 120:
        return None

    close = close.dropna()
    idx = close.index

    high = _get_field(pivot, code, "high")
    low = _get_field(pivot, code, "low")
    open_ = _get_field(pivot, code, "open")
    volume = _get_field(pivot, code, "volume")
    amount = _get_field(pivot, code, "amount")

    if high is not None:
        high = high.reindex(idx)
    if low is not None:
        low = low.reindex(idx)
    if open_ is not None:
        open_ = open_.reindex(idx)
    if volume is not None:
        volume = volume.reindex(idx).fillna(0)
    if amount is not None:
        amount = amount.reindex(idx).fillna(0)

    factors = pd.DataFrame(index=idx)

    # ── 1. 收益率因子 ──
    ret_1d = close.pct_change()
    factors["RET_1D"] = ret_1d
    factors["RET_5D"] = close.pct_change(5)
    factors["RET_10D"] = close.pct_change(10)
    factors["RET_20D"] = close.pct_change(20)
    factors["RET_60D"] = close.pct_change(60)

    # ── 2. K线形态因子 ──
    if high is not None and low is not None and open_ is not None:
        # KMID: (close - open) / open
        o = open_.replace(0, np.nan)
        factors["KMID"] = (close - open_) / o
        # KLEN: (high - low) / open
        factors["KLEN"] = (high - low) / o
        # KMID2: (close - open) / (high - low + 1e-8)
        factors["KMID2"] = (close - open_) / (high - low + 1e-8)
        # KUP: (high - max(open, close)) / open
        max_oc = pd.concat([open_, close], axis=1).max(axis=1)
        factors["KUP"] = (high - max_oc) / o
        # KLOW: (min(open, close) - low) / open
        min_oc = pd.concat([open_, close], axis=1).min(axis=1)
        factors["KLOW"] = (min_oc - low) / o
        # RSV: (close - low) / (high - low + 1e-8)
        factors["RSV"] = (close - low) / (high - low + 1e-8)
        # WillR: (high - close) / (high - low + 1e-8)
        factors["WILLR"] = (high - close) / (high - low + 1e-8)

    # ── 3. 均线偏离因子 ──
    for w in [5, 10, 20, 60]:
        ma = close.rolling(w).mean()
        factors[f"MA{w}_DEV"] = close / ma - 1

    # ── 4. 波动率因子 ──
    for w in [5, 10, 20, 60]:
        factors[f"STD{w}"] = ret_1d.rolling(w).std()

    # 下行波动率
    factors["DOWN_STD20"] = ret_1d.clip(upper=0).rolling(20).std()

    # ── 5. 换手率/成交量因子 ──
    if volume is not None and volume.sum() > 0:
        for w in [5, 10, 20]:
            vol_ma = volume.rolling(w).mean()
            factors[f"VOL{w}"] = volume / vol_ma.replace(0, np.nan) - 1
            # 量比变化
            if w > 5:
                factors[f"VOL_DELTA_{w}"] = vol_ma.pct_change(w)

    if amount is not None and amount.sum() > 0 and volume is not None:
        # VWAP 偏离
        cum_amount = amount.rolling(20).sum()
        cum_volume = volume.rolling(20).sum()
        vwap = cum_amount / cum_volume.replace(0, np.nan)
        factors["VWAP20_DEV"] = close / vwap - 1

    # ── 6. 量价相关性 ──
    # 5日量价相关系数
    def rolling_corr(a, b, window):
        """滚动相关系数."""
        ma = a.rolling(window).mean()
        mb = b.rolling(window).mean()
        cov = ((a - ma) * (b - mb)).rolling(window).mean()
        sa = ((a - ma) ** 2).rolling(window).mean() ** 0.5
        sb = ((b - mb) ** 2).rolling(window).mean() ** 0.5
        return cov / (sa * sb + 1e-8)

    if volume is not None and volume.sum() > 0:
        factors["CORR_RET_VOL_5"] = rolling_corr(ret_1d, volume, 5)
        factors["CORR_RET_VOL_20"] = rolling_corr(ret_1d, volume, 20)

    # ── 7. 价格与均线的相关系数 ──
    ma5 = close.rolling(5).mean()
    factors["CORR_CLOSE_MA5_10"] = rolling_corr(close, ma5, 10)

    # ── 8. 高阶矩 ──
    # 偏度 (20日)
    ret_ma20 = ret_1d.rolling(20).mean()
    ret_std20 = ret_1d.rolling(20).std()
    factors["SKEW20"] = (
        (ret_1d - ret_ma20) ** 3
    ).rolling(20).mean() / (ret_std20 ** 3 + 1e-8)

    # 峰度 (20日)
    factors["KURT20"] = (
        (ret_1d - ret_ma20) ** 4
    ).rolling(20).mean() / (ret_std20 ** 4 + 1e-8) - 3

    # ── 9. 极值因子 ──
    factors["MAX_HIGH_5"] = close.rolling(5).max() / close - 1
    factors["MIN_LOW_5"] = close.rolling(5).min() / close - 1
    factors["MAX_HIGH_20"] = close.rolling(20).max() / close - 1
    factors["MIN_LOW_20"] = close.rolling(20).min() / close - 1

    # ── 10. 涨跌天数比 ──
    up_days = (ret_1d > 0).astype(float)
    down_days = (ret_1d < 0).astype(float)
    factors["UP_DAYS_20"] = up_days.rolling(20).sum() / 20
    factors["DOWN_DAYS_20"] = down_days.rolling(20).sum() / 20

    # ── 11. 收益加速度 ──
    factors["ROC5_ACC"] = factors["RET_5D"] - factors["RET_10D"]
    factors["ROC10_ACC"] = factors["RET_10D"] - factors["RET_20D"]

    # ── 12. 振幅因子 ──
    if high is not None and low is not None:
        amplitude = (high - low) / close.shift(1)
        factors["AMP5"] = amplitude.rolling(5).mean()
        factors["AMP20"] = amplitude.rolling(20).mean()

    # ── 13. RSI ──
    delta = ret_1d
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain_14 = gain.rolling(14).mean()
    avg_loss_14 = loss.rolling(14).mean()
    rs = avg_gain_14 / (avg_loss_14 + 1e-8)
    factors["RSI14"] = 100 - 100 / (1 + rs)

    # ── 14. MACD 相关 ──
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    factors["DIF"] = dif / close
    dea = dif.ewm(span=9, adjust=False).mean()
    factors["DEA"] = dea / close
    factors["MACD_HIST"] = (dif - dea) / close

    # ── 15. 布林带 ──
    bb_ma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    factors["BB_WIDTH"] = (2 * bb_std) / bb_ma.replace(0, np.nan)
    factors["BB_PCT"] = (close - bb_ma) / (2 * bb_std + 1e-8)

    # ── 16. Alpha101 类因子 ──
    # rank(ts_argmax(signedpower(returns, 2), 5))
    ret_2 = ret_1d ** 2
    factors["ALPHA_RET2_5"] = ret_2.rolling(5).apply(lambda x: x.argmax()) / 5

    # correlation(rank(close), rank(volume), 10)
    if volume is not None:
        close_rank = close.rolling(10).rank(pct=True)
        vol_rank = volume.rolling(10).rank(pct=True)
        factors["CORR_RANK_10"] = rolling_corr(close_rank, vol_rank, 10)

    return factors.dropna(how="all")


def _cross_sectional_rank(factor_df: pd.DataFrame) -> pd.DataFrame:
    """对因子值按日期进行截面排名归一化（向量化实现）.

    每个交易日，将所有股票的因子值排名并归一化到 [0, 1].
    """
    stacked = factor_df.stack(level=0)
    ranked = stacked.groupby(level=0).rank(pct=True)
    result = ranked.unstack(level=1).swaplevel(axis=1)
    return result


def get_feature_names() -> list[str]:
    """返回所有因子名称列表（用于训练时对齐特征）."""
    return [
        "RET_1D", "RET_5D", "RET_10D", "RET_20D", "RET_60D",
        "KMID", "KLEN", "KMID2", "KUP", "KLOW",
        "RSV", "WILLR",
        "MA5_DEV", "MA10_DEV", "MA20_DEV", "MA60_DEV",
        "STD5", "STD10", "STD20", "STD60", "DOWN_STD20",
        "VOL5", "VOL10", "VOL20", "VOL_DELTA_10", "VOL_DELTA_20",
        "VWAP20_DEV",
        "CORR_RET_VOL_5", "CORR_RET_VOL_20",
        "CORR_CLOSE_MA5_10",
        "SKEW20", "KURT20",
        "MAX_HIGH_5", "MIN_LOW_5", "MAX_HIGH_20", "MIN_LOW_20",
        "UP_DAYS_20", "DOWN_DAYS_20",
        "ROC5_ACC", "ROC10_ACC",
        "AMP5", "AMP20",
        "RSI14",
        "DIF", "DEA", "MACD_HIST",
        "BB_WIDTH", "BB_PCT",
        "ALPHA_RET2_5", "CORR_RANK_10",
    ]


def get_available_features(factor_df: pd.DataFrame) -> list[str]:
    """返回因子 DataFrame 中实际存在的因子名称（用于动态特征对齐）."""
    factors: set[str] = set()
    if isinstance(factor_df.columns, pd.MultiIndex):
        for _code, feat in factor_df.columns:
            factors.add(feat)
    return sorted(factors)
