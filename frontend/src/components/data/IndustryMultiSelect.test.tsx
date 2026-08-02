import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import IndustryMultiSelect from './IndustryMultiSelect';
import { parseIndustryCatalog } from '../../services/industryCatalog';

const READY_CATALOG = parseIndustryCatalog({
  schema_version: 'industry-catalog/v2',
  classification: 'eastmoney',
  industries: [
    { code: 'BK0477', name: '银行' },
    { code: 'BK0428', name: '证券' },
  ],
  count: 2,
  source: 'akshare',
  filterable: true,
  mapped_stocks: 4200,
  requested_stocks: 300,
  requested_mapped_stocks: 258,
  map_coverage: 0.86,
  coverage_scope: 'requested_codes',
  minimum_coverage: 0.85,
});

const noop = () => {};

describe('IndustryMultiSelect', () => {
  it('renders an unavailable state with reason and retry instead of a dead select', () => {
    const catalog = parseIndustryCatalog({
      schema_version: 'industry-catalog/v2',
      classification: 'eastmoney',
      industries: [],
      filterable: false,
      reason: '行业映射覆盖率不足，暂停筛选。',
    });
    const html = renderToStaticMarkup(
      <IndustryMultiSelect
        catalog={catalog}
        loading={false}
        error={null}
        onRetry={noop}
        selected={[]}
        onChange={noop}
      />,
    );
    expect(html).toContain('行业筛选不可用');
    expect(html).toContain('行业映射覆盖率不足，暂停筛选。');
    expect(html).toContain('重试');
    expect(html).not.toContain('role="combobox"');
  });

  it('offers explicit online completion only when the caller is authorized', () => {
    const catalog = parseIndustryCatalog({
      schema_version: 'industry-catalog/v2',
      classification: 'cninfo_008001',
      industries: [],
      filterable: false,
      reason: 'industry_map_coverage_insufficient',
    });
    const html = renderToStaticMarkup(
      <IndustryMultiSelect
        catalog={catalog}
        loading={false}
        error={null}
        onRetry={noop}
        onRefresh={noop}
        refreshing={false}
        selected={[]}
        onChange={noop}
      />,
    );
    expect(html).toContain('联网补全');
    expect(html).toContain('重试本地缓存');
  });

  it('renders the error state with an explicit retry action', () => {
    const html = renderToStaticMarkup(
      <IndustryMultiSelect
        catalog={null}
        loading={false}
        error="网络异常"
        onRetry={noop}
        selected={[]}
        onChange={noop}
      />,
    );
    expect(html).toContain('行业目录加载失败');
    expect(html).toContain('网络异常');
    expect(html).toContain('重试');
  });

  it('renders the loading state accessibly', () => {
    const html = renderToStaticMarkup(
      <IndustryMultiSelect
        catalog={null}
        loading
        error={null}
        onRetry={noop}
        selected={[]}
        onChange={noop}
      />,
    );
    expect(html).toContain('role="status"');
    expect(html).toContain('行业目录加载中');
  });

  it('renders a searchable combobox, selected tags and provenance when ready', () => {
    const html = renderToStaticMarkup(
      <IndustryMultiSelect
        catalog={READY_CATALOG}
        loading={false}
        error={null}
        onRetry={noop}
        selected={['银行']}
        onChange={noop}
      />,
    );
    expect(html).toContain('role="combobox"');
    expect(html).toContain('aria-expanded');
    expect(html).toContain('已选择的行业');
    expect(html).toContain('移除行业 银行');
    expect(html).toContain('东方财富行业');
    expect(html).toContain('akshare');
    expect(html).toContain('可筛选行业 2 个');
    expect(html).toContain('86.0%');
    // The BK board code must never be rendered as an industry name.
    expect(html).not.toContain('>BK0477<');
  });

  it('keeps inherited invalid selections visible and explicitly clearable', () => {
    const html = renderToStaticMarkup(
      <IndustryMultiSelect
        catalog={READY_CATALOG}
        loading={false}
        error={null}
        onRetry={noop}
        selected={['银行', 'BK9999', '不存在的行业']}
        onChange={noop}
      />,
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain('2 个行业选择无法校验');
    expect(html).toContain('清除全部无效选择');
    expect(html).toContain('移除无法校验的行业 BK9999');
    expect(html).toContain('不会被静默提交');
    // The valid part still renders as a normal selection tag.
    expect(html).toContain('移除行业 银行');
  });

  it('shows unverifiable selections when the catalog is unavailable and they must be cleared', () => {
    const catalog = parseIndustryCatalog({
      schema_version: 'industry-catalog/v2',
      classification: 'eastmoney',
      industries: [],
      filterable: false,
      reason: '服务暂不可用。',
    });
    const html = renderToStaticMarkup(
      <IndustryMultiSelect
        catalog={catalog}
        loading={false}
        error={null}
        onRetry={noop}
        selected={['银行']}
        onChange={noop}
      />,
    );
    expect(html).toContain('1 个行业选择无法校验');
    expect(html).toContain('清除全部无效选择');
  });

  it('warns when invalid entries were excluded from an otherwise ready pool catalog', () => {
    const catalog = parseIndustryCatalog({
      schema_version: 'industry-catalog/v2',
      classification: 'eastmoney',
      industries: [
        { code: 'BK0477', name: 'BK0477' },
        { code: 'BK0733', name: '生物医药' },
      ],
      filterable: true,
      mapped_stocks: 1000,
      requested_stocks: 100,
      requested_mapped_stocks: 90,
      map_coverage: 0.9,
      coverage_scope: 'requested_codes',
      minimum_coverage: 0.8,
    });
    const onChange = vi.fn();
    const html = renderToStaticMarkup(
      <IndustryMultiSelect
        catalog={catalog}
        loading={false}
        error={null}
        onRetry={noop}
        selected={[]}
        onChange={onChange}
      />,
    );
    expect(html).toContain('已排除 1 条名称不可读的行业条目');
    expect(onChange).not.toHaveBeenCalled();
  });
});
