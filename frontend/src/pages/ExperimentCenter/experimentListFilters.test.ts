import { describe, expect, it } from 'vitest';
import type { StrategyMetadata } from '../../types/strategy';
import {
  buildExperimentListFilters,
  buildExperimentSortSearchParams,
  experimentSortOptions,
  filterStrategiesByCategory,
  parseExperimentSortKey,
  parseExperimentSortOrder,
  reconcileStrategySelection,
  strategyCategoryOptions,
} from './experimentListFilters';

function strategy(
  strategyId: string,
  category: StrategyMetadata['category'],
): StrategyMetadata {
  return {
    strategy_id: strategyId,
    display_name: strategyId,
    version: '1.0.0',
    category,
    description: '',
    supported_modes: ['batch'],
    requires_training: category === 'ml',
    retrain_frequency: category === 'ml' ? 'monthly' : 'never',
    training_mode: category === 'ml' ? 'periodic' : 'none',
    portfolio_signal_mode: category === 'ml' ? 'target_weights' : 'event_orders',
    execution_config: {
      param_key: '_execution',
      defaults: {
        initial_capital: 1_000_000,
        max_positions: 20,
        lot_size: 100,
        volume_participation: null,
        commission_rate: 0.0003,
        slippage_rate: 0.001,
        stamp_duty_rate: 0.001,
        min_commission: 5,
      },
    },
    params: [],
    sub_strategies: [],
    integration_method: '',
    tags: [],
  };
}

const strategies = [
  strategy('technical_v1', 'technical'),
  strategy('ml_v1', 'ml'),
  strategy('factor_v1', 'factor'),
  strategy('portfolio_v1', 'portfolio'),
  strategy('composite_v1', 'composite'),
];

describe('experiment category filters', () => {
  it('exposes every backend strategy category in the expected order', () => {
    expect(strategyCategoryOptions.map((option) => option.value)).toEqual([
      '',
      'technical',
      'ml',
      'factor',
      'portfolio',
      'composite',
    ]);
  });

  it.each([
    ['technical', 'technical_v1'],
    ['ml', 'ml_v1'],
    ['factor', 'factor_v1'],
    ['portfolio', 'portfolio_v1'],
    ['composite', 'composite_v1'],
  ] as const)('limits %s to matching strategies', (category, strategyId) => {
    expect(
      filterStrategiesByCategory(strategies, category).map(
        (item) => item.strategy_id,
      ),
    ).toEqual([strategyId]);
  });

  it('keeps all strategies when no category is selected', () => {
    expect(filterStrategiesByCategory(strategies, '')).toBe(strategies);
  });

  it('clears an incompatible selected strategy', () => {
    expect(reconcileStrategySelection('ml_v1', 'factor', strategies)).toBe('');
  });

  it('keeps a compatible strategy and preserves selection without a category', () => {
    expect(reconcileStrategySelection('ml_v1', 'ml', strategies)).toBe(
      'ml_v1',
    );
    expect(reconcileStrategySelection('ml_v1', '', strategies)).toBe('ml_v1');
  });

  it('builds the backend request with strategy_category and pagination', () => {
    expect(
      buildExperimentListFilters({
        strategyCategory: 'ml',
        strategyId: 'ml_v1',
        status: 'completed',
        starredOnly: true,
        search: 'Alpha',
        sortBy: 'sharpe_ratio',
        sortOrder: 'asc',
        page: 1,
        limit: 20,
      }),
    ).toEqual({
      strategy_category: 'ml',
      strategy_id: 'ml_v1',
      status: 'completed',
      is_starred: true,
      search: 'Alpha',
      sort_by: 'sharpe_ratio',
      sort_order: 'asc',
      page: 1,
      limit: 20,
    });
  });

  it('offers all server-supported sort fields with clear labels', () => {
    expect(experimentSortOptions).toEqual([
      { value: 'created_at', label: '创建时间' },
      { value: 'annual_return', label: '年化收益' },
      { value: 'sharpe_ratio', label: 'Sharpe 比率' },
      { value: 'max_drawdown', label: '最大回撤' },
      { value: 'strategy_id', label: '策略 ID' },
      { value: 'status', label: '状态' },
    ]);
  });

  it('falls back safely when URL sort state is absent or invalid', () => {
    expect(parseExperimentSortKey(null)).toBe('created_at');
    expect(parseExperimentSortKey('invalid')).toBe('created_at');
    expect(parseExperimentSortKey('annual_return')).toBe('annual_return');
    expect(parseExperimentSortOrder(null)).toBe('desc');
    expect(parseExperimentSortOrder('invalid')).toBe('desc');
    expect(parseExperimentSortOrder('asc')).toBe('asc');
  });

  it('updates sort URL state while preserving unrelated query state', () => {
    const current = new URLSearchParams(
      'strategy_id=ma_cross_v1&sort_by=status',
    );
    const next = buildExperimentSortSearchParams(
      current,
      'annual_return',
      'asc',
    );

    expect(next.get('strategy_id')).toBe('ma_cross_v1');
    expect(next.get('sort_by')).toBe('annual_return');
    expect(next.get('sort_order')).toBe('asc');

    const defaults = buildExperimentSortSearchParams(
      next,
      'created_at',
      'desc',
    );
    expect(defaults.get('sort_by')).toBeNull();
    expect(defaults.get('sort_order')).toBeNull();
    expect(defaults.get('strategy_id')).toBe('ma_cross_v1');
  });
});
