import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { FactorStabilityResult } from '../../services/factorResearch';
import FactorStabilityConfigPanel from './FactorStabilityConfig';
import {
  defaultStabilityConfig,
  validateStabilityConfig,
} from './factorStabilityForm';
import FactorStabilityResults from './FactorStabilityResults';

describe('factor stability configuration', () => {
  it('pre-registers ordered windows and requires an explicit locked declaration', () => {
    const config = defaultStabilityConfig('2024-01-01', '2025-12-31');
    expect(config.train.start).toBe('2024-01-01');
    expect(config.train.end < config.validation.start).toBe(true);
    expect(config.validation.end < config.locked.start).toBe(true);
    expect(config.locked.end).toBe('2025-12-31');
    expect(validateStabilityConfig(config, '2024-01-01', '2025-12-31'))
      .toBe('提交前必须声明锁定窗');

    config.locked_declared = true;
    expect(validateStabilityConfig(config, '2024-01-01', '2025-12-31')).toBeNull();
  });

  it('renders the disabled empty state and leakage policy', () => {
    const disabled = renderToStaticMarkup(
      <FactorStabilityConfigPanel
        value={null}
        researchStart="2024-01-01"
        researchEnd="2025-12-31"
        onChange={() => undefined}
      />,
    );
    expect(disabled).toContain('本次不生成分窗样本外证据');

    const config = defaultStabilityConfig('2024-01-01', '2025-12-31');
    const enabled = renderToStaticMarkup(
      <FactorStabilityConfigPanel
        value={{ ...config, locked_declared: true }}
        researchStart="2024-01-01"
        researchEnd="2025-12-31"
        onChange={() => undefined}
      />,
    );
    expect(enabled).toContain('每个窗口在计算前瞻收益前按窗口结束日截断');
    expect(enabled).toContain('已检验假设总数');
  });
});

describe('factor stability results', () => {
  it('shows window metrics, decay, coverage and conservative interpretation', () => {
    const summary = {
      count: 63,
      mean: 0.04,
      std: 0.1,
      icir: 0.4,
      positive_ratio: 0.6,
      t_stat: 3.1,
    };
    const result = {
      schema_version: 'factor-stability/v1',
      design: {
        mode: 'fixed_three_way',
        pre_registered: true,
        locked_declared_before_run: true,
        parameter_policy: 'fixed',
        factor_data_policy: 'visible rows only',
        fit_policy: 'per date',
        forward_return_policy: 'truncated',
        aggregation_policy: 'not pooled',
      },
      windows: (['train', 'validation', 'locked'] as const).map((role) => ({
        role,
        requested_start: '2024-01-01',
        requested_end: '2024-12-31',
        actual_start: '2024-01-02',
        actual_end: '2024-12-31',
        sessions: role === 'train' ? 252 : 63,
        minimum_sessions: role === 'train' ? 252 : 63,
        horizons: {
          '5': {
            ic: {
              series: [],
              summary: { pearson_ic: summary, rank_ic: summary },
            },
            multiple_testing: {
              raw_approx_p_value: 0.002,
              adjusted_p_value: 0.04,
              passes_adjusted_alpha: true,
            },
          },
        },
        quantile_returns: {
          mean_group_returns: { '1': -0.01, '5': 0.02 },
          long_short: summary,
          monotonicity: 0.9,
        },
        decay: {
          points: [{ horizon: 5, pearson_ic: summary, rank_ic: summary }],
        },
        coverage: {
          factor_dates: 63,
          valid_factor_dates: 63,
          evaluable_primary_dates: 58,
          minimum_evaluable_primary_dates: 42,
          primary_evaluation_ratio: 58 / 63,
        },
      })),
      stability_summary: {
        primary_horizon: 5,
        rank_ic_means: [0.04, 0.03, 0.02],
        rank_ic_irs: [0.4, 0.3, 0.2],
        long_short_means: [0.04, 0.03, 0.02],
        rank_ic_mean_range: 0.02,
        rank_ic_sign_consistent: true,
        locked_minus_validation_rank_ic: -0.01,
        windows_with_evaluable_primary_ic: 3,
      },
      multiple_testing: {
        hypotheses_tested: 20,
        correction: 'bonferroni',
        alpha: 0.05,
        adjusted_alpha: 0.0025,
        p_value_method: 'normal approximation',
        interpretation: '统计显著性不证明因子有效。',
      },
      warnings: ['指标不得跨窗混算。'],
    } satisfies FactorStabilityResult;

    const html = renderToStaticMarkup(
      <FactorStabilityResults stability={result} configured />,
    );
    expect(html).toContain('预注册样本外稳定性');
    expect(html).toContain('预先锁定');
    expect(html).toContain('RankIC 衰减分窗');
    expect(html).toContain('Bonferroni');
    expect(html).toContain('统计显著性不证明因子有效');
  });

  it('keeps legacy runs readable with an explicit empty state', () => {
    const html = renderToStaticMarkup(
      <FactorStabilityResults stability={undefined} configured={false} />,
    );
    expect(html).toContain('本次运行未启用预注册样本外评估');
  });
});
