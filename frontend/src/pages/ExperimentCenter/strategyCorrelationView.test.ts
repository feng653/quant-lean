import { describe, expect, it } from 'vitest';
import type { StrategyCorrelationReport } from '../../services/experiments';
import {
  buildCorrelationHeatmap,
  correlationColor,
  pairKey,
} from './strategyCorrelationView';

const REPORT: StrategyCorrelationReport = {
  analysis_role: 'post_hoc_diversification_diagnostic',
  method: 'pearson',
  min_observations: 60,
  return_definition: 'adjacent_persisted_equity_pct_change',
  thresholds: {
    near_duplicate: 0.95,
    high_positive: 0.8,
    negative_diversifier: -0.25,
    low_absolute: 0.2,
  },
  experiments: [
    {
      id: 2,
      name: '低波动',
      strategy_id: 'low_volatility',
      test_start: '2020-01-01',
      test_end: '2025-12-31',
      quality: {
        equity_observations: 100,
        return_observations: 99,
        invalid_equity_points: 0,
        duplicate_dates: 0,
        invalid_returns: 0,
        return_start: '2020-01-02',
        return_end: '2025-12-31',
      },
    },
    {
      id: 9,
      name: '动量',
      strategy_id: 'momentum',
      test_start: '2020-01-01',
      test_end: '2025-12-31',
      quality: {
        equity_observations: 100,
        return_observations: 99,
        invalid_equity_points: 0,
        duplicate_dates: 0,
        invalid_returns: 0,
        return_start: '2020-01-02',
        return_end: '2025-12-31',
      },
    },
  ],
  matrix: {
    experiment_ids: [2, 9],
    values: [[1, null], [null, 1]],
    overlap_counts: [[99, 42], [42, 99]],
  },
  pairs: [{
    left_experiment_id: 2,
    right_experiment_id: 9,
    correlation: null,
    overlap: 42,
    overlap_start: '2020-01-02',
    overlap_end: '2020-03-01',
    interval_mismatch_exclusions: 0,
    classification: 'unavailable',
    unavailable_reason: 'insufficient_overlap',
  }],
  warnings: [],
  summary: {
    total_pairs: 1,
    available_pairs: 0,
    unavailable_pairs: 1,
    high_correlation_pairs: 0,
    negative_diversifier_pairs: 0,
  },
};

describe('strategy correlation view helpers', () => {
  it('uses a stable undirected pair key', () => {
    expect(pairKey(9, 2)).toBe('2:9');
    expect(pairKey(2, 9)).toBe('2:9');
  });

  it('distinguishes risk and diversifier colors from unavailable cells', () => {
    expect(correlationColor(0.9)).toBe('#b91c1c');
    expect(correlationColor(-0.9)).toBe('#1d4ed8');
    expect(correlationColor(null)).toBe('#d1d5db');
  });

  it('preserves overlap evidence and marks unavailable cells in the heatmap', () => {
    const option = buildCorrelationHeatmap(REPORT);
    const series = option.series[0];
    expect(series.data).toHaveLength(4);
    const unavailable = series.data.find(
      (cell) => cell.leftId === 2 && cell.rightId === 9,
    );
    expect(unavailable?.unavailable).toBe(true);
    expect(unavailable?.value[3]).toBe(42);
    expect(option.xAxis.data).toEqual(['#2 低波动', '#9 动量']);
  });
});
