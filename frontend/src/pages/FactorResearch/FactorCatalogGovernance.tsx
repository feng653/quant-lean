import { useState } from 'react';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import StatusTag from '../../components/shared/StatusTag';
import { useAuthStore } from '../../store/authStore';
import {
  setFactorLifecycle,
  type FactorDefinition,
} from '../../services/factorResearch';

function shortDigest(value: string): string {
  return `${value.slice(0, 10)}…${value.slice(-8)}`;
}

export default function FactorCatalogGovernance({
  factors,
  onChanged,
}: {
  factors: FactorDefinition[];
  onChanged: () => Promise<void>;
}) {
  const user = useAuthStore((state) => state.user);
  const canGovern = Boolean(
    user?.is_admin || user?.permissions.includes('admin:users'),
  );
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const changeStatus = async (
    factor: FactorDefinition,
    status: 'published' | 'deprecated',
  ) => {
    const identity = `${factor.factor_id}@${factor.version}`;
    setSaving(identity);
    setError(null);
    try {
      await setFactorLifecycle({
        factor_id: factor.factor_id,
        version: factor.version,
        definition_digest: factor.definition_digest,
        expected_revision: factor.revision,
        status,
        idempotency_key: `factor-${status}-${crypto.randomUUID()}`,
      });
      await onChanged();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '目录状态更新失败');
    } finally {
      setSaving(null);
    }
  };

  return (
    <Card
      className="mt-4"
      title="因子目录与版本"
      description="定义摘要锚定受审代码；网页只能改变精确版本的发布状态，不能提交表达式或 Python。"
    >
      {error && <p role="alert" className="mb-3 text-sm text-danger-700">{error}</p>}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left text-xs">
          <thead>
            <tr className="border-b border-ink-200 text-ink-500">
              <th className="p-2">因子</th>
              <th className="p-2">版本</th>
              <th className="p-2">参数契约</th>
              <th className="p-2">依赖</th>
              <th className="p-2">不可变摘要</th>
              <th className="p-2">状态</th>
              {canGovern && <th className="p-2 text-right">治理</th>}
            </tr>
          </thead>
          <tbody>
            {factors.map((factor) => {
              const identity = `${factor.factor_id}@${factor.version}`;
              return (
                <tr key={identity} className="border-b border-ink-100">
                  <td className="p-2">
                    <div className="font-medium text-ink-800">{factor.name}</div>
                    <div className="font-mono text-ink-400">{factor.factor_id}</div>
                  </td>
                  <td className="p-2 font-mono">
                    {factor.version}
                    {factor.current && (
                      <div className="text-accent-700">当前代码版本</div>
                    )}
                    {factor.supersedes && (
                      <div className="text-ink-400">替代 {factor.supersedes}</div>
                    )}
                  </td>
                  <td className="p-2">
                    {Object.keys(factor.parameters).length
                      ? Object.entries(factor.parameters)
                        .map(([key, value]) => `${key}=${String(value)}`).join(', ')
                      : '无参数'}
                    <div className="text-ink-400">未知参数拒绝</div>
                  </td>
                  <td className="p-2">
                    {factor.dependencies.length
                      ? factor.dependencies
                        .map((item) => `${item.factor_id}@${item.version}`).join(', ')
                      : '无'}
                  </td>
                  <td className="p-2 font-mono" title={factor.definition_digest}>
                    {shortDigest(factor.definition_digest)}
                  </td>
                  <td className="p-2">
                    <StatusTag variant={factor.deprecated ? 'blocked' : 'verified'}>
                      {factor.deprecated ? '已弃用' : '已发布'}
                    </StatusTag>
                    <div className="mt-1 text-ink-400">修订 {factor.revision}</div>
                  </td>
                  {canGovern && (
                    <td className="p-2 text-right">
                      <Button
                        size="sm"
                        variant={factor.deprecated ? 'secondary' : 'danger'}
                        disabled={saving === identity}
                        onClick={() => void changeStatus(
                          factor,
                          factor.deprecated ? 'published' : 'deprecated',
                        )}
                      >
                        {factor.deprecated ? '重新发布' : '弃用'}
                      </Button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
