import Banner from '../../components/shared/Banner';
import Input from '../../components/shared/Input';
import type {
  FactorResearchWindow,
  FactorStabilityConfig,
} from '../../services/factorResearch';
import {
  defaultStabilityConfig,
  validateStabilityConfig,
} from './factorStabilityForm';

interface Props {
  value: FactorStabilityConfig | null;
  researchStart: string;
  researchEnd: string;
  onChange: (value: FactorStabilityConfig | null) => void;
}

export default function FactorStabilityConfigPanel({
  value,
  researchStart,
  researchEnd,
  onChange,
}: Props) {
  const updateWindow = (
    role: 'train' | 'validation' | 'locked',
    field: keyof FactorResearchWindow,
    date: string,
  ) => {
    if (value === null) return;
    onChange({
      ...value,
      [role]: { ...value[role], [field]: date },
    });
  };
  const error = validateStabilityConfig(value, researchStart, researchEnd);

  return (
    <section className="mt-5 rounded border border-ink-200 bg-ink-50/40 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-ink-800">
            预注册样本外稳定性
          </h3>
          <p className="mt-1 text-xs text-ink-500">
            独立评估训练、验证和锁定窗口；训练至少 252 个交易日，验证与锁定各至少 63 个交易日。
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-ink-700">
          <input
            type="checkbox"
            aria-label="启用预注册样本外稳定性"
            checked={value !== null}
            onChange={(event) => onChange(
              event.target.checked
                ? defaultStabilityConfig(researchStart, researchEnd)
                : null,
            )}
          />
          启用
        </label>
      </div>

      {value === null ? (
        <p role="status" className="mt-3 text-sm text-ink-500">
          本次不生成分窗样本外证据。启用后，系统会对每个窗口单独截断前瞻收益。
        </p>
      ) : (
        <>
          <Banner className="mt-3" variant="warning" title="防泄漏边界">
            每个窗口在计算前瞻收益前按窗口结束日截断；日度 IC 不跨窗合并。锁定窗查看后不能回改本次运行配置。
          </Banner>
          <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
            {([
              ['train', '训练窗'],
              ['validation', '验证窗'],
              ['locked', '锁定窗'],
            ] as const).map(([role, label]) => (
              <fieldset key={role} className="rounded border border-ink-200 p-3">
                <legend className="px-1 text-sm font-medium text-ink-700">{label}</legend>
                <div className="space-y-3">
                  <Input
                    label="开始"
                    type="date"
                    min={researchStart || undefined}
                    max={researchEnd || undefined}
                    value={value[role].start}
                    onChange={(event) => updateWindow(role, 'start', event.target.value)}
                  />
                  <Input
                    label="结束"
                    type="date"
                    min={researchStart || undefined}
                    max={researchEnd || undefined}
                    value={value[role].end}
                    onChange={(event) => updateWindow(role, 'end', event.target.value)}
                  />
                </div>
              </fieldset>
            ))}
          </div>
          <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
            <Input
              label="已检验假设总数"
              type="number"
              min={1}
              max={10_000}
              value={value.hypotheses_tested}
              hint="包含平台外已尝试的同类因子"
              onChange={(event) => onChange({
                ...value,
                hypotheses_tested: Number(event.target.value),
              })}
            />
            <Input
              label="显著性阈值 α"
              type="number"
              min={0.001}
              max={0.2}
              step={0.001}
              value={value.alpha}
              hint="使用 Bonferroni 保守校正"
              onChange={(event) => onChange({
                ...value,
                alpha: Number(event.target.value),
              })}
            />
            <label className="flex items-center gap-2 self-center text-sm text-ink-700">
              <input
                type="checkbox"
                checked={value.locked_declared}
                onChange={(event) => onChange({
                  ...value,
                  locked_declared: event.target.checked,
                })}
              />
              我在运行前声明锁定窗，且不会按结果回改本次配置
            </label>
          </div>
          {error && <p role="alert" className="mt-3 text-sm text-danger-fg">{error}</p>}
        </>
      )}
    </section>
  );
}
