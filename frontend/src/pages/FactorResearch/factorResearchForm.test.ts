import { describe, expect, it } from 'vitest';
import type {
  FactorCacheCapability,
  FactorDefinition,
  FactorResearchReadiness,
} from '../../services/factorResearch';
import {
  defaultResearchStart,
  firstAvailableFactor,
  firstReadyPool,
  parseResearchHorizons,
  researchConfigEquals,
} from './factorResearchForm';

const legacyPool: FactorCacheCapability = {
  pool_id: 'csi300',
  label: 'CSI300',
  ready: false,
  disabled_reason: 'legacy_or_unverified_schema',
  date_start: '2015-01-01',
  date_end: '2026-07-30',
  n_dates: 1,
  n_stocks: 300,
  fields: ['close'],
  available_factor_ids: ['momentum_20'],
  schema_version: 3,
  source_trust: 'unverified',
  source_providers: [],
  source_evidence_levels: [],
  ready_for_unbiased_research: false,
  neutralization_ready: false,
  point_in_time: {
    schema_version: 'point-in-time-readiness/v1',
    ready: false,
    universe: { ready: false, reason: 'effective_dated_history_missing' },
    security_master: { ready: false, reason: 'effective_dated_history_missing' },
    industry: {
      ready: false,
      neutralization_ready: false,
      reason: 'current_snapshot_not_valid_for_historical_research',
    },
    limitations: ['effective_dated_history_missing'],
  },
};

describe('factor research form safety', () => {
  it('parses, sorts and bounds custom horizons', () => {
    expect(parseResearchHorizons('20, 1，5')).toEqual([1, 5, 20]);
    expect(parseResearchHorizons('1,1')).toBeNull();
    expect(parseResearchHorizons('0,5')).toBeNull();
    expect(parseResearchHorizons('253')).toBeNull();
    expect(parseResearchHorizons(Array.from({ length: 13 }, (_, index) => index + 1).join(',')))
      .toBeNull();
  });

  it('never selects an unavailable legacy pool as the default', () => {
    const trustedPool = {
      ...legacyPool,
      pool_id: 'csi500',
      ready: true,
      disabled_reason: null,
      schema_version: 4,
      source_trust: 'public_cross_validated_research_only',
    };
    const readiness = {
      schema_version: 'factor-research-readiness/v1',
      ready: true,
      pools: [legacyPool, trustedPool],
      limits: {
        max_horizons: 12,
        max_horizon: 252,
        max_window_days: 3653,
        quantiles: { min: 2, max: 10 },
      },
    } satisfies FactorResearchReadiness;

    expect(firstReadyPool(readiness)?.pool_id).toBe('csi500');
  });

  it('uses a bounded two-year default and selects only supported factors', () => {
    expect(defaultResearchStart('2020-01-01', '2026-07-30')).toBe('2024-07-30');
    expect(defaultResearchStart('2025-01-01', '2026-07-30')).toBe('2025-01-01');
    const factors = [
      { factor_id: 'momentum_20' },
      { factor_id: 'liquidity_20' },
    ] as FactorDefinition[];
    const pool = {
      ...legacyPool,
      available_factor_ids: ['liquidity_20'],
    };
    expect(firstAvailableFactor(factors, pool)?.factor_id).toBe('liquidity_20');
  });

  it('compares locked protocol configs by meaning rather than JSON key order', () => {
    const local = {
      schema_version: 'factor-research-protocol/v1',
      factor_ids: ['short_reversal_5'],
      implementation: {
        horizons: [1, 5, 20],
        primary_horizon: 5,
        quantiles: 5,
        cost_scenarios_bps: [0, 5, 10, 20],
      },
    };
    const canonicalApi = {
      factor_ids: ['short_reversal_5'],
      implementation: {
        cost_scenarios_bps: [0, 5, 10, 20],
        horizons: [1, 5, 20],
        primary_horizon: 5,
        quantiles: 5,
      },
      schema_version: 'factor-research-protocol/v1',
    };

    expect(researchConfigEquals(local, canonicalApi)).toBe(true);
    expect(researchConfigEquals(local, {
      ...canonicalApi,
      implementation: {
        ...canonicalApi.implementation,
        primary_horizon: 20,
      },
    })).toBe(false);
  });
});
