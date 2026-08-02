import { describe, expect, it, vi } from 'vitest';
import type { PortfolioAllocation } from '../types/trading';
import {
  createDeployment,
  normalizeStrategyAnalytics,
  toAllocationPayload,
} from './trading';
import api from './api';

vi.mock('./api', () => ({
  default: { post: vi.fn() },
}));

describe('toAllocationPayload', () => {
  it('removes relational response fields before sending an allocation', () => {
    const responseAllocation = {
      deployment_id: 3,
      target_weight_bps: 2500,
      min_weight_bps: 0,
      max_weight_bps: 5000,
      locked: 0,
      risk_budget_bps: 1200,
      portfolio_id: 2,
      revision: 4,
      display_name: 'Alpha',
      strategy_id: 'alpha_v1',
      deployment_status: 'active',
    };

    expect(toAllocationPayload(
      responseAllocation as unknown as PortfolioAllocation,
    )).toEqual({
      deployment_id: 3,
      target_weight_bps: 2500,
      min_weight_bps: 0,
      max_weight_bps: 5000,
      locked: false,
      risk_budget_bps: 1200,
    });
  });
});

describe('normalizeStrategyAnalytics', () => {
  it('normalizes the backend daily-series contract', () => {
    const result = normalizeStrategyAnalytics({
      portfolio_id: 7,
      date_range: {
        start_date: '2026-07-01',
        end_date: '2026-07-02',
      },
      strategies: [{
        deployment_id: 3,
        display_name: 'Alpha',
        strategy_id: 'alpha_v1',
        metrics: { cumulative_return: 0.02 },
      }],
      series: [{
        date: '2026-07-01',
        portfolio_total_equity: 1_010_000,
        portfolio_daily_return: 0.01,
        strategies: [{
          deployment_id: 3,
          total_equity: 505_000,
          daily_pnl: 5_000,
          daily_return: 0.01,
        }],
      }],
    }, 7);

    expect(result.start_date).toBe('2026-07-01');
    expect(result.end_date).toBe('2026-07-02');
    expect(result.portfolio_series[0]).toMatchObject({
      date: '2026-07-01',
      nav: 1_010_000,
      daily_return: 0.01,
    });
    expect(result.strategies[0].series[0]).toMatchObject({
      date: '2026-07-01',
      equity: 505_000,
      daily_pnl: 5_000,
      daily_return: 0.01,
    });
  });
});

describe('createDeployment', () => {
  it('explains that an atomic deployment was not submitted when the API rejects it', async () => {
    vi.mocked(api.post).mockRejectedValueOnce({
      isAxiosError: true,
      response: {
        status: 409,
        data: {
          detail: {
            code: 'promotion_not_approved',
            message: 'Research promotion was revoked',
          },
        },
        headers: { 'x-request-id': '0226c4963775498d9547bca7fc8d38bf' },
      },
    });

    await expect(createDeployment({
      strategy_id: 'factor_momentum_v1',
      display_name: 'test',
      params: {},
      mode: 'batch',
    })).rejects.toThrow('部署未提交');
  });
});
