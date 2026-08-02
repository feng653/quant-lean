"""Pure research computations for factor validation and attribution."""

from backend.research.factor_analysis import (
    analyze_factor_decay,
    analyze_quantile_returns,
    attribute_portfolio_returns,
    calculate_ic,
    compute_forward_returns,
    cross_sectional_preprocess,
    factor_correlation_matrix,
    neutralize_factor_exposures,
    neutralize_industry_size,
)

__all__ = [
    "analyze_factor_decay",
    "analyze_quantile_returns",
    "attribute_portfolio_returns",
    "calculate_ic",
    "compute_forward_returns",
    "cross_sectional_preprocess",
    "factor_correlation_matrix",
    "neutralize_factor_exposures",
    "neutralize_industry_size",
]
