import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import EmptyState from '../../components/shared/EmptyState';
import PageHeader from '../../components/shared/PageHeader';
import Select from '../../components/shared/Select';
import StatusTag from '../../components/shared/StatusTag';
import Table from '../../components/shared/Table';
import { useAuthStore } from '../../store/authStore';
import {
  getModelLifecycle,
  listDeployments,
  triggerModelRetrain,
} from '../../services/trading';
import type {
  Deployment,
  ModelLifecycle,
  ModelLifecycleVersion,
  ModelRetrainAttempt,
} from '../../types/trading';
import { formatBackendDateTime } from '../../utils/datetime';
import type { Column } from '../../components/shared/Table';

const frequencyLabels: Record<string, string> = {
  daily: '每日',
  weekly: '每周',
  monthly: '每月',
  quarterly: '每季度',
  never: '不自动重训练',
};

function digest(value?: string | null): string {
  return value ? `${value.slice(0, 12)}…` : '-';
}

function metric(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(4)
    : '-';
}

export default function ModelLifecyclePage() {
  const user = useAuthStore((state) => state.user);
  const canRetrain = Boolean(
    user?.is_admin || user?.permissions.includes('trading:deploy'),
  );
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [lifecycle, setLifecycle] = useState<ModelLifecycle | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ jobId: string } | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const items = await listDeployments();
      setDeployments(items);
      const targetId = items.some((item) => item.id === selectedId)
        ? selectedId
        : items.find((item) => item.requires_retraining)?.id ?? items[0]?.id ?? null;
      setSelectedId(targetId);
      setLifecycle(targetId ? await getModelLifecycle(targetId) : null);
      setError(null);
    } catch (err: unknown) {
      if (!quiet) setError(err instanceof Error ? err.message : '加载模型生命周期失败');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [selectedId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => void load(true), 10_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const selectDeployment = async (rawId: string) => {
    const deploymentId = Number(rawId);
    setSelectedId(deploymentId);
    setLoading(true);
    try {
      setLifecycle(await getModelLifecycle(deploymentId));
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载模型生命周期失败');
    } finally {
      setLoading(false);
    }
  };

  const trigger = async () => {
    if (!selectedId) return;
    setBusy(true);
    try {
      const result = await triggerModelRetrain(selectedId);
      setNotice({ jobId: result.job_id });
      setLifecycle(await getModelLifecycle(selectedId));
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '提交重训练失败');
    } finally {
      setBusy(false);
    }
  };

  const versionColumns = useMemo<Column<ModelLifecycleVersion>[]>(() => [
    {
      key: 'version',
      header: '版本',
      render: (item) => (
        <div className="flex items-center gap-2">
          <span className="font-mono">v{item.model_version}</span>
          {Boolean(item.is_latest) && <StatusTag variant="verified">当前冠军</StatusTag>}
        </div>
      ),
    },
    {
      key: 'window',
      header: '训练 / 验证窗口',
      render: (item) => (
        <div className="text-xs">
          <p>{item.train_window_start ?? '-'} → {item.train_window_end ?? '-'}</p>
          <p className="text-ink-400">{item.validation_window_start ?? '-'} → {item.validation_window_end ?? '-'}</p>
        </div>
      ),
    },
    {
      key: 'validation',
      header: '验证 RankIC',
      render: (item) => (
        <span className="font-mono text-xs">
          {metric(item.validation_metrics.validation_rank_ic)}
        </span>
      ),
    },
    {
      key: 'integrity',
      header: '完整性',
      render: (item) => item.manifest_verified ? (
        <StatusTag variant="verified">证据完整</StatusTag>
      ) : (
        <StatusTag variant="blocked">不可晋级</StatusTag>
      ),
    },
    {
      key: 'digest',
      header: 'SHA-256',
      render: (item) => <span className="font-mono text-xs">{digest(item.model_sha256)}</span>,
    },
    {
      key: 'created',
      header: '创建时间',
      render: (item) => <span className="text-xs">{formatBackendDateTime(item.created_at)}</span>,
    },
  ], []);

  const attemptColumns = useMemo<Column<ModelRetrainAttempt>[]>(() => [
    {
      key: 'attempt',
      header: '尝试',
      render: (item) => (
        <span className="font-mono text-xs">{item.attempt_id.slice(0, 12)}</span>
      ),
    },
    {
      key: 'candidate',
      header: '候选版本',
      render: (item) => <span className="font-mono">v{item.candidate_model_version}</span>,
    },
    {
      key: 'status',
      header: '状态',
      render: (item) => {
        const variant = item.status === 'promoted'
          ? 'verified'
          : item.status === 'running'
            ? 'running'
            : 'error';
        return <StatusTag variant={variant}>{item.status}</StatusTag>;
      },
    },
    {
      key: 'evidence',
      header: '门禁 / 失败证据',
      className: 'max-w-sm',
      render: (item) => item.failure ? (
        <div className="text-xs">
          <p className="font-mono font-medium text-danger-strong">{item.failure.code}</p>
          <p className="whitespace-normal break-words text-ink-500">{item.failure.message}</p>
        </div>
      ) : item.manifest_verified ? (
        <span className="text-xs text-ok-strong">验证、清单和字节证据通过</span>
      ) : (
        <span className="text-xs text-ink-400">候选处理中</span>
      ),
    },
    {
      key: 'created',
      header: '开始 / 完成',
      render: (item) => (
        <div className="text-xs">
          <p>{formatBackendDateTime(item.created_at)}</p>
          <p className="text-ink-400">{formatBackendDateTime(item.completed_at)}</p>
        </div>
      ),
    },
  ], []);

  return (
    <div className="space-y-5">
      <PageHeader
        title="模型生命周期"
        description="训练型策略的调度、人工触发、版本、验证门禁与失败回退证据。这里只管理研究与模拟盘模型，不会自动发布到实盘。"
        tags={<StatusTag variant="paper">仅研究与模拟交易</StatusTag>}
        actions={lifecycle?.deployment.requires_retraining ? (
          <Button
            onClick={() => void trigger()}
            loading={busy}
            disabled={!canRetrain}
            title={canRetrain ? undefined : '需要 trading:deploy 权限'}
          >
            人工触发重训练
          </Button>
        ) : undefined}
      />

      {error && <Banner variant="danger">{error}</Banner>}
      {notice && (
        <Banner variant="ok" title="重训练任务已进入统一队列">
          任务 <span className="font-mono">{notice.jobId.slice(0, 12)}</span> 已提交；
          重复点击会返回同一活动任务。<Link className="ml-1 underline" to="/jobs">查看任务详情</Link>
        </Banner>
      )}

      <Card title="部署选择与调度状态">
        {deployments.length === 0 && !loading ? (
          <EmptyState title="暂无策略部署" description="请先从已验证实验发布策略。" icon="strategies" />
        ) : (
          <div className="space-y-4">
            <Select
              label="策略部署"
              value={selectedId ? String(selectedId) : ''}
              onChange={(event) => void selectDeployment(event.target.value)}
              options={deployments.map((item) => ({
                value: String(item.id),
                label: `${item.display_name || item.strategy_id} · ${item.requires_retraining ? '训练型' : '非训练型'}`,
              }))}
            />
            {lifecycle && (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div className="rounded border border-ink-200 p-3">
                  <p className="text-xs text-ink-400">当前冠军</p>
                  <p className="mt-1 font-mono text-lg">v{lifecycle.deployment.current_model_version}</p>
                </div>
                <div className="rounded border border-ink-200 p-3">
                  <p className="text-xs text-ink-400">重训练频率</p>
                  <p className="mt-1 text-sm">{frequencyLabels[lifecycle.deployment.retrain_frequency ?? 'never'] ?? lifecycle.deployment.retrain_frequency}</p>
                </div>
                <div className="rounded border border-ink-200 p-3">
                  <p className="text-xs text-ink-400">下次到期</p>
                  <p className="mt-1 text-sm">{formatBackendDateTime(lifecycle.schedule.next_retrain_at, '未安排')}</p>
                </div>
                <div className="rounded border border-ink-200 p-3">
                  <p className="text-xs text-ink-400">调度器</p>
                  <div className="mt-1">
                    <StatusTag variant={lifecycle.schedule.enabled ? 'verified' : 'warning'}>
                      {lifecycle.schedule.enabled ? `每 ${lifecycle.schedule.scan_minutes} 分钟扫描` : '已停用'}
                    </StatusTag>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </Card>

      {lifecycle && (
        <>
          <Banner variant="info" title="安全回退边界">
            候选模型只有通过独立验证窗口、规范清单和字节完整性门禁才会原子替换当前冠军。
            训练失败、校验失败或并发冲突都会保留原冠军；平台不提供自动实盘发布。
          </Banner>
          <Card title="不可变版本历史" description="API 仅返回模型存储键和摘要，不暴露本机路径。">
            <Table
              data={lifecycle.versions}
              columns={versionColumns}
              keyField="id"
              loading={loading}
              emptyMessage="尚无可验证的模型版本"
              minWidth="900px"
            />
          </Card>
          <Card title="重训练尝试与失败证据" description="失败候选不会覆盖当前冠军，错误信息已做路径脱敏。">
            <Table
              data={lifecycle.attempts}
              columns={attemptColumns}
              keyField="attempt_id"
              loading={loading}
              emptyMessage="尚无重训练尝试"
              minWidth="900px"
            />
          </Card>
        </>
      )}
    </div>
  );
}
