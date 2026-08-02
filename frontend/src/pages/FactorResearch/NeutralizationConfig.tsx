import type {
  FactorCacheCapability,
  NeutralizationMode,
} from '../../services/factorResearch';
import { neutralizationUnavailableReason } from './neutralizationForm';

const OPTIONS: Array<{
  mode: NeutralizationMode;
  label: string;
  description: string;
}> = [
  { mode: 'none', label: '不做中性化', description: '保留原始截面因子暴露' },
  { mode: 'industry', label: '行业中性', description: '逐交易日查询 PIT 行业并做截面回归' },
  { mode: 'size', label: '规模中性', description: '使用有字段级 provenance 的 PIT 市值' },
  { mode: 'industry+size', label: '行业 + 规模', description: '同一交易日联合回归行业哑变量与对数市值' },
];

export default function NeutralizationConfig({
  pool,
  value,
  onChange,
}: {
  pool: FactorCacheCapability | undefined;
  value: NeutralizationMode;
  onChange: (mode: NeutralizationMode) => void;
}) {
  return (
    <fieldset className="mt-4 rounded border border-ink-200 bg-surface p-4">
      <legend className="px-1 text-sm font-semibold text-ink-800">
        截面风险中性化
      </legend>
      <p className="mb-3 text-xs text-ink-500">
        回归只使用同一交易日截面。行业快照、市值替代值或覆盖有缺口的数据不会被静默采用。
      </p>
      <div className="grid gap-2 md:grid-cols-2">
        {OPTIONS.map((option) => {
          const reason = neutralizationUnavailableReason(pool, option.mode);
          const disabled = option.mode !== 'none' && reason !== null;
          return (
            <label
              key={option.mode}
              className={`rounded border p-3 ${
                value === option.mode ? 'border-accent-600 bg-accent-50' : 'border-ink-200'
              } ${disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
            >
              <span className="flex items-center gap-2 text-sm font-medium text-ink-800">
                <input
                  type="radio"
                  name="factor-neutralization"
                  value={option.mode}
                  checked={value === option.mode}
                  disabled={disabled}
                  onChange={() => onChange(option.mode)}
                />
                {option.label}
              </span>
              <span className="mt-1 block text-xs text-ink-500">
                {disabled ? reason : option.description}
              </span>
            </label>
          );
        })}
      </div>
      {pool?.neutralization?.size.ready && (
        <p className="mt-3 text-xs text-ink-500">
          规模字段：{pool.neutralization.size.selected_field} ·
          来源证据已绑定到缓存完整性摘要
        </p>
      )}
    </fieldset>
  );
}
