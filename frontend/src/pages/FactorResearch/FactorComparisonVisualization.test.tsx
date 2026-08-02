// @vitest-environment jsdom

import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FactorRunComparison } from '../../services/factorResearch';
import FactorComparisonVisualization from './FactorComparisonVisualization';

const chart = vi.hoisted(() => vi.fn());

vi.mock('../../components/shared/EChart', () => ({
  default: (props: { option: unknown }) => {
    chart(props);
    return <div data-testid="comparison-chart" />;
  },
}));

function comparison(datasetConsistent = true): FactorRunComparison {
  return {
    schema_version: 'factor-research-comparison/v1',
    dataset_consistent: datasetConsistent,
    runs: [{
      run_id: 'frun_a',
      factor_id: 'momentum_20',
      created_at: '2026-07-31T00:00:00Z',
      dataset_digest: 'a'.repeat(64),
      primary_horizon: 5,
      rank_ic_mean: 0.0234,
      rank_ic_ir: 0.31,
      rank_ic_positive_ratio: 0.62,
      long_short_mean: 0.0123,
      monotonicity: 0.7,
    }],
  };
}

describe('FactorComparisonVisualization', () => {
  beforeEach(() => chart.mockClear());

  it('renders a descriptive chart without implying automatic selection', () => {
    render(<FactorComparisonVisualization comparison={comparison()} factorNames={{ momentum_20: '20日动量' }} />);

    expect(screen.getByRole('region', { name: '因子比较可视化' })).toBeTruthy();
    expect(screen.getByText('RankIC 与多空均值')).toBeTruthy();
    expect(screen.getByText(/保留 API 返回顺序/)).toBeTruthy();
    expect(screen.getByTestId('comparison-chart')).toBeTruthy();
    const option = chart.mock.calls[0][0].option as { series: Array<{ data: Array<number | null> }> };
    expect(option.series[0].data).toEqual([0.0234]);
    expect(option.series[1].data).toEqual([0.0123]);
  });

  it('warns against direct comparison and leaves invalid measurements absent', () => {
    const invalid = comparison(false);
    invalid.runs[0].rank_ic_mean = Number.NaN;
    invalid.runs[0].long_short_mean = Number.POSITIVE_INFINITY;
    invalid.runs[0].rank_ic_ir = null;
    invalid.runs[0].rank_ic_positive_ratio = null;

    render(<FactorComparisonVisualization comparison={invalid} factorNames={{ momentum_20: '20日动量' }} />);

    expect(screen.getByRole('alert').textContent).toContain('不可直接横比');
    expect(screen.getByText(/不能据此比较高低、选优或形成交易建议/)).toBeTruthy();
    const option = chart.mock.calls[0][0].option as { series: Array<{ data: Array<number | null> }> };
    expect(option.series[0].data).toEqual([null]);
    expect(option.series[1].data).toEqual([null]);
  });
});
