import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import {
  deleteParameterPreset,
  listParameterPresets,
  updateParameterPreset,
} from '../../services/experiments';
import { listStrategies } from '../../services/strategies';
import type { ParameterPreset } from '../../types/experiment';
import type { ParamField, StrategyMetadata } from '../../types/strategy';
import { useAuthStore } from '../../store/authStore';
import { strategyCategoryLabel, trainingModeLabel } from '../../utils/strategy';
import Badge from '../../components/shared/Badge';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import Select from '../../components/shared/Select';
import Skeleton from '../../components/shared/Skeleton';
import Textarea from '../../components/shared/Textarea';
import { formatPct } from '../../components/shared/chartTheme';
import { formatBackendDateTime } from '../../utils/datetime';

function displayMetric(value: unknown, pct = false): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  return pct ? formatPct(value) : value.toFixed(2);
}

function validateParam(param: ParamField, raw: string): string | null {
  if (param.required && raw.trim() === '') return '必填';
  if (raw.trim() === '') return null;
  const type = param.type.toLowerCase();
  if (type === 'int' || type === 'integer') {
    const num = Number(raw);
    if (!Number.isFinite(num)) return '请输入有效数字';
    if (!Number.isInteger(num)) return '必须是整数';
    if (param.min != null && num < param.min) return `不能小于 ${param.min}`;
    if (param.max != null && num > param.max) return `不能大于 ${param.max}`;
  } else if (type === 'float' || type === 'number') {
    const num = Number(raw);
    if (!Number.isFinite(num)) return '请输入有效数字';
    if (param.min != null && num < param.min) return `不能小于 ${param.min}`;
    if (param.max != null && num > param.max) return `不能大于 ${param.max}`;
  }
  return null;
}

function coerceParam(param: ParamField, raw: string): unknown {
  if (raw.trim() === '') return '';
  const type = param.type.toLowerCase();
  if (type === 'int' || type === 'integer') return parseInt(raw, 10);
  if (type === 'float' || type === 'number') return parseFloat(raw);
  if (type === 'bool' || type === 'boolean') return raw === 'true';
  return raw;
}

interface EditingState {
  id: number;
  name: string;
  params: Record<string, string>;
  notes: string;
  labels: string;
  isDefault: boolean;
}

export default function ParameterPresetPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const user = useAuthStore((s) => s.user);
  const canManage = user?.is_admin || user?.permissions.includes('experiments:create');

  const [strategies, setStrategies] = useState<StrategyMetadata[]>([]);
  const [strategyId, setStrategyId] = useState(searchParams.get('strategy_id') ?? '');
  const [presets, setPresets] = useState<ParameterPreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [editing, setEditing] = useState<EditingState | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void listStrategies()
      .then((result) => {
        setStrategies(result);
        if (!strategyId && result.length > 0) {
          setStrategyId(result[0].strategy_id);
        }
      })
      .catch(() => setStrategies([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!strategyId) return;
    setSearchParams({ strategy_id: strategyId }, { replace: true });
  }, [strategyId, setSearchParams]);

  const loadPresets = useCallback(async () => {
    if (!strategyId) {
      setPresets([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await listParameterPresets(strategyId);
      setPresets(result);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '获取参数方案失败');
    } finally {
      setLoading(false);
    }
  }, [strategyId]);

  useEffect(() => {
    void loadPresets();
  }, [loadPresets]);

  const strategy = strategies.find((item) => item.strategy_id === strategyId) ?? null;

  const startEdit = (preset: ParameterPreset) => {
    if (!strategy) return;
    const params: Record<string, string> = {};
    for (const param of strategy.params) {
      const value = preset.params[param.name];
      params[param.name] = value === undefined || value === null ? '' : String(value);
    }
    setEditing({
      id: preset.id,
      name: preset.name,
      params,
      notes: preset.notes ?? '',
      labels: preset.labels.join(', '),
      isDefault: preset.is_default,
    });
    setMessage(null);
  };

  const saveEdit = async () => {
    if (!editing || !strategy) return;
    for (const param of strategy.params) {
      const error = validateParam(param, editing.params[param.name] ?? '');
      if (error) {
        setMessage('请先修正参数方案中的无效字段');
        return;
      }
    }
    setSaving(true);
    try {
      const params = Object.fromEntries(
        strategy.params.map((param) => [param.name, coerceParam(param, editing.params[param.name] ?? '')]),
      );
      const updated = await updateParameterPreset(editing.id, {
        name: editing.name.trim(),
        params,
        notes: editing.notes ?? '',
        labels: editing.labels.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
        is_default: editing.isDefault,
      });
      setPresets((current) =>
        current.map((item) => ({
          ...item,
          ...(item.id === updated.id ? updated : {}),
          is_default: updated.is_default ? item.id === updated.id : item.is_default,
        })),
      );
      setEditing(null);
      setMessage(`参数方案“${updated.name}”已保存`);
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : '保存参数方案失败');
    } finally {
      setSaving(false);
    }
  };

  const makeDefault = async (preset: ParameterPreset) => {
    try {
      await updateParameterPreset(preset.id, { is_default: true });
      setPresets((current) =>
        current.map((item) => ({ ...item, is_default: item.id === preset.id })),
      );
      setMessage(`“${preset.name}”已设为默认方案`);
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : '设置默认方案失败');
    }
  };

  const removePreset = async (preset: ParameterPreset) => {
    if (!window.confirm(`确认删除参数方案“${preset.name}”？来源实验不会被删除。`)) return;
    try {
      await deleteParameterPreset(preset.id);
      setPresets((current) => current.filter((item) => item.id !== preset.id));
      setMessage(`参数方案“${preset.name}”已删除`);
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : '删除参数方案失败');
    }
  };

  return (
    <div>
      <PageHeader
        title="参数方案"
        description="保存、维护并复用经过验证的策略参数。"
        breadcrumb={[{ label: '研究' }, { label: '实验中心', to: '/experiment' }, { label: '参数管理' }]}
        actions={
          canManage && strategy ? (
            <Button size="sm" onClick={() => navigate(`/experiment/new?strategy_id=${strategy.strategy_id}`)}>
              <Icon name="plus" className="h-4 w-4" />
              新建实验
            </Button>
          ) : undefined
        }
      />

      <Card className="mb-4" padding="md">
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-full max-w-md">
            <Select
              label="策略"
              value={strategyId}
              onChange={(event) => setStrategyId(event.target.value)}
              options={strategies.map((item) => ({
                value: item.strategy_id,
                label: `${item.display_name} · ${item.strategy_id}`,
              }))}
            />
          </div>
          {strategy && (
            <div className="flex flex-wrap items-center gap-1.5 pb-1">
              <Badge variant="info">{strategyCategoryLabel(strategy.category)}</Badge>
              <Badge variant={strategy.requires_training ? 'warning' : 'default'}>
                {trainingModeLabel(strategy)}
              </Badge>
              <Badge variant="accent">{presets.length} 个已保存方案</Badge>
            </div>
          )}
        </div>
      </Card>

      {message && <Banner variant="info" className="mb-4">{message}</Banner>}
      {error && (
        <Banner
          variant="danger"
          className="mb-4"
          action={<Button variant="secondary" size="sm" onClick={() => void loadPresets()}>重试</Button>}
        >
          {error}
        </Banner>
      )}

      {loading ? (
        <div className="space-y-3">
          <Skeleton className="h-36 w-full" />
          <Skeleton className="h-36 w-full" />
        </div>
      ) : presets.length === 0 ? (
        <div className="rounded-md border border-ink-200 bg-surface">
          <EmptyState
            icon="presets"
            title="该策略还没有参数方案"
            description="完成实验后，可在实验详情页将已验证参数保存为方案。"
          />
        </div>
      ) : (
        <div className="space-y-4">
          {presets.map((preset) => {
            const isEditing = editing?.id === preset.id;
            return (
              <Card
                key={preset.id}
                title={
                  undefined
                }
                padding="md"
              >
                {!isEditing ? (
                  <div>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div className="flex items-center gap-2">
                          <h3 className="text-base font-semibold text-ink-900">{preset.name}</h3>
                          {preset.is_default && <Badge variant="success" size="sm">默认</Badge>}
                        </div>
                        <p className="tnum mt-1 text-xs text-ink-400">
                          更新于 {formatBackendDateTime(preset.updated_at)}
                          {preset.source_experiment_id ? ` · 来源实验 #${preset.source_experiment_id}` : ''}
                        </p>
                      </div>
                      {canManage && (
                        <div className="flex flex-wrap items-center gap-2">
                          <Button variant="secondary" size="sm" onClick={() => navigate(`/experiment/new?preset_id=${preset.id}`)}>
                            调出使用
                          </Button>
                          {!preset.is_default && (
                            <Button variant="ghost" size="sm" onClick={() => void makeDefault(preset)}>
                              设为默认
                            </Button>
                          )}
                          <Button variant="ghost" size="sm" onClick={() => startEdit(preset)}>
                            <Icon name="edit" className="h-4 w-4" />
                            编辑
                          </Button>
                          <Button variant="ghost" size="sm" onClick={() => void removePreset(preset)}>
                            <Icon name="trash" className="h-4 w-4 text-danger-fg" />
                          </Button>
                        </div>
                      )}
                    </div>

                    <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                      <SnapshotMetric label="Sharpe" value={displayMetric(preset.metrics_snapshot?.sharpe_ratio)} />
                      <SnapshotMetric label="年化收益" value={displayMetric(preset.metrics_snapshot?.annual_return, true)} />
                      <SnapshotMetric label="最大回撤" value={displayMetric(preset.metrics_snapshot?.max_drawdown, true)} />
                      <SnapshotMetric label="股票池" value={preset.pool_preset ?? '-'} />
                    </div>

                    <div className="mt-3 flex flex-wrap gap-1.5">
                      {Object.entries(preset.params).map(([key, value]) => (
                        <span key={key} className="rounded-sm border border-ink-200 bg-ink-50 px-2 py-0.5 font-mono text-2xs text-ink-600">
                          {key}={JSON.stringify(value)}
                        </span>
                      ))}
                    </div>

                    {preset.labels.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {preset.labels.map((label) => (
                          <span key={label} className="rounded-sm bg-accent-50 px-1.5 py-px text-2xs text-accent-800">
                            {label}
                          </span>
                        ))}
                      </div>
                    )}
                    {preset.notes && (
                      <p className="mt-2 text-sm leading-6 text-ink-500">{preset.notes}</p>
                    )}
                  </div>
                ) : (
                  <div>
                    <div className="mb-4 max-w-md">
                      <Input
                        label="方案名称"
                        value={editing.name}
                        onChange={(event) => setEditing({ ...editing, name: event.target.value })}
                      />
                    </div>
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                      {strategy?.params.map((param) => {
                        const raw = editing.params[param.name] ?? '';
                        const error = validateParam(param, raw);
                        const type = param.type.toLowerCase();
                        if (type === 'bool' || type === 'boolean') {
                          return (
                            <label key={param.name} className="flex min-h-[38px] cursor-pointer items-center gap-2 self-end text-sm text-ink-700">
                              <input
                                type="checkbox"
                                checked={raw === 'true'}
                                onChange={(event) =>
                                  setEditing({
                                    ...editing,
                                    params: { ...editing.params, [param.name]: String(event.target.checked) },
                                  })
                                }
                                className="h-4 w-4 rounded-sm border-ink-300 text-accent-700 focus:ring-accent-600"
                              />
                              {param.name}（{raw === 'true' ? '开启' : '关闭'}）
                            </label>
                          );
                        }
                        if (param.choices && param.choices.length > 0) {
                          return (
                            <Select
                              key={param.name}
                              label={param.name}
                              value={raw}
                              onChange={(event) =>
                                setEditing({
                                  ...editing,
                                  params: { ...editing.params, [param.name]: event.target.value },
                                })
                              }
                              options={param.choices.map((choice) => ({ value: choice, label: choice }))}
                              error={error ?? undefined}
                            />
                          );
                        }
                        return (
                          <Input
                            key={param.name}
                            label={param.name}
                            type={type === 'int' || type === 'integer' || type === 'float' || type === 'number' ? 'number' : 'text'}
                            value={raw}
                            onChange={(event) =>
                              setEditing({
                                ...editing,
                                params: { ...editing.params, [param.name]: event.target.value },
                              })
                            }
                            error={error ?? undefined}
                            hint={param.description}
                          />
                        );
                      })}
                    </div>
                    <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
                      <Input
                        label="标签（逗号分隔）"
                        value={editing.labels}
                        onChange={(event) => setEditing({ ...editing, labels: event.target.value })}
                      />
                      <Textarea
                        label="备注"
                        rows={2}
                        value={editing.notes}
                        onChange={(event) => setEditing({ ...editing, notes: event.target.value })}
                      />
                    </div>
                    <label className="mt-3 flex cursor-pointer items-center gap-2 text-sm text-ink-700">
                      <input
                        type="checkbox"
                        checked={editing.isDefault}
                        onChange={(event) => setEditing({ ...editing, isDefault: event.target.checked })}
                        className="h-4 w-4 rounded-sm border-ink-300 text-accent-700 focus:ring-accent-600"
                      />
                      设为该策略的默认方案
                    </label>
                    <div className="mt-4 flex items-center gap-2">
                      <Button size="sm" onClick={() => void saveEdit()} loading={saving}>
                        保存修改
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setEditing(null)}>
                        取消
                      </Button>
                    </div>
                  </div>
                )}
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SnapshotMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-ink-100 p-2.5">
      <p className="text-2xs text-ink-400">{label}</p>
      <p className="tnum mt-0.5 text-sm font-semibold">{value}</p>
    </div>
  );
}
