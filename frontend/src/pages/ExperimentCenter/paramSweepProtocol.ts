import type {
  PromoteSweepResponse,
  SweepExperimentResult,
  SweepResultResponse,
} from '../../services/experiments';

export interface StrictSweepWindows {
  selectionStart: string;
  selectionEnd: string;
  lockedTestStart: string;
  lockedTestEnd: string;
  trainStart?: string | null;
  trainEnd?: string | null;
}

export interface SweepWindowErrors {
  selectionStart?: string;
  selectionEnd?: string;
  lockedTestStart?: string;
  lockedTestEnd?: string;
  trainWindow?: string;
}

export interface SweepDisplayResult {
  experiment_id: number;
  params: Record<string, unknown>;
  sharpe: number | null;
  return: number | null;
  max_drawdown: number | null;
  win_rate: number | null;
  status: string;
}

export type SweepMetricTarget = 'sharpe' | 'return' | 'max_drawdown';

export function sweepMetricValue(
  result: SweepDisplayResult,
  target: SweepMetricTarget,
): number | null {
  const value = target === 'sharpe'
    ? result.sharpe
    : target === 'return'
      ? result.return
      : result.max_drawdown;
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export function compareSweepResults(
  a: SweepDisplayResult,
  b: SweepDisplayResult,
  target: SweepMetricTarget,
): number {
  const av = sweepMetricValue(a, target);
  const bv = sweepMetricValue(b, target);
  if (av === null && bv === null) {
    return a.experiment_id - b.experiment_id;
  }
  if (av === null) return 1;
  if (bv === null) return -1;
  // All stored metrics are maximized. Drawdown is a negative value, so a
  // value closer to zero must sort before a deeper loss.
  return bv - av || a.experiment_id - b.experiment_id;
}

export function validateStrictSweepWindows(
  windows: StrictSweepWindows,
): SweepWindowErrors {
  const errors: SweepWindowErrors = {};
  if (!windows.selectionStart) errors.selectionStart = '请选择选模窗口起始日期';
  if (!windows.selectionEnd) errors.selectionEnd = '请选择选模窗口结束日期';
  if (!windows.lockedTestStart) {
    errors.lockedTestStart = '请选择锁定最终测试起始日期';
  }
  if (!windows.lockedTestEnd) {
    errors.lockedTestEnd = '请选择锁定最终测试结束日期';
  }

  if (
    windows.selectionStart
    && windows.selectionEnd
    && windows.selectionStart >= windows.selectionEnd
  ) {
    errors.selectionEnd = '选模窗口结束日期必须晚于起始日期';
  }
  if (
    windows.lockedTestStart
    && windows.lockedTestEnd
    && windows.lockedTestStart >= windows.lockedTestEnd
  ) {
    errors.lockedTestEnd = '锁定最终测试结束日期必须晚于起始日期';
  }
  if (
    windows.selectionEnd
    && windows.lockedTestStart
    && windows.selectionEnd >= windows.lockedTestStart
  ) {
    errors.lockedTestStart = '必须严格晚于选模窗口结束日期，两个窗口不能重叠';
  }
  if (Boolean(windows.trainStart) !== Boolean(windows.trainEnd)) {
    errors.trainWindow = '训练窗口信息不完整，无法开始扫描';
  } else if (
    windows.trainEnd
    && windows.selectionStart
    && windows.trainEnd >= windows.selectionStart
  ) {
    errors.trainWindow = '训练窗口必须在选模窗口开始前结束';
  }
  return errors;
}

export function hasSweepWindowErrors(errors: SweepWindowErrors): boolean {
  return Object.keys(errors).length > 0;
}

export function mapSelectionResult(
  result: SweepExperimentResult,
): SweepDisplayResult {
  return {
    experiment_id: result.id,
    params: result.params,
    sharpe: result.selection_metrics.sharpe_ratio,
    return: result.selection_metrics.annual_return,
    max_drawdown: result.selection_metrics.max_drawdown,
    win_rate: result.selection_metrics.win_rate,
    status: result.status,
  };
}

export function canPromoteSweepTrial(status: string): boolean {
  return status === 'completed';
}

export function parsePositiveQueryId(value: string | null): number | null {
  if (value === null || !/^[1-9]\d*$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

export function restoredSweepPromotion(
  data: SweepResultResponse,
): PromoteSweepResponse | null {
  const experimentId = data.sweep.promoted_experiment_id;
  const sourceExperimentId = data.sweep.promotion_source_experiment_id;
  if (
    !Number.isSafeInteger(experimentId)
    || Number(experimentId) <= 0
    || !Number.isSafeInteger(sourceExperimentId)
    || Number(sourceExperimentId) <= 0
  ) {
    return null;
  }
  return {
    sweep_id: data.sweep.id,
    source_experiment_id: Number(sourceExperimentId),
    experiment_id: Number(experimentId),
    created: false,
    research_trust: 'locked_test',
  };
}
