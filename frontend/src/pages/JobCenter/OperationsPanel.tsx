import type { JobObservability } from '../../types/job';
import { formatSchedulerReasons, jobTypeLabel } from '../../utils/jobs';
import Card from '../../components/shared/Card';
import StatusTag from '../../components/shared/StatusTag';

function seconds(value: number | null): string {
  if (value == null) return '-';
  if (value < 60) return `${Math.round(value)} 秒`;
  return `${Math.round(value / 60)} 分`;
}

function ratio(value: number | null): string {
  return value == null ? '-' : `${(value * 100).toFixed(1)}%`;
}

const objectiveLabels: Record<string, string> = {
  job_success_rate: '任务成功率',
  queue_wait_p95_seconds: '排队时长 P95',
  sqlite_contention_events: 'SQLite 争用',
  service_starts: '服务启动次数',
};

function objectiveValue(name: string, value: number | null): string {
  if (value == null) return '-';
  if (name === 'job_success_rate') return ratio(value);
  if (name === 'queue_wait_p95_seconds') return seconds(value);
  return String(value);
}

export default function OperationsPanel({ data }: { data: JobObservability }) {
  const metric = data.worker.metrics;
  const recentRefresh = data.data_refresh.recent[0];
  const jobRows = Object.entries(data.jobs.by_type);
  const objectiveRows = Object.entries(data.slo.objectives);
  const recentAlerts = data.slo.alerting?.recent ?? [];

  return (
    <Card className="mb-4" padding="md">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-ink-800">服务 SLO 与资源预算</h2>
          <p className="mt-0.5 text-xs text-ink-400">
            最近 {data.window_hours} 小时 · 低基数聚合，不含用户、路径、Token 或任务 ID
          </p>
        </div>
        <StatusTag variant={data.worker.pause_heavy ? 'warning' : 'verified'}>
          {data.worker.pause_heavy ? '重任务暂停领取' : '预算正常'}
        </StatusTag>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <div className="rounded border border-ink-100 bg-ink-50 p-2.5">
          <p className="text-2xs text-ink-400">CPU 归一化负载</p>
          <p className="tnum mt-1 text-sm font-semibold">{ratio(metric?.normalized_load ?? null)}</p>
        </div>
        <div className="rounded border border-ink-100 bg-ink-50 p-2.5">
          <p className="text-2xs text-ink-400">可用内存</p>
          <p className="tnum mt-1 text-sm font-semibold">
            {metric?.memory_available_mb == null ? '-' : `${Math.round(metric.memory_available_mb)} MB`}
          </p>
        </div>
        <div className="rounded border border-ink-100 bg-ink-50 p-2.5">
          <p className="text-2xs text-ink-400">磁盘剩余</p>
          <p className="tnum mt-1 text-sm font-semibold">
            {metric?.disk_free_mb == null ? '-' : `${Math.round(metric.disk_free_mb)} MB`}
          </p>
        </div>
        <div className="rounded border border-ink-100 bg-ink-50 p-2.5">
          <p className="text-2xs text-ink-400">缓存质量</p>
          <p className="tnum mt-1 text-sm font-semibold">
            {data.cache_quality.counts.research_ready}/{data.cache_quality.counts.total} 研究可用
          </p>
        </div>
      </div>

      <p className="mt-2 text-xs text-ink-500">
        调度原因：{formatSchedulerReasons(data.worker.reasons)}
        {recentRefresh && ` · 最近数据刷新：${recentRefresh.stage} ${(recentRefresh.progress * 100).toFixed(0)}%`}
      </p>

      <div className="mt-3 overflow-x-auto">
        <table className="min-w-full text-left text-xs">
          <thead className="text-ink-400">
            <tr>
              <th className="py-1.5 pr-4 font-medium">SLO 目标</th>
              <th className="py-1.5 pr-4 font-medium">目标值</th>
              <th className="py-1.5 pr-4 font-medium">实际值</th>
              <th className="py-1.5 pr-4 font-medium">状态</th>
              <th className="py-1.5 font-medium">观察窗</th>
            </tr>
          </thead>
          <tbody>
            {objectiveRows.map(([name, item]) => (
              <tr key={name} className="border-t border-ink-100">
                <td className="py-1.5 pr-4">{objectiveLabels[name] || name}</td>
                <td className="tnum py-1.5 pr-4">
                  {item.target != null
                    ? `≥ ${objectiveValue(name, item.target)}`
                    : `≤ ${objectiveValue(name, item.target_max ?? null)}`}
                </td>
                <td className="tnum py-1.5 pr-4">{objectiveValue(name, item.actual)}</td>
                <td className="py-1.5 pr-4">
                  <StatusTag variant={item.met === false ? 'error' : item.met === true ? 'verified' : 'unverified'}>
                    {item.met === false ? '越界' : item.met === true ? '达标' : '待样本'}
                  </StatusTag>
                </td>
                <td className="tnum py-1.5">{data.window_hours} 小时</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-3 rounded border border-ink-100 bg-ink-50 p-2.5 text-xs">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-medium text-ink-700">最近 SLO 告警</p>
          {data.slo.alerting && (
            <p className="text-ink-400">
              连续 {data.slo.alerting.confirmations_required} 次确认 · 冷却 {seconds(data.slo.alerting.cooldown_seconds)}
            </p>
          )}
        </div>
        {recentAlerts.length === 0 ? (
          <p className="mt-1 text-ink-400">暂无 breach/recovery 告警</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {recentAlerts.slice(0, 5).map((alert, index) => (
              <li key={`${alert.objective}-${alert.created_at}-${index}`} className="flex flex-wrap gap-x-2 text-ink-600">
                <span className={alert.transition === 'breach' ? 'text-danger-strong' : 'text-ok-strong'}>
                  {alert.transition === 'breach' ? '越界' : '恢复'}
                </span>
                <span>{objectiveLabels[alert.objective] || alert.objective}</span>
                <span className="tnum">
                  {objectiveValue(alert.objective, alert.actual)} / {objectiveValue(alert.objective, alert.threshold)}
                </span>
                <span>{alert.window_hours} 小时窗</span>
                {!alert.notification_emitted && <span className="text-ink-400">冷却期内仅审计</span>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-3 overflow-x-auto">
        <table className="min-w-full text-left text-xs">
          <thead className="text-ink-400">
            <tr>
              <th className="py-1.5 pr-4 font-medium">任务类型</th>
              <th className="py-1.5 pr-4 font-medium">提交</th>
              <th className="py-1.5 pr-4 font-medium">成功率</th>
              <th className="py-1.5 pr-4 font-medium">排队 P95</th>
              <th className="py-1.5 font-medium">运行 P95</th>
            </tr>
          </thead>
          <tbody>
            {jobRows.length === 0 ? (
              <tr><td className="py-2 text-ink-400" colSpan={5}>窗口内暂无任务样本</td></tr>
            ) : jobRows.map(([type, item]) => (
              <tr key={type} className="border-t border-ink-100">
                <td className="py-1.5 pr-4">{jobTypeLabel[type] || type}</td>
                <td className="tnum py-1.5 pr-4">{item.submitted}</td>
                <td className="tnum py-1.5 pr-4">{ratio(item.success_rate)}</td>
                <td className="tnum py-1.5 pr-4">{seconds(item.queue_wait_seconds.p95)}</td>
                <td className="tnum py-1.5">{seconds(item.run_duration_seconds.p95)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
