/**
 * Shared ECharts theme tokens — mirrors the Tailwind palette so charts stay
 * consistent with the design system. Signed market numbers follow the
 * A-share convention (red up / green down) and are never used for generic
 * success/failure semantics.
 */

export const CHART_COLORS = {
  accent: '#236866',
  accentLight: '#539e9a',
  ink: '#7d7870',
  inkLight: '#aba69e',
  rise: '#b23a2a',
  fall: '#1e7c5b',
  warn: '#8a5a0b',
  danger: '#a92c22',
  steel: '#2b5ea7',
  ochre: '#a26d1f',
  grid: '#e6e4e0',
  axis: '#d3d0ca',
  axisLabel: '#5c574f',
  split: '#f3f2ef',
} as const;

export const SERIES_PALETTE = [
  CHART_COLORS.accent,
  CHART_COLORS.steel,
  CHART_COLORS.ochre,
  CHART_COLORS.rise,
  CHART_COLORS.ink,
  CHART_COLORS.accentLight,
  CHART_COLORS.fall,
  CHART_COLORS.inkLight,
] as const;

export function baseGrid(overrides: Record<string, unknown> = {}) {
  return {
    left: 12,
    right: 16,
    top: 32,
    bottom: 28,
    containLabel: true,
    ...overrides,
  };
}

export function baseTooltip(overrides: Record<string, unknown> = {}) {
  return {
    trigger: 'axis',
    backgroundColor: '#ffffff',
    borderColor: CHART_COLORS.grid,
    borderWidth: 1,
    textStyle: { color: '#1d1a16', fontSize: 12 },
    axisPointer: { lineStyle: { color: CHART_COLORS.axis } },
    ...overrides,
  };
}

export function baseXAxis(overrides: Record<string, unknown> = {}) {
  return {
    type: 'category',
    axisLine: { lineStyle: { color: CHART_COLORS.axis } },
    axisTick: { show: false },
    axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 11 },
    ...overrides,
  };
}

export function baseYAxis(overrides: Record<string, unknown> = {}) {
  return {
    type: 'value',
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 11 },
    splitLine: { lineStyle: { color: CHART_COLORS.split } },
    ...overrides,
  };
}

export function baseLegend(overrides: Record<string, unknown> = {}) {
  return {
    top: 0,
    right: 0,
    itemWidth: 14,
    itemHeight: 8,
    textStyle: { color: CHART_COLORS.axisLabel, fontSize: 11 },
    ...overrides,
  };
}

/** Format a ratio as a signed percentage string, e.g. 0.0823 → +8.23%. */
export function formatSignedPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  const pct = value * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(digits)}%`;
}

/** Format a ratio as an unsigned percentage string. */
export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  return `${(value * 100).toFixed(digits)}%`;
}

/** Format CNY amounts; large values collapse to 万. */
export function formatCny(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-';
  const abs = Math.abs(value);
  if (abs >= 100_000_000) return `¥${(value / 100_000_000).toFixed(2)}亿`;
  if (abs >= 10_000) return `¥${(value / 10_000).toFixed(1)}万`;
  return `¥${value.toLocaleString('zh-CN', { maximumFractionDigits: digits })}`;
}

/** Class name for signed market numbers (A-share convention: red up, green down). */
export function signedToneClass(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) {
    return 'text-ink-500';
  }
  return value > 0 ? 'text-rise' : 'text-fall';
}
