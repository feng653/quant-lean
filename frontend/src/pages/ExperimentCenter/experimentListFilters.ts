import type { StrategyMetadata } from '../../types/strategy';
import type {
  ExperimentFilters,
  ExperimentSortKey,
  ExperimentSortOrder,
} from '../../services/experiments';

export type StrategyCategoryFilter = StrategyMetadata['category'] | '';

export const strategyCategoryOptions: Array<{
  value: StrategyCategoryFilter;
  label: string;
}> = [
  { value: '', label: '全部分类' },
  { value: 'technical', label: '技术指标' },
  { value: 'ml', label: '机器学习' },
  { value: 'factor', label: '因子策略' },
  { value: 'portfolio', label: '组合管理' },
  { value: 'composite', label: '复合策略' },
];

export const experimentSortOptions: Array<{
  value: ExperimentSortKey;
  label: string;
}> = [
  { value: 'created_at', label: '创建时间' },
  { value: 'annual_return', label: '年化收益' },
  { value: 'sharpe_ratio', label: 'Sharpe 比率' },
  { value: 'max_drawdown', label: '最大回撤' },
  { value: 'strategy_id', label: '策略 ID' },
  { value: 'status', label: '状态' },
];

export const experimentSortOrderOptions: Array<{
  value: ExperimentSortOrder;
  label: string;
}> = [
  { value: 'desc', label: '降序' },
  { value: 'asc', label: '升序' },
];

const experimentSortKeys = new Set(
  experimentSortOptions.map((option) => option.value),
);

export function parseExperimentSortKey(
  value: string | null,
): ExperimentSortKey {
  return value && experimentSortKeys.has(value as ExperimentSortKey)
    ? value as ExperimentSortKey
    : 'created_at';
}

export function parseExperimentSortOrder(
  value: string | null,
): ExperimentSortOrder {
  return value === 'asc' ? 'asc' : 'desc';
}

export function buildExperimentSortSearchParams(
  current: URLSearchParams,
  sortBy: ExperimentSortKey,
  sortOrder: ExperimentSortOrder,
): URLSearchParams {
  const next = new URLSearchParams(current);
  if (sortBy === 'created_at') {
    next.delete('sort_by');
  } else {
    next.set('sort_by', sortBy);
  }
  if (sortOrder === 'desc') {
    next.delete('sort_order');
  } else {
    next.set('sort_order', sortOrder);
  }
  return next;
}

export function filterStrategiesByCategory(
  strategies: StrategyMetadata[],
  category: StrategyCategoryFilter,
): StrategyMetadata[] {
  if (!category) return strategies;
  return strategies.filter((strategy) => strategy.category === category);
}

export function reconcileStrategySelection(
  strategyId: string,
  category: StrategyCategoryFilter,
  strategies: StrategyMetadata[],
): string {
  if (!strategyId || !category) return strategyId;
  const selectedStrategy = strategies.find(
    (strategy) => strategy.strategy_id === strategyId,
  );
  if (!selectedStrategy) return strategyId;
  return selectedStrategy.category === category ? strategyId : '';
}

export function buildExperimentListFilters({
  strategyCategory,
  strategyId,
  status,
  starredOnly,
  search,
  sortBy,
  sortOrder,
  page,
  limit,
}: {
  strategyCategory: StrategyCategoryFilter;
  strategyId: string;
  status: string;
  starredOnly: boolean;
  search: string;
  sortBy: ExperimentSortKey;
  sortOrder: ExperimentSortOrder;
  page: number;
  limit: number;
}): ExperimentFilters {
  return {
    strategy_category: strategyCategory || undefined,
    strategy_id: strategyId || undefined,
    status: status || undefined,
    is_starred: starredOnly || undefined,
    search: search || undefined,
    sort_by: sortBy,
    sort_order: sortOrder,
    page,
    limit,
  };
}
