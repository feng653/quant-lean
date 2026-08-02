// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import DataCenterPage from './DataCenterPage';

const mocks = vi.hoisted(() => ({
  listPools: vi.fn(),
  getDataUpdateStatus: vi.fn(),
  getResearchDataSources: vi.fn(),
  getResearchDataConflicts: vi.fn(),
  triggerResearchDataRefresh: vi.fn(),
  triggerDataUpdate: vi.fn(),
  triggerPitGovernanceRefresh: vi.fn(),
  invalidatePoolCache: vi.fn(),
  refreshIndustryCatalog: vi.fn(),
}));

vi.mock('../../services/data', () => mocks);
vi.mock('../../components/data/useIndustryCatalog', () => ({
  useIndustryCatalog: () => ({
    catalog: {
      status: 'unavailable',
      reason: 'test',
    },
    loading: false,
    error: null,
    retry: vi.fn(),
  }),
}));

describe('DataCenterPage research sources', () => {
  beforeEach(() => {
    mocks.listPools.mockResolvedValue([
      { id: 'csi300', name: '沪深300', count: 300, declared_count: 300 },
    ]);
    mocks.getDataUpdateStatus.mockResolvedValue({
      broker_status: { status: 'failed' },
      research_refresh_status: {
        status: 'completed',
        result: {
          status: 'collection_in_progress',
          collection: {
            run_id: 'run-1', completed_tasks: 320, planned_tasks: 640,
            pending_tasks: 320, calls_this_invocation: 16,
            reconciled_session_count: 42, complete: false,
            failures: {
              task1: {
                task: { dataset: 'daily_basic' },
                diagnostic: { code: 'provider_rate_limited', retryable: true },
              },
            },
            optional_failures: [{
              task: { dataset: 'index_daily' },
              diagnostic: { code: 'provider_lag', retryable: true },
            }],
          },
        },
      },
      market_data_update_contract: { available: false },
      research_data_contract: {
        available: true, classification: 'vendor_research_trusted',
        research_trust_profile: 'tushare_research_trusted',
        allowed_uses: ['exploratory_research', 'paper_simulation'],
        risk_policy: 'warning_only', live_eligible: false,
        market: { available: true, date_start: '2016-01-04', date_end: '2020-04-22', row_count: 3406397 },
      },
      research_pools: [{
        pool_id: 'csi300', available: true, record_count: 300,
        requested_as_of: '2026-08-02', resolved_month: '2026-06', generation_id: 'abc',
        classification: 'vendor_research_trusted', warnings: ['monthly_snapshot'], live_eligible: false,
      }],
      pools_cache: [{
        pool_id: 'csi300', exists: false, date_start: null, date_end: null,
        n_dates: 0, n_stocks: 0, file_size_mb: 0, last_updated: null,
      }],
    });
    mocks.getResearchDataSources.mockResolvedValue({
      schema_version: 'research-data-sources/v1',
      mode: 'research_and_paper_warning_only',
      live_trading_policy: 'hard_locked',
      sources: [{
        source_id: 'tushare', display_name: 'Tushare Pro', installed: true,
        configured: true, available: true, refreshable: true,
        classification: 'vendor_research_trusted', capabilities: ['historical_index_membership'],
        last_observation: '2026-06', row_count: 327600, generation_id: 'abc',
        warnings: ['monthly_snapshot_not_exact_intramonth_timeline'], live_eligible: false,
        datasets: [{ dataset: 'index_membership', status: 'retained_research_generation', record_count: 327600 }],
      }, {
        source_id: 'baostock', display_name: 'BaoStock', installed: true,
        configured: true, available: true, refreshable: false,
        classification: 'cross_check_only', capabilities: ['daily'],
        last_observation: null, row_count: 0, generation_id: null,
        warnings: ['cross_check_only'], live_eligible: false,
        datasets: [{ dataset: 'daily', status: 'not_retained', record_count: 0 }],
      }],
    });
    mocks.getResearchDataConflicts.mockResolvedValue({
      schema_version: 'research-data-conflicts/v1', status: 'conflicts_observed',
      conflict_count: 1,
      conflicts: [],
      comparisons: [{
        left_source: 'tushare', right_source: 'activated_local', pool_id: 'csi300',
        as_of: '2026-06-30', status: 'conflict', left_count: 300, right_count: 300,
        only_left_count: 1, only_right_count: 1,
        only_left_sample: ['000001'], only_right_sample: ['600000'],
        independent: false, lineage_status: 'same_or_unproven_lineage',
        weight_conflict_count: 1,
        weight_conflict_sample: [{
          security_code: '000001', field: 'weight', left_value: 1.2,
          right_value: 1.1, absolute_delta: 0.1, tolerance: 1e-8,
        }],
      }],
      cross_validated: false,
      uncompared: [{
        left_source: 'tushare', right_source: 'activated_local', pool_id: 'csi300',
        reason: 'not_independently_cross_validated', fields: ['ohlcv'],
      }],
    });
    mocks.triggerResearchDataRefresh.mockResolvedValue({
      job_id: 'job-1', message: '研究数据刷新已提交', mode: 'async_research_data_warning_only',
    });
  });

  afterEach(() => cleanup());

  it('separates usable research data from unavailable production cache', async () => {
    render(<MemoryRouter><DataCenterPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText('Tushare Pro').length).toBeGreaterThan(0));

    expect(screen.getAllByText('研究可用')).toHaveLength(1);
    expect(screen.getByText('跨数据源具体冲突')).toBeTruthy();
    expect(screen.getByText(/仅 tushare：000001/)).toBeTruthy();
    expect(screen.getByText('旧运行时行情缓存诊断')).toBeTruthy();
    expect(screen.getByText('研究行情可用')).toBeTruthy();
    expect(screen.getByText(/000001 权重 1.2/)).toBeTruthy();
    expect(screen.getByText(/未比较：not_independently_cross_validated/)).toBeTruthy();
    expect(screen.getByText('实盘级数据维护已后移')).toBeTruthy();
    expect(screen.getByRole('progressbar', { name: '研究数据实际采集进度' }).getAttribute('aria-valuenow')).toBe('50');
    expect(screen.getByText(/已完成 320 \/ 计划 640/)).toBeTruthy();
    expect(screen.getByText(/daily_basic：provider_rate_limited/)).toBeTruthy();
    expect(screen.getByText(/index_daily=provider_lag/)).toBeTruthy();
  });

  it('submits a bounded Tushare research refresh instead of production update', async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><DataCenterPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText('Tushare Pro').length).toBeGreaterThan(0));
    await user.click(screen.getByRole('button', { name: '拉取并生成研究版本' }));

    await waitFor(() => expect(mocks.triggerResearchDataRefresh).toHaveBeenCalledWith({
      source_id: 'tushare',
      from_month: '2016-01',
      max_calls: 16,
    }));
    expect(mocks.triggerDataUpdate).not.toHaveBeenCalled();
  });
});
