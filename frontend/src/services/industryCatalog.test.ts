import { describe, expect, it } from 'vitest';
import {
  canSubmitIndustries,
  isHumanReadableIndustryName,
  parseIndustryCatalog,
  partitionIndustrySelection,
} from './industryCatalog';
import type { IndustryCatalogState } from './industryCatalog';

const READY_FIXTURE = {
  schema_version: 'industry-catalog/v2',
  classification: 'eastmoney',
  industries: [
    { code: 'BK0477', name: '银行' },
    { code: 'BK0428', name: '证券' },
    { code: 'BK0733', name: '生物医药' },
  ],
  count: 3,
  source: 'akshare',
  filterable: true,
  mapped_stocks: 4200,
  requested_stocks: 300,
  requested_mapped_stocks: 258,
  map_coverage: 0.86,
  coverage_scope: 'requested_codes',
  minimum_coverage: 0.85,
};

describe('isHumanReadableIndustryName', () => {
  it('rejects BK codes, bare numeric codes and empty names', () => {
    expect(isHumanReadableIndustryName('BK0477', 'BK0477')).toBe(false);
    expect(isHumanReadableIndustryName('bk0477', 'BK0477')).toBe(false);
    expect(isHumanReadableIndustryName('801010', '801010')).toBe(false);
    expect(isHumanReadableIndustryName('', 'BK0477')).toBe(false);
    expect(isHumanReadableIndustryName('   ', 'BK0477')).toBe(false);
  });

  it('accepts genuine human-readable names', () => {
    expect(isHumanReadableIndustryName('银行', 'BK0477')).toBe(true);
    expect(isHumanReadableIndustryName('农林牧渔', '801010')).toBe(true);
    expect(isHumanReadableIndustryName('Industrials', 'GICS-20')).toBe(true);
  });
});

describe('parseIndustryCatalog', () => {
  it('parses a valid v2 payload with provenance', () => {
    const state = parseIndustryCatalog(READY_FIXTURE);
    expect(state.status).toBe('ready');
    if (state.status !== 'ready') return;
    expect(state.entries).toHaveLength(3);
    expect(state.invalidCount).toBe(0);
    expect(state.meta.source).toBe('akshare');
    expect(state.meta.mapCoverage).toBe(0.86);
  });

  it('never exposes BK codes as industry names (production bug regression)', () => {
    const state = parseIndustryCatalog({
      ...READY_FIXTURE,
      classification: 'eastmoney',
      industries: [
        { code: 'BK0477', name: 'BK0477' },
        { code: 'BK0428', name: 'BK0428' },
        { code: 'BK0733', name: '生物医药' },
      ],
      count: 3,
    });
    expect(state.status).toBe('ready');
    if (state.status !== 'ready') return;
    expect(state.entries).toEqual([{ code: 'BK0733', name: '生物医药' }]);
    expect(state.invalidCount).toBe(2);
    expect(state.entries.every((entry) => !/^bk\d+$/i.test(entry.name))).toBe(true);
  });

  it('fails closed when every entry is malformed', () => {
    const state = parseIndustryCatalog({
      ...READY_FIXTURE,
      classification: 'eastmoney',
      industries: [
        { code: 'BK0477', name: 'BK0477' },
        { code: 'BK0428', name: 'BK0428' },
      ],
    });
    expect(state.status).toBe('unavailable');
    if (state.status !== 'unavailable') return;
    expect(state.reason).toContain('无法校验');
    expect(state.invalidCount).toBe(2);
  });

  it('respects filterable=false with the server-provided reason', () => {
    const state = parseIndustryCatalog({
      ...READY_FIXTURE,
      filterable: false,
      reason: '行业映射覆盖率不足，暂停筛选。',
    });
    expect(state.status).toBe('unavailable');
    if (state.status !== 'unavailable') return;
    expect(state.reason).toBe('行业映射覆盖率不足，暂停筛选。');
  });

  it('fails closed for a legacy v1 payload without pool-scoped readiness', () => {
    const state = parseIndustryCatalog({
      classification: 'eastmoney',
      industries: [
        { code: '801010', name: '农林牧渔' },
        { code: '801780', name: '银行' },
      ],
      source: 'fallback (offline)',
    });
    expect(state.status).toBe('unavailable');
    if (state.status !== 'unavailable') return;
    expect(state.reason).toContain('v2');
    expect(state.meta.schemaVersion).toBeNull();
  });

  it('fails closed when coverage is below the admission minimum', () => {
    const state = parseIndustryCatalog({
      ...READY_FIXTURE,
      map_coverage: 0.42,
      minimum_coverage: 0.8,
    });
    expect(state.status).toBe('unavailable');
    if (state.status !== 'unavailable') return;
    expect(state.reason).toContain('覆盖证据');
  });

  it('fails closed when explicit filterability lacks a pool coverage scope', () => {
    const state = parseIndustryCatalog({
      ...READY_FIXTURE,
      coverage_scope: 'not_evaluated',
      requested_stocks: 0,
      requested_mapped_stocks: 0,
      map_coverage: null,
    });
    expect(state.status).toBe('unavailable');
    if (state.status !== 'unavailable') return;
    expect(state.reason).toContain('覆盖证据');
  });

  it('fails closed when coverage disagrees with the requested pool counts', () => {
    const state = parseIndustryCatalog({
      ...READY_FIXTURE,
      requested_mapped_stocks: 294,
      map_coverage: 0.86,
    });
    expect(state.status).toBe('unavailable');
    if (state.status !== 'unavailable') return;
    expect(state.reason).toContain('覆盖证据');
  });

  it('degrades structurally invalid payloads to unavailable', () => {
    expect(parseIndustryCatalog(null).status).toBe('unavailable');
    expect(parseIndustryCatalog('broken').status).toBe('unavailable');
    expect(parseIndustryCatalog({ industries: 'not-an-array' }).status).toBe('unavailable');
  });
});

describe('partitionIndustrySelection', () => {
  const ready = parseIndustryCatalog(READY_FIXTURE);

  it('splits inherited selections into valid and invalid', () => {
    const partition = partitionIndustrySelection(['银行', '不存在的行业', '证券'], ready);
    expect(partition.valid).toEqual(['银行', '证券']);
    expect(partition.invalid).toEqual(['不存在的行业']);
  });

  it('marks every selection unverifiable when the catalog is unavailable', () => {
    const unavailable: IndustryCatalogState = {
      status: 'unavailable',
      reason: 'test',
      invalidCount: 0,
      meta: {
        classification: 'eastmoney',
        schemaVersion: null,
        source: null,
        reason: 'test',
        filterable: false,
        declaredCount: null,
        mappedStocks: null,
        requestedStocks: null,
        requestedMappedStocks: null,
        mapCoverage: null,
        coverageScope: null,
        minimumCoverage: null,
      },
    };
    expect(partitionIndustrySelection(['银行'], unavailable).invalid).toEqual(['银行']);
    expect(partitionIndustrySelection(['银行'], null).invalid).toEqual(['银行']);
  });
});

describe('canSubmitIndustries submit guard', () => {
  const ready = parseIndustryCatalog(READY_FIXTURE);

  it('allows an empty selection (means all industries)', () => {
    expect(canSubmitIndustries([], null).ok).toBe(true);
    expect(canSubmitIndustries([], ready).ok).toBe(true);
  });

  it('fails closed when selections exist but the catalog is not ready', () => {
    const guard = canSubmitIndustries(['银行'], null);
    expect(guard.ok).toBe(false);
    expect(guard.reason).toContain('无法校验');
  });

  it('blocks inherited invalid selections until explicitly cleared', () => {
    const guard = canSubmitIndustries(['银行', 'BK0477'], ready);
    expect(guard.ok).toBe(false);
    expect(guard.reason).toContain('BK0477');
    expect(guard.reason).toContain('清除');
  });

  it('allows fully validated selections', () => {
    expect(canSubmitIndustries(['银行', '证券'], ready).ok).toBe(true);
  });
});
