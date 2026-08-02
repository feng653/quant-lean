import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { FactorCacheCapability } from '../../services/factorResearch';
import NeutralizationConfig from './NeutralizationConfig';
import { neutralizationUnavailableReason } from './neutralizationForm';

function pool(overrides: Partial<FactorCacheCapability> = {}): FactorCacheCapability {
  return {
    pool_id: 'csi300',
    label: 'CSI300',
    ready: true,
    disabled_reason: null,
    date_start: '2024-01-01',
    date_end: '2024-12-31',
    n_dates: 200,
    n_stocks: 300,
    fields: ['close'],
    available_factor_ids: ['momentum_20'],
    schema_version: 4,
    source_trust: 'licensed',
    source_providers: ['fixture'],
    source_evidence_levels: ['licensed'],
    ready_for_unbiased_research: false,
    neutralization_ready: false,
    neutralization: {
      schema_version: 'factor-neutralization-readiness/v1',
      modes: {
        none: { ready: true, reason: null },
        industry: {
          ready: false,
          reason: 'current_snapshot_not_valid_for_historical_research',
        },
        size: {
          ready: false,
          reason: 'point_in_time_size_provenance_missing',
        },
        'industry+size': {
          ready: false,
          reason: 'current_snapshot_not_valid_for_historical_research',
        },
      },
      industry: {
        ready: false,
        reason: 'current_snapshot_not_valid_for_historical_research',
        query_semantics: 'one_verified_as_of_query_per_trading_date',
      },
      size: {
        schema_version: 'factor-neutralization-readiness/v1',
        ready: false,
        reason: 'point_in_time_size_provenance_missing',
        selected_field: null,
        available_fields: [],
        required_provenance_schema: 'point-in-time-field-provenance/v1',
      },
    },
    point_in_time: {
      schema_version: 'point-in-time-readiness/v1',
      ready: false,
      universe: { ready: false, reason: 'effective_dated_history_missing' },
      security_master: { ready: false, reason: 'effective_dated_history_missing' },
      industry: {
        ready: false,
        reason: 'current_snapshot_not_valid_for_historical_research',
      },
      limitations: [],
    },
    ...overrides,
  };
}

describe('factor neutralization config', () => {
  it('disables unavailable PIT modes with a safe explanation', () => {
    const html = renderToStaticMarkup(
      <NeutralizationConfig pool={pool()} value="none" onChange={() => undefined} />,
    );
    expect(html).toContain('只有当前行业快照，不能回填历史');
    expect(html).toContain('市值字段缺少点时 provenance');
    expect(html.match(/disabled=""/g)?.length).toBe(3);
    expect(neutralizationUnavailableReason(pool(), 'industry')).toContain('当前行业快照');
  });

  it('enables only modes explicitly proven ready', () => {
    const ready = pool();
    if (!ready.neutralization) throw new Error('fixture missing readiness');
    ready.neutralization.modes.industry = { ready: true, reason: null };
    ready.neutralization.industry.ready = true;
    const html = renderToStaticMarkup(
      <NeutralizationConfig pool={ready} value="industry" onChange={() => undefined} />,
    );
    expect(neutralizationUnavailableReason(ready, 'industry')).toBeNull();
    expect(html).toContain('逐交易日查询 PIT 行业');
  });
});
