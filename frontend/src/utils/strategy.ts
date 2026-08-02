import type { StrategyMetadata } from '../types/strategy';

export const STRATEGY_CATEGORY_LABEL: Record<StrategyMetadata['category'], string> = {
  technical: '技术指标',
  ml: '机器学习',
  factor: '因子策略',
  portfolio: '组合管理',
  composite: '复合策略',
};

export function strategyCategoryLabel(category: string): string {
  return STRATEGY_CATEGORY_LABEL[category as StrategyMetadata['category']] ?? category;
}

/** Derive the training mode the same way the platform driver does. */
export function strategyTrainingMode(strategy: StrategyMetadata): 'none' | 'train_once' | 'periodic' {
  if (strategy.training_mode) return strategy.training_mode;
  if (!strategy.requires_training) return 'none';
  return strategy.retrain_frequency === 'never' ? 'train_once' : 'periodic';
}

export function trainingModeLabel(strategy: StrategyMetadata): string {
  const mode = strategyTrainingMode(strategy);
  if (mode === 'periodic') return `周期重训练 · ${strategy.retrain_frequency}`;
  if (mode === 'train_once') return '一次训练';
  return '无需训练';
}
