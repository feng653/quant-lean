import { describe, expect, it } from 'vitest';
import type { FactorResearchRun } from '../../services/factorResearch';
import {
  FACTOR_WORKBENCH_PRESETS,
  filterAndSortFactorRuns,
} from './factorWorkbench';

function run(
  runId: string,
  factorId: string,
  createdAt: string,
  primaryHorizon: number,
): FactorResearchRun {
  return {
    run_id: runId,
    factor_id: factorId,
    request: { primary_horizon: primaryHorizon } as FactorResearchRun['request'],
    request_digest: 'a',
    dataset_digest: 'b',
    result_digest: 'c',
    run_digest: 'd',
    schema_version: 'factor-research/v4',
    created_at: createdAt,
    archived_at: null,
  };
}

describe('factor workbench presets', () => {
  it('keeps the primary horizon inside every preset horizon list', () => {
    for (const preset of FACTOR_WORKBENCH_PRESETS) {
      const horizons = preset.horizonsText.split(',').map((item) => Number(item.trim()));
      expect(horizons).toContain(preset.primaryHorizon);
      expect(preset.defaultCostBps).toBeGreaterThanOrEqual(0);
    }
  });
});

describe('filterAndSortFactorRuns', () => {
  const runs = [
    run('frun_old_momentum', 'momentum_20', '2026-01-01T00:00:00Z', 20),
    run('frun_new_reversal', 'short_reversal_5', '2026-02-01T00:00:00Z', 5),
    run('frun_new_momentum', 'momentum_20', '2026-03-01T00:00:00Z', 5),
  ];

  it('filters by factor and searches stable identifiers', () => {
    expect(filterAndSortFactorRuns(runs, {
      factorId: 'momentum_20',
      query: 'NEW',
      sort: 'newest',
    }).map((item) => item.run_id)).toEqual(['frun_new_momentum']);
  });

  it('sorts by factor and uses newest time as a deterministic tie break', () => {
    expect(filterAndSortFactorRuns(runs, {
      factorId: '',
      query: '',
      sort: 'factor',
    }).map((item) => item.run_id)).toEqual([
      'frun_new_momentum',
      'frun_old_momentum',
      'frun_new_reversal',
    ]);
  });

  it('sorts by primary horizon without mutating the input', () => {
    const before = runs.map((item) => item.run_id);
    expect(filterAndSortFactorRuns(runs, {
      factorId: '',
      query: '',
      sort: 'horizon',
    }).map((item) => item.run_id)).toEqual([
      'frun_new_momentum',
      'frun_new_reversal',
      'frun_old_momentum',
    ]);
    expect(runs.map((item) => item.run_id)).toEqual(before);
  });
});
