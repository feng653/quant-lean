import { useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import type { IndustryCatalogState, IndustryEntry } from '../../services/industryCatalog';
import {
  industryClassificationLabel,
  partitionIndustrySelection,
} from '../../services/industryCatalog';
import Banner from '../shared/Banner';
import Button from '../shared/Button';
import Icon from '../shared/Icon';
import Skeleton from '../shared/Skeleton';

interface IndustryMultiSelectProps {
  catalog: IndustryCatalogState | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onRefresh?: () => void;
  refreshing?: boolean;
  /** Selected industry names (may include inherited, not-yet-validated ones). */
  selected: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

/**
 * Searchable, accessible industry multi-select.
 *
 * Safety contract:
 * - Only catalog-validated, human-readable names are selectable — BK codes
 *   are never rendered as industry names.
 * - `filterable: false` / structurally invalid catalogs render an explicit
 *   unavailable state with provenance and retry, not a disabled-looking
 *   empty box.
 * - Inherited selections that fail validation stay visible in a danger zone
 *   and must be cleared explicitly; they are never silently dropped or
 *   silently submitted.
 */
export default function IndustryMultiSelect({
  catalog,
  loading,
  error,
  onRetry,
  onRefresh,
  refreshing = false,
  selected,
  onChange,
  disabled = false,
}: IndustryMultiSelectProps) {
  const listId = useId();
  const inputId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);

  const ready = catalog?.status === 'ready' ? catalog : null;
  const partition = partitionIndustrySelection(selected, catalog);

  const filtered: IndustryEntry[] = useMemo(() => {
    if (!ready) return [];
    const keyword = query.trim().toLowerCase();
    if (!keyword) return ready.entries;
    return ready.entries.filter(
      (entry) =>
        entry.name.toLowerCase().includes(keyword) || entry.code.toLowerCase().includes(keyword),
    );
  }, [ready, query]);

  const toggle = (name: string) => {
    if (disabled) return;
    if (selected.includes(name)) {
      onChange(selected.filter((item) => item !== name));
    } else {
      onChange([...selected, name]);
    }
  };

  const removeName = (name: string) => {
    onChange(selected.filter((item) => item !== name));
  };

  const clearInvalid = () => {
    onChange(partition.valid);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (disabled) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => Math.min(index + 1, filtered.length - 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((index) => Math.max(index - 1, 0));
    } else if (event.key === 'Enter') {
      if (open && filtered[activeIndex]) {
        event.preventDefault();
        toggle(filtered[activeIndex].name);
      }
    } else if (event.key === 'Escape') {
      setOpen(false);
    }
  };

  const onBlur = () => {
    // Close only when focus leaves the whole combobox container.
    window.setTimeout(() => {
      if (!containerRef.current?.contains(document.activeElement)) {
        setOpen(false);
      }
    }, 0);
  };

  /* ── Loading ─────────────────────────────────────────────────────────── */
  if (loading) {
    return (
      <div aria-label="行业目录加载中" role="status">
        <Skeleton lines={2} className="h-9 w-full" />
        <p className="mt-2 text-xs text-ink-500">正在加载并校验行业目录...</p>
      </div>
    );
  }

  /* ── Transport / HTTP error ──────────────────────────────────────────── */
  if (error) {
    return (
      <Banner
        variant="danger"
        title="行业目录加载失败"
        action={
          <Button variant="secondary" size="sm" onClick={onRetry}>
            <Icon name="refresh" className="h-4 w-4" />
            重试
          </Button>
        }
      >
        {error}。行业筛选不可用；{selected.length > 0 ? '已选择的行业无法校验，提交将被阻止。' : '不选择行业仍可提交（表示使用全部行业）。'}
      </Banner>
    );
  }

  /* ── Unavailable catalog (filterable=false or invalid payload) ───────── */
  if (!ready) {
    const unavailableReason = catalog?.status === 'unavailable' ? catalog.reason : '行业目录不可用。';
    return (
      <div className="rounded border border-warn-border bg-warn-bg p-3.5">
        <div className="flex items-start gap-2.5">
          <Icon name="warning" className="mt-0.5 h-5 w-5 shrink-0 text-warn-strong" aria-hidden />
          <div className="min-w-0 flex-1 text-sm leading-6 text-warn-strong">
            <p className="font-semibold">行业筛选不可用</p>
            <p>{unavailableReason}</p>
            {catalog?.status === 'unavailable' && (
              <p className="mt-1 text-xs">
                分类：{industryClassificationLabel(catalog.meta.classification)}
                {catalog.meta.source ? ` · 来源：${catalog.meta.source}` : ''}
              </p>
            )}
            <p className="mt-1 text-xs">
              不选择行业仍可提交（表示使用全部行业）；已选择的行业必须显式清除。
            </p>
          </div>
          <div className="flex shrink-0 flex-col gap-2">
            {onRefresh && (
              <Button
                variant="secondary"
                size="sm"
                onClick={onRefresh}
                loading={refreshing}
              >
                <Icon name="refresh" className="h-4 w-4" />
                联网补全
              </Button>
            )}
            <Button variant="secondary" size="sm" onClick={onRetry}>
              重试本地缓存
            </Button>
          </div>
        </div>
        {selected.length > 0 && (
          <InvalidSelectionZone invalid={partition.invalid} onRemove={removeName} onClearAll={clearInvalid} />
        )}
      </div>
    );
  }

  /* ── Ready: searchable combobox ──────────────────────────────────────── */
  const activeOptionId = open && filtered[activeIndex] ? `${listId}-opt-${activeIndex}` : undefined;

  return (
    <div>
      <div ref={containerRef} className="relative" onBlur={onBlur}>
        <div className="relative">
          <Icon
            name="search"
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-400"
            aria-hidden
          />
          <input
            id={inputId}
            type="text"
            role="combobox"
            aria-expanded={open}
            aria-controls={listId}
            aria-activedescendant={activeOptionId}
            aria-autocomplete="list"
            aria-label="搜索并选择行业"
            disabled={disabled}
            value={query}
            placeholder="搜索行业名称或代码..."
            onChange={(event) => {
              setQuery(event.target.value);
              setOpen(true);
              setActiveIndex(0);
            }}
            onFocus={() => setOpen(true)}
            onKeyDown={onKeyDown}
            className="block w-full rounded border border-ink-300 bg-surface py-2 pl-9 pr-3 text-sm text-ink-900 placeholder-ink-400 focus:border-accent-600 focus:outline-none focus:ring-2 focus:ring-accent-600/30 disabled:cursor-not-allowed disabled:bg-ink-100"
          />
        </div>
        {open && !disabled && (
          <div
            id={listId}
            role="listbox"
            aria-multiselectable="true"
            aria-label="可选行业"
            className="absolute z-30 mt-1 max-h-64 w-full overflow-y-auto rounded border border-ink-200 bg-surface py-1 shadow-menu scrollbar-thin"
          >
            {filtered.length === 0 ? (
              <p className="px-3 py-2 text-sm text-ink-400">没有匹配的行业</p>
            ) : (
              filtered.map((entry, index) => {
                const checked = selected.includes(entry.name);
                return (
                  <div
                    key={entry.code}
                    id={`${listId}-opt-${index}`}
                    role="option"
                    aria-selected={checked}
                    tabIndex={-1}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      toggle(entry.name);
                    }}
                    onMouseEnter={() => setActiveIndex(index)}
                    className={`flex cursor-pointer items-center gap-2.5 px-3 py-2 text-sm ${
                      index === activeIndex ? 'bg-accent-50' : ''
                    } ${checked ? 'font-medium text-accent-900' : 'text-ink-700'}`}
                  >
                    <span
                      aria-hidden
                      className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border ${
                        checked ? 'border-accent-700 bg-accent-700 text-white' : 'border-ink-300 bg-surface'
                      }`}
                    >
                      {checked && <Icon name="check" className="h-3 w-3" />}
                    </span>
                    <span className="flex-1 truncate">{entry.name}</span>
                    <span className="font-mono text-2xs text-ink-400">{entry.code}</span>
                  </div>
                );
              })
            )}
          </div>
        )}
      </div>

      {/* Valid selections */}
      {partition.valid.length > 0 && (
        <ul aria-label="已选择的行业" className="mt-2 flex flex-wrap gap-1.5">
          {partition.valid.map((name) => (
            <li
              key={name}
              className="inline-flex items-center gap-1.5 rounded-sm border border-accent-200 bg-accent-50 py-0.5 pl-2 pr-1 text-xs font-medium text-accent-800"
            >
              {name}
              <button
                type="button"
                aria-label={`移除行业 ${name}`}
                disabled={disabled}
                onClick={() => removeName(name)}
                className="rounded-sm p-0.5 text-accent-700 transition-colors hover:bg-accent-100 disabled:opacity-50"
              >
                <Icon name="close" className="h-3 w-3" />
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Invalid / unverifiable inherited selections — explicit clear only */}
      {partition.invalid.length > 0 && (
        <InvalidSelectionZone invalid={partition.invalid} onRemove={removeName} onClearAll={clearInvalid} />
      )}

      {/* Provenance & coverage */}
      <div className="mt-2 space-y-0.5 text-xs leading-5 text-ink-500">
        <p>
          目录：{industryClassificationLabel(ready.meta.classification)}
          {ready.meta.source ? ` · 来源：${ready.meta.source}` : ''}
          {' · '}可筛选行业 {ready.entries.length} 个
          {ready.meta.declaredCount !== null && ready.meta.declaredCount !== ready.entries.length
            ? `（服务端声明 ${ready.meta.declaredCount} 个）`
            : ''}
        </p>
        {ready.invalidCount > 0 && (
          <p className="text-warn-strong">
            已排除 {ready.invalidCount} 条名称不可读的行业条目（例如板块代码被误作名称），这些条目不可选择。
          </p>
        )}
        {ready.meta.mappedStocks !== null && (
          <p>
            行业映射覆盖 {ready.meta.mappedStocks.toLocaleString('zh-CN')} 只股票
            {ready.meta.mapCoverage !== null
              ? `，覆盖率 ${(ready.meta.mapCoverage * 100).toFixed(1)}%`
              : ''}
            {ready.meta.minimumCoverage !== null
              ? `（准入下限 ${(ready.meta.minimumCoverage * 100).toFixed(0)}%）`
              : ''}
          </p>
        )}
      </div>
      <p className="mt-1 text-xs text-ink-400">不选择表示使用全部行业；可使用键盘上下移动、回车勾选、Esc 关闭列表。</p>
    </div>
  );
}

function InvalidSelectionZone({
  invalid,
  onRemove,
  onClearAll,
}: {
  invalid: string[];
  onRemove: (name: string) => void;
  onClearAll: () => void;
}) {
  if (invalid.length === 0) return null;
  return (
    <div role="alert" className="mt-3 rounded border border-danger-border bg-danger-bg p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="text-sm leading-6 text-danger-strong">
          <p className="font-semibold">
            {invalid.length} 个行业选择无法校验
          </p>
          <p className="text-xs">
            这些选择可能来自继承的配置或目录变更，不会被静默提交。请逐项确认或清除。
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={onClearAll} className="shrink-0">
          清除全部无效选择
        </Button>
      </div>
      <ul aria-label="无法校验的行业选择" className="mt-2 flex flex-wrap gap-1.5">
        {invalid.map((name) => (
          <li
            key={name}
            className="inline-flex items-center gap-1.5 rounded-sm border border-danger-border bg-surface py-0.5 pl-2 pr-1 text-xs font-medium text-danger-fg"
          >
            {name}
            <button
              type="button"
              aria-label={`移除无法校验的行业 ${name}`}
              onClick={() => onRemove(name)}
              className="rounded-sm p-0.5 text-danger-fg transition-colors hover:bg-danger-bg"
            >
              <Icon name="close" className="h-3 w-3" />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
