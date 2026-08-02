import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { FactorResearchResult } from '../../services/factorResearch';
import NeutralizationResult from './NeutralizationResult';

describe('factor neutralization result', () => {
  it('shows before/after exposure and exclusions', () => {
    const result = {
      schema_version: 'factor-neutralization/v1',
      mode: 'industry+size',
      status: 'completed',
      fit_window: 'same_trading_date_only',
      inputs: { industry: {}, size: {} },
      factor_summaries: {},
      primary_factor: {
        schema_version: 'factor-neutralization/v1',
        mode: 'industry+size',
        method: 'daily_cross_sectional_ols',
        fit_window: 'same_trading_date_only',
        summary: {
          dates_total: 2,
          dates_neutralized: 1,
          dates_excluded: 1,
          observations_neutralized: 10,
          possible_observations: 20,
          coverage_ratio: 0.5,
          dropped_by_reason: { factor_missing: 1 },
          mean_r_squared_before: 0.8,
          mean_r_squared_after: 0,
        },
        daily: [{
          date: '2024-01-02',
          status: 'ok',
          sample_count: 10,
          candidate_count: 10,
          coverage_ratio: 1,
          dropped_by_reason: {
            factor_missing: 0,
            industry_missing: 0,
            size_missing_or_nonpositive: 0,
          },
          rank: 3,
          feature_count: 3,
          before: {
            r_squared: 0.8,
            intercept: 1,
            baseline_industry: 'BANK',
            industry_coefficients: { TECH: 0.3 },
            log_market_cap: 0.4,
          },
          after: {
            r_squared: 0,
            intercept: 0,
            baseline_industry: 'BANK',
            industry_coefficients: { TECH: 0 },
            log_market_cap: 0,
          },
        }],
      },
    } satisfies NonNullable<FactorResearchResult['neutralization']>;

    const html = renderToStaticMarkup(<NeutralizationResult result={result} />);
    expect(html).toContain('中性化前平均 R²');
    expect(html).toContain('0.8000');
    expect(html).toContain('0.4000 / 0.0000');
    expect(html).toContain('有效 1/2 日');
  });

  it('does not render for old or non-neutralized runs', () => {
    expect(renderToStaticMarkup(<NeutralizationResult result={undefined} />)).toBe('');
  });
});
