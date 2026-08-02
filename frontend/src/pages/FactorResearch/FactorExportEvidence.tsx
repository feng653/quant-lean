import StatusTag from '../../components/shared/StatusTag';
import type { FactorResearchRun } from '../../services/factorResearch';

function digest(value: string): string {
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}

export default function FactorExportEvidence({
  runs,
  selectedRunIds,
  published,
}: {
  runs: FactorResearchRun[];
  selectedRunIds: string[];
  published: {
    strategyId: string;
    version: string;
    evidenceCount: number;
  } | null;
}) {
  const selected = runs.filter((run) => selectedRunIds.includes(run.run_id));
  return (
    <div className="rounded border border-ink-200 bg-ink-50/40 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold text-ink-800">发布证据</h4>
        <StatusTag variant={selected.length ? 'verified' : 'blocked'}>
          {selected.length ? `${selected.length} 条不可变运行` : '未绑定，禁止导出'}
        </StatusTag>
      </div>
      <p className="mt-1 text-xs text-ink-500">
        发布时服务器会重新校验运行归属、数据摘要、结果摘要与因子定义版本；归档不会删除证据。
      </p>
      {selected.length > 0 && (
        <ul className="mt-2 space-y-1 font-mono text-xs text-ink-600">
          {selected.map((run) => (
            <li key={run.run_id} className="break-all">
              {run.factor_id}@{run.factor_version ?? 'legacy_unversioned'}
              {' · 数据 '}{digest(run.dataset_digest)}
              {' · 结果 '}{digest(run.result_digest)}
            </li>
          ))}
        </ul>
      )}
      {published && (
        <div className="mt-3 border-t border-ink-200 pt-2 text-xs text-ink-600">
          已发布 <span className="font-mono">{published.strategyId}</span>
          {' · '}版本 {published.version}
          {' · '}绑定 {published.evidenceCount} 条证据
        </div>
      )}
    </div>
  );
}
