from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from backend.research.factor_analysis import (
    analyze_factor_decay,
    analyze_quantile_returns,
    attribute_portfolio_returns,
    calculate_ic,
    compute_forward_returns,
    cross_sectional_preprocess,
    factor_correlation_matrix,
    neutralize_industry_size,
)


def _frame(payload: dict) -> pd.DataFrame:
    return pd.DataFrame(
        payload["values"],
        index=pd.to_datetime(payload["dates"]),
        columns=payload["codes"],
        dtype=float,
    )


def _assert_json_safe(result: dict) -> None:
    json.dumps(result, allow_nan=False, sort_keys=True)


def test_preprocessing_is_daily_robust_deterministic_and_input_immutable() -> None:
    dates = pd.to_datetime(["2025-01-03", "2025-01-02", "2025-01-04"])
    factor = pd.DataFrame(
        {
            "D": [100.0, 1_030.0, np.nan],
            "B": [2.0, 1_010.0, np.nan],
            "A": [1.0, 1_000.0, np.nan],
            "C": [3.0, np.nan, np.nan],
        },
        index=dates,
    )
    original = factor.copy(deep=True)

    result = cross_sectional_preprocess(
        factor,
        winsor_method="mad",
        mad_scale=2.0,
        missing="median",
    )
    processed = _frame(result["values"])

    pdt.assert_frame_equal(factor, original)
    assert result["values"]["codes"] == ["A", "B", "C", "D"]
    assert result["values"]["dates"] == [
        "2025-01-02",
        "2025-01-03",
        "2025-01-04",
    ]
    assert processed.loc["2025-01-02"].mean() == pytest.approx(0.0)
    assert processed.loc["2025-01-03"].mean() == pytest.approx(0.0)
    assert processed.loc["2025-01-02", "C"] == pytest.approx(
        processed.loc["2025-01-02"].median()
    )
    assert processed.loc["2025-01-03"].max() < 2.0
    assert processed.loc["2025-01-04"].isna().all()
    assert result["diagnostics"][-1]["status"] == "insufficient_samples"
    _assert_json_safe(result)


def test_quantile_winsor_and_constant_cross_section_are_safe() -> None:
    factor = pd.DataFrame(
        [[1.0, 2.0, 3.0, 1_000_000.0], [7.0, 7.0, 7.0, 7.0]],
        index=pd.date_range("2025-01-01", periods=2),
        columns=list("ABCD"),
    )
    result = cross_sectional_preprocess(
        factor,
        winsor_method="quantile",
        lower_quantile=0.1,
        upper_quantile=0.9,
    )
    processed = _frame(result["values"])

    assert processed.iloc[0].max() < 2.0
    assert processed.iloc[1].eq(0.0).all()
    assert result["diagnostics"][1]["status"] == "constant_cross_section"


def test_neutralization_removes_industry_and_size_without_cross_date_fit() -> None:
    codes = [f"S{number}" for number in range(8)]
    dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
    industries = pd.Series(
        {code: ("Bank" if index < 4 else "Tech") for index, code in enumerate(codes)}
    )
    caps = pd.DataFrame(
        [
            np.exp(np.linspace(8.0, 12.0, len(codes))),
            np.exp(np.linspace(10.0, 14.0, len(codes))),
        ],
        index=dates,
        columns=codes,
    )
    industry_effect = np.array([1.5] * 4 + [-0.75] * 4)
    factor = pd.DataFrame(
        [
            3.0 + industry_effect + 2.0 * np.log(caps.iloc[0]),
            -100.0 + industry_effect - 4.0 * np.log(caps.iloc[1]),
        ],
        index=dates,
        columns=codes,
    )
    original = factor.copy(deep=True)

    result = neutralize_industry_size(
        factor,
        industries,
        caps,
        min_samples=4,
    )
    residuals = _frame(result["residuals"])

    pdt.assert_frame_equal(factor, original)
    assert residuals.abs().to_numpy().max() < 1e-10
    assert result["exposures"][0]["coefficients"]["log_market_cap"] == pytest.approx(2.0)
    assert result["exposures"][1]["coefficients"]["log_market_cap"] == pytest.approx(-4.0)
    assert result["exposures"][0]["date"] == "2025-01-02"
    assert result["exposures"][1]["date"] == "2025-01-03"
    _assert_json_safe(result)


def test_neutralization_degrades_for_singular_small_and_all_nan_sections() -> None:
    date = pd.Timestamp("2025-01-02")
    codes = list("ABCDEF")
    industries = pd.Series(
        {"A": "Bank", "B": "Bank", "C": "Bank", "D": "Tech", "E": "Tech", "F": "Tech"}
    )
    # log(cap) is exactly collinear with the Tech dummy.
    caps = pd.Series(
        {code: np.exp(1.0 if industries[code] == "Bank" else 2.0) for code in codes}
    )
    singular = neutralize_industry_size(
        pd.DataFrame([[1, 2, 3, 4, 5, 6]], index=[date], columns=codes),
        industries,
        caps,
    )
    assert singular["exposures"][0]["status"] == "rank_deficient"
    assert _frame(singular["residuals"]).notna().all().all()

    small = neutralize_industry_size(
        pd.DataFrame([[1.0, 3.0]], index=[date], columns=["A", "D"]),
        industries,
        caps,
        min_samples=3,
    )
    assert small["exposures"][0]["status"] == "demean_fallback"
    assert _frame(small["residuals"]).loc[date].tolist() == [-1.0, 1.0]

    all_nan = neutralize_industry_size(
        pd.DataFrame([[np.nan] * 6], index=[date], columns=codes),
        industries,
        caps,
    )
    assert all_nan["exposures"][0]["n_obs"] == 0
    assert _frame(all_nan["residuals"]).isna().all().all()
    _assert_json_safe(all_nan)


def test_forward_returns_respect_multiindex_and_evaluation_boundary() -> None:
    dates = pd.date_range("2025-01-01", periods=6, freq="D")
    prices = pd.DataFrame(
        {
            ("B", "volume"): np.arange(10, 16),
            ("A", "close"): [10, 11, 12, 13, 14, 15],
            ("B", "close"): [20, 22, 24, 26, 28, 30],
        },
        index=dates,
    )
    prices.columns = pd.MultiIndex.from_tuples(prices.columns)
    original = prices.copy(deep=True)
    first = compute_forward_returns(
        prices,
        horizons=(2, 1),
        evaluation_end="2025-01-04",
    )
    mutated = prices.copy(deep=True)
    mutated.loc[dates[4]:, ("A", "close")] = 1_000_000
    second = compute_forward_returns(
        mutated,
        horizons=(1, 2),
        evaluation_end="2025-01-04",
    )

    pdt.assert_frame_equal(prices, original)
    assert first == second
    one_day = _frame(first["horizons"]["1"])
    two_day = _frame(first["horizons"]["2"])
    assert list(one_day.index) == list(dates[:4])
    assert one_day.loc[dates[2], "A"] == pytest.approx(13 / 12 - 1)
    assert np.isnan(one_day.loc[dates[3], "A"])
    assert two_day.loc[dates[1], "B"] == pytest.approx(26 / 22 - 1)
    assert two_day.iloc[-2:].isna().all().all()
    _assert_json_safe(first)
    with pytest.raises(ValueError, match="正整数"):
        compute_forward_returns(prices, horizons=(1, 2.5))
    with pytest.raises(ValueError, match="不能为空"):
        compute_forward_returns(pd.DataFrame(), horizons=(1,))


def test_ic_rank_ic_summaries_and_small_sample_guards() -> None:
    dates = pd.date_range("2025-01-01", periods=3)
    codes = list("ABCDEF")
    factor = pd.DataFrame(
        [range(6), range(6), range(6)],
        index=dates,
        columns=codes,
        dtype=float,
    )
    returns = pd.DataFrame(
        [range(6), range(5, -1, -1), [1.0] * 6],
        index=dates,
        columns=codes,
        dtype=float,
    )
    result = calculate_ic(factor, returns, min_samples=5)

    assert result["series"][0]["pearson_ic"] == pytest.approx(1.0)
    assert result["series"][1]["rank_ic"] == pytest.approx(-1.0)
    assert result["series"][2]["pearson_ic"] is None
    assert result["summary"]["pearson_ic"]["count"] == 2
    assert result["summary"]["pearson_ic"]["mean"] == pytest.approx(0.0)
    assert result["summary"]["rank_ic"]["positive_ratio"] == pytest.approx(0.5)

    too_small = calculate_ic(
        factor.iloc[:, :3],
        returns.iloc[:, :3],
        min_samples=5,
    )
    assert too_small["summary"]["pearson_ic"]["count"] == 0
    assert too_small["summary"]["pearson_ic"]["mean"] is None
    _assert_json_safe(too_small)


def test_factor_decay_quantiles_and_correlation_are_deterministic() -> None:
    dates = pd.date_range("2025-01-01", periods=3)
    codes = [f"S{number:02d}" for number in range(10)]
    factor = pd.DataFrame(
        [np.arange(10, dtype=float)] * 3,
        index=dates,
        columns=codes,
    )
    one_day = factor * 0.01
    five_day = -factor * 0.02

    decay = analyze_factor_decay(
        factor,
        {"5": five_day, "1": one_day},
        min_samples=5,
    )
    assert [point["horizon"] for point in decay["points"]] == [1, 5]
    assert decay["points"][0]["rank_ic"]["mean"] == pytest.approx(1.0)
    assert decay["points"][1]["rank_ic"]["mean"] == pytest.approx(-1.0)
    with pytest.raises(ValueError, match="不重复"):
        analyze_factor_decay(
            factor,
            {1: one_day, "1": one_day},
            min_samples=5,
        )

    quantiles = analyze_quantile_returns(
        factor[factor.columns[::-1]],
        one_day,
        quantiles=5,
        min_samples=10,
    )
    means = list(quantiles["mean_group_returns"].values())
    assert means == sorted(means)
    assert quantiles["long_short"]["mean"] > 0
    assert quantiles["monotonicity"] == pytest.approx(1.0)

    correlations = factor_correlation_matrix(
        {"inverse": -factor, "base": factor},
        method="spearman",
        min_samples=5,
    )
    assert correlations["factors"] == ["base", "inverse"]
    assert correlations["matrix"][0][0] == 1.0
    assert correlations["matrix"][0][1] == pytest.approx(-1.0)
    assert correlations["matrix"][1][0] == pytest.approx(-1.0)
    assert correlations["matrix"][1][1] == 1.0
    assert correlations["valid_date_counts"][0][1] == 3
    all_nan = factor_correlation_matrix(
        {"empty": factor * np.nan},
        min_samples=5,
    )
    assert all_nan["matrix"] == [[None]]
    assert all_nan["valid_date_counts"] == [[0]]
    _assert_json_safe(quantiles)
    _assert_json_safe(correlations)
    _assert_json_safe(all_nan)


def test_portfolio_industry_size_attribution_and_singular_diagnostics() -> None:
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=60, freq="D")
    industry = pd.DataFrame(
        {
            "Tech": rng.normal(0.0005, 0.01, len(dates)),
            "Bank": rng.normal(0.0002, 0.008, len(dates)),
        },
        index=dates,
    )
    size = pd.Series(rng.normal(0.0, 0.006, len(dates)), index=dates)
    portfolio = 0.001 + 0.5 * industry["Tech"] - 0.25 * industry["Bank"] + 0.2 * size
    originals = (
        portfolio.copy(deep=True),
        industry.copy(deep=True),
        size.copy(deep=True),
    )

    result = attribute_portfolio_returns(
        portfolio,
        industry,
        size,
        min_samples=20,
    )

    pdt.assert_series_equal(portfolio, originals[0])
    pdt.assert_frame_equal(industry, originals[1])
    pdt.assert_series_equal(size, originals[2])
    assert result["status"] == "ok"
    assert result["exposures"]["intercept"] == pytest.approx(0.001)
    assert result["exposures"]["industry::Tech"] == pytest.approx(0.5)
    assert result["exposures"]["industry::Bank"] == pytest.approx(-0.25)
    assert result["exposures"]["log_size"] == pytest.approx(0.2)
    assert result["diagnostics"]["r_squared"] == pytest.approx(1.0)

    singular_industry = industry.assign(TechCopy=industry["Tech"])
    singular = attribute_portfolio_returns(
        portfolio,
        singular_industry,
        size,
        min_samples=20,
    )
    assert singular["status"] == "rank_deficient"
    assert singular["diagnostics"]["rank"] < singular["diagnostics"]["n_features"]
    _assert_json_safe(result)
    _assert_json_safe(singular)


def test_portfolio_attribution_all_nan_and_short_history_return_nulls() -> None:
    dates = pd.date_range("2025-01-01", periods=4)
    result = attribute_portfolio_returns(
        pd.Series([np.nan] * 4, index=dates),
        pd.DataFrame({"Tech": [0.1, 0.2, 0.3, 0.4]}, index=dates),
        min_samples=3,
    )

    assert result["status"] == "insufficient_samples"
    assert result["diagnostics"]["sample_count"] == 0
    assert result["exposures"]["industry::Tech"] is None
    assert result["residual_returns"] == {"dates": [], "values": []}
    _assert_json_safe(result)
