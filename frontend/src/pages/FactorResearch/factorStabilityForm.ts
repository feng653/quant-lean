import type { FactorStabilityConfig } from '../../services/factorResearch';

const DAY_MS = 86_400_000;

function dateAt(start: string, offsetDays: number): string {
  const value = new Date(`${start}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() + offsetDays);
  return value.toISOString().slice(0, 10);
}

export function defaultStabilityConfig(
  researchStart: string,
  researchEnd: string,
): FactorStabilityConfig {
  const startMs = Date.parse(`${researchStart}T00:00:00Z`);
  const endMs = Date.parse(`${researchEnd}T00:00:00Z`);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs) {
    return {
      mode: 'fixed_three_way',
      train: { start: '', end: '' },
      validation: { start: '', end: '' },
      locked: { start: '', end: '' },
      locked_declared: false,
      hypotheses_tested: 1,
      correction: 'bonferroni',
      alpha: 0.05,
    };
  }
  const days = Math.floor((endMs - startMs) / DAY_MS);
  const trainEndOffset = Math.max(0, Math.floor(days * 0.6));
  const validationEndOffset = Math.max(
    trainEndOffset + 2,
    Math.floor(days * 0.8),
  );
  return {
    mode: 'fixed_three_way',
    train: {
      start: researchStart,
      end: dateAt(researchStart, trainEndOffset),
    },
    validation: {
      start: dateAt(researchStart, trainEndOffset + 1),
      end: dateAt(researchStart, validationEndOffset),
    },
    locked: {
      start: dateAt(researchStart, validationEndOffset + 1),
      end: researchEnd,
    },
    locked_declared: false,
    hypotheses_tested: 1,
    correction: 'bonferroni',
    alpha: 0.05,
  };
}

export function validateStabilityConfig(
  value: FactorStabilityConfig | null,
  researchStart: string,
  researchEnd: string,
): string | null {
  if (value === null) return null;
  const dates = [
    value.train.start,
    value.train.end,
    value.validation.start,
    value.validation.end,
    value.locked.start,
    value.locked.end,
  ];
  if (dates.some((date) => !/^\d{4}-\d{2}-\d{2}$/.test(date))) {
    return '样本外评估的六个日期必须完整填写';
  }
  if (dates.some((date, index) => index > 0 && date <= dates[index - 1])) {
    return '训练、验证与锁定窗口必须严格有序且互不重叠';
  }
  if (value.train.start < researchStart || value.locked.end > researchEnd) {
    return '样本外窗口必须位于研究总区间内';
  }
  if (!value.locked_declared) return '提交前必须声明锁定窗';
  if (
    !Number.isInteger(value.hypotheses_tested)
    || value.hypotheses_tested < 1
    || value.hypotheses_tested > 10_000
  ) return '检验假设总数必须是 1–10000 的整数';
  if (!Number.isFinite(value.alpha) || value.alpha <= 0 || value.alpha >= 1) {
    return '显著性阈值必须位于 0 与 1 之间';
  }
  return null;
}
