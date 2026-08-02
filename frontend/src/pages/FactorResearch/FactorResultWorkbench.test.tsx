// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FactorResearchResult } from '../../services/factorResearch';
import FactorResultWorkbench from './FactorResultWorkbench';
import { diagnoseFactorResult } from './factorResultDiagnosis';

function baseResult(): FactorResearchResult {
  return {
    schema_version: 'factor-research/v4',
    factor: {
      factor_id: 'momentum_20',
      version: '1.0.0',
      definition_digest: 'a'.repeat(64),
      name: '20日动量',
      description: '测试因子',
      direction: 'high',
      lookback: 20,
      required_fields: ['close'],
      category: 'momentum',
      parameters: {},
      parameter_schema: {},
      dependencies: [],
      supersedes: null,
      status: 'published',
      deprecated: false,
      current: true,
      revision: 1,
      published_at: '2026-07-01T00:00:00Z',
      deprecated_at: null,
    },
    request: {
      factor_id: 'momentum_20',
      pool_preset: 'csi300',
      start: '2016-01-04',
      end: '2026-06-30',
      horizons: [5],
      primary_horizon: 5,
      quantiles: 5,
    },
    dataset: {
      cache_key: 'csi300',
      rows: 2500,
      codes: 288,
      date_start: '2016-01-04',
      date_end: '2026-06-30',
      content_sha256: 'b'.repeat(64),
      source_provenance: {},
    },
    preprocessing: { config: {}, diagnostics: [] },
    ic: {},
    decay: { points: [] },
    quantile_returns: {
      mean_group_returns: {},
      long_short: {
        count: 0,
        mean: null,
        std: null,
        icir: null,
        positive_ratio: null,
        t_stat: null,
      },
      monotonicity: null,
    },
    stability: null,
    limitations: ['仅用于研究。'],
    run: {
      run_id: `frun_${'c'.repeat(32)}`,
      created_at: '2026-07-31T00:00:00Z',
      request_digest: 'd'.repeat(64),
      dataset_digest: 'b'.repeat(64),
      result_digest: 'e'.repeat(64),
      run_digest: 'f'.repeat(64),
      archived_at: null,
    },
  };
}

function completeResult(): FactorResearchResult {
  const result = baseResult();
  result.protocol_review = {
    schema_version: 'factor-research-protocol-review/v1',
    protocol_id: `fproto_${'a'.repeat(32)}`,
    version: 1,
    payload_digest: '1'.repeat(64),
    question: '动量是否在样本外稳定？',
    hypothesis: '方向调整后的 RankIC 为正。',
    passed: true,
    checks: [{
      metric: 'rank_ic_mean',
      operator: '>=',
      threshold: 0,
      actual: 0.02,
      passed: true,
    }],
    export_rules: {
      allow_strategy_export: true,
      require_all_thresholds: true,
      require_dataset_consistency: true,
      minimum_evidence_runs: 1,
    },
    read_only: true,
  };
  result.stability = {
    schema_version: 'factor-research-stability/v1',
    design: {
      mode: 'fixed_three_way',
      pre_registered: true,
      locked_declared_before_run: true,
      parameter_policy: 'fixed',
      factor_data_policy: 'windowed',
      fit_policy: 'window_only',
      forward_return_policy: 'no_cross_window',
      aggregation_policy: 'separate',
    },
    windows: (['train', 'validation', 'locked'] as const).map((role) => ({
      role,
      requested_start: '2016-01-04',
      requested_end: '2018-12-31',
      actual_start: '2016-01-04',
      actual_end: '2018-12-31',
      sessions: 700,
      minimum_sessions: 60,
      horizons: {
        '5': {
          ic: {
            series: [],
            summary: {
              pearson_ic: {
                count: 600,
                mean: 0.02,
                std: 0.1,
                icir: 0.2,
                positive_ratio: 0.55,
                t_stat: 2,
              },
              rank_ic: {
                count: 600,
                mean: 0.02,
                std: 0.1,
                icir: 0.2,
                positive_ratio: 0.55,
                t_stat: 2,
              },
            },
          },
          multiple_testing: {
            raw_approx_p_value: 0.01,
            adjusted_p_value: 0.02,
            passes_adjusted_alpha: true,
          },
        },
      },
      quantile_returns: {
        mean_group_returns: {},
        long_short: {
          count: 600,
          mean: 0.001,
          std: 0.01,
          icir: 0.1,
          positive_ratio: 0.55,
          t_stat: 2,
        },
        monotonicity: 0.8,
      },
      decay: { points: [] },
      coverage: {
        factor_dates: 700,
        valid_factor_dates: 690,
        evaluable_primary_dates: 680,
        minimum_evaluable_primary_dates: 60,
        primary_evaluation_ratio: 0.97,
      },
    })),
    stability_summary: {
      primary_horizon: 5,
      rank_ic_means: [0.02, 0.02, 0.02],
      rank_ic_irs: [0.2, 0.2, 0.2],
      long_short_means: [0.001, 0.001, 0.001],
      rank_ic_mean_range: 0,
      rank_ic_sign_consistent: true,
      locked_minus_validation_rank_ic: 0,
      windows_with_evaluable_primary_ic: 3,
    },
    multiple_testing: {
      hypotheses_tested: 2,
      correction: 'bonferroni',
      alpha: 0.05,
      adjusted_alpha: 0.025,
      p_value_method: 'normal',
      interpretation: '校正后解释。',
    },
    warnings: [],
  };
  result.implementation = {
    schema_version: 'factor-research-implementation/v1',
    status: 'available',
    assumptions: {
      rebalance_interval_sessions: 5,
      return_horizon_sessions: 5,
      default_cost_bps: 10,
      cost_scenarios_bps: [0, 10],
      cost_convention: 'one_way_turnover',
      capacity_participation_rates: [0.01],
      capacity_currency: 'CNY',
    },
    coverage: {
      sampled_rebalance_dates: 500,
      evaluated_rebalance_dates: 490,
      evaluable_observations: 100000,
      possible_observations: 110000,
      evaluation_ratio: 0.91,
      tradable: {
        status: 'available',
        reason: null,
        positive_amount_observations: 100000,
        amount_observations: 100000,
        ratio: 1,
      },
    },
    gross: {
      mean_group_returns: {},
      long_short: { count: 490, mean: 0.002, min: -0.1, max: 0.1 },
    },
    net_default: {
      cost_bps: 10,
      mean_group_returns: {},
      long_short: { count: 490, mean: 0.001, min: -0.1, max: 0.1 },
    },
    cost_sensitivity: [],
    turnover: {
      series: [],
      long_short: { count: 490, mean: 0.2, min: 0, max: 1 },
      mean_group_turnover: {},
    },
    capacity: {
      status: 'available',
      reason: null,
      amount_field: 'amount',
      available_rebalance_dates: 490,
      total_rebalance_dates: 490,
      scenarios: {},
    },
  };
  return result;
}

describe('factor result diagnosis workbench', () => {
  afterEach(() => cleanup());

  it('fails closed when protocol, stability and implementation evidence are absent', async () => {
    const onNavigate = vi.fn();
    render(<FactorResultWorkbench result={baseResult()} onNavigate={onNavigate} />);

    expect(screen.getByText('证据不完整，暂缓导出')).not.toBeNull();
    expect(screen.getByText(/补齐前不能把本次结果解释为可交易因子/)).not.toBeNull();
    expect(screen.getAllByText('待补证据')).toHaveLength(4);

    await userEvent.setup().click(screen.getByRole('button', {
      name: '补齐证据：样本外稳定性',
    }));
    expect(onNavigate).toHaveBeenCalledWith('factor-stability-results');
  });

  it('blocks advancement for failed locked evidence and negative cost-adjusted return', () => {
    const result = completeResult();
    if (!result.stability || !result.implementation || !result.protocol_review) {
      throw new Error('test fixture incomplete');
    }
    result.stability.stability_summary.rank_ic_sign_consistent = false;
    const locked = result.stability.windows.find((window) => window.role === 'locked');
    if (!locked) throw new Error('locked test window missing');
    locked.horizons['5'].ic.summary.rank_ic.mean = -0.01;
    locked.horizons['5'].multiple_testing.passes_adjusted_alpha = false;
    result.implementation.net_default.long_short.mean = -0.0005;
    result.protocol_review.passed = false;
    result.protocol_review.checks[0].passed = false;

    const diagnosis = diagnoseFactorResult(result);
    expect(diagnosis.decision).toBe('blocked');
    expect(diagnosis.checks.find((check) => check.id === 'stability')?.summary)
      .toContain('不要用锁定窗继续调参');
    expect(diagnosis.checks.find((check) => check.id === 'implementation')?.summary)
      .toContain('未保留正向收益');

    render(<FactorResultWorkbench result={result} onNavigate={vi.fn()} />);
    expect(screen.getByText('当前证据不支持进入策略池')).not.toBeNull();
    expect(screen.getAllByText('阻断晋级')).toHaveLength(3);
  });

  it('labels complete positive evidence only as a candidate for human review', () => {
    const diagnosis = diagnoseFactorResult(completeResult());
    expect(diagnosis.decision).toBe('candidate');
    expect(diagnosis.checks.every((check) => check.status === 'passed')).toBe(true);

    render(<FactorResultWorkbench result={completeResult()} onNavigate={vi.fn()} />);
    expect(screen.getByText('可进入下一轮人工复核')).not.toBeNull();
    expect(screen.getByText(/不代表已证明因果关系、未来收益或实盘适用性/)).not.toBeNull();
    expect(screen.getAllByText('证据齐全')).toHaveLength(5);
  });
});
