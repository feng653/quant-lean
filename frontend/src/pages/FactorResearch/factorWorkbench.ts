import type { FactorResearchRun } from '../../services/factorResearch';

export type FactorWorkbenchPresetId =
  | 'quick_signal'
  | 'cost_aware'
  | 'medium_horizon';

export interface FactorWorkbenchPreset {
  id: FactorWorkbenchPresetId;
  name: string;
  description: string;
  horizonsText: string;
  primaryHorizon: number;
  quantiles: number;
  rebalanceInterval: number;
  defaultCostBps: number;
  costScenariosText: string;
  participationRatesText: string;
  winsorMethod: 'mad' | 'quantile' | 'none';
  orthogonalize: boolean;
}

export const FACTOR_WORKBENCH_PRESETS: FactorWorkbenchPreset[] = [
  {
    id: 'quick_signal',
    name: '快速信号诊断',
    description: '观察 1/5/20 日衰减，适合先确认方向、覆盖和短期换手。',
    horizonsText: '1, 5, 20',
    primaryHorizon: 5,
    quantiles: 5,
    rebalanceInterval: 5,
    defaultCostBps: 10,
    costScenariosText: '0, 5, 10, 20',
    participationRatesText: '0.01, 0.05, 0.1',
    winsorMethod: 'mad',
    orthogonalize: true,
  },
  {
    id: 'cost_aware',
    name: '成本与容量压力',
    description: '提高成本档位并缩短调仓间隔，用于暴露高换手因子的实施风险。',
    horizonsText: '1, 5, 10',
    primaryHorizon: 5,
    quantiles: 5,
    rebalanceInterval: 3,
    defaultCostBps: 20,
    costScenariosText: '0, 10, 20, 35, 50',
    participationRatesText: '0.005, 0.01, 0.03, 0.05',
    winsorMethod: 'mad',
    orthogonalize: true,
  },
  {
    id: 'medium_horizon',
    name: '中期稳健性',
    description: '以 20 日为主周期，检查 5/20/60 日衰减并降低换手。',
    horizonsText: '5, 20, 60',
    primaryHorizon: 20,
    quantiles: 5,
    rebalanceInterval: 20,
    defaultCostBps: 10,
    costScenariosText: '0, 5, 10, 20',
    participationRatesText: '0.01, 0.05, 0.1',
    winsorMethod: 'mad',
    orthogonalize: true,
  },
];

export type FactorRunSort = 'newest' | 'oldest' | 'factor' | 'horizon';

export function filterAndSortFactorRuns(
  runs: FactorResearchRun[],
  {
    factorId,
    query,
    sort,
  }: {
    factorId: string;
    query: string;
    sort: FactorRunSort;
  },
): FactorResearchRun[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filtered = runs.filter((run) => {
    if (factorId && run.factor_id !== factorId) return false;
    if (!normalizedQuery) return true;
    return (
      run.run_id.toLocaleLowerCase().includes(normalizedQuery)
      || run.factor_id.toLocaleLowerCase().includes(normalizedQuery)
    );
  });
  return [...filtered].sort((left, right) => {
    if (sort === 'oldest') {
      return left.created_at.localeCompare(right.created_at)
        || left.run_id.localeCompare(right.run_id);
    }
    if (sort === 'factor') {
      return left.factor_id.localeCompare(right.factor_id)
        || right.created_at.localeCompare(left.created_at)
        || left.run_id.localeCompare(right.run_id);
    }
    if (sort === 'horizon') {
      return left.request.primary_horizon - right.request.primary_horizon
        || right.created_at.localeCompare(left.created_at)
        || left.run_id.localeCompare(right.run_id);
    }
    return right.created_at.localeCompare(left.created_at)
      || left.run_id.localeCompare(right.run_id);
  });
}
