import { useEffect, useMemo, useState } from 'react';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import Input from '../../components/shared/Input';
import StatusTag from '../../components/shared/StatusTag';
import {
  createFactorProtocol,
  createFactorProtocolVersion,
  listFactorProtocols,
  lockFactorProtocol,
  type FactorProtocolPayload,
  type FactorProtocolReference,
  type FactorProtocolSeries,
  type FactorProtocolVersion,
} from '../../services/factorResearch';
import { formatBackendDateTime } from '../../utils/datetime';
import { useAuthStore } from '../../store/authStore';

type ProtocolBase = Omit<
  FactorProtocolPayload,
  'question' | 'hypothesis' | 'thresholds' | 'export_rules'
>;

interface Props {
  base: ProtocolBase | null;
  suggestedDatasetDigest?: string | null;
  activeReference: FactorProtocolReference | null;
  onApply: (
    payload: FactorProtocolPayload,
    reference: FactorProtocolReference,
  ) => void;
}

export default function FactorProtocolPanel({
  base,
  suggestedDatasetDigest,
  activeReference,
  onApply,
}: Props) {
  const user = useAuthStore((state) => state.user);
  const canManage = Boolean(
    user?.is_admin || user?.permissions.includes('experiments:create'),
  );
  const [protocols, setProtocols] = useState<FactorProtocolSeries[]>([]);
  const [name, setName] = useState('因子有效性预注册');
  const [question, setQuestion] = useState('该因子在锁定窗口和成本后是否仍具有稳定预测能力？');
  const [hypothesis, setHypothesis] = useState('主周期 RankIC 与多空收益达到预设门槛，且结果可由固定数据版本复现。');
  const [rankIcMean, setRankIcMean] = useState(0.02);
  const [rankIcIr, setRankIcIr] = useState(0.3);
  const [longShortMean, setLongShortMean] = useState(0);
  const [minimumRuns, setMinimumRuns] = useState(1);
  const [datasetDigest, setDatasetDigest] = useState('');
  const [requireThresholds, setRequireThresholds] = useState(true);
  const [requireDataset, setRequireDataset] = useState(true);
  const [allowExport, setAllowExport] = useState(true);
  const [selectedProtocolId, setSelectedProtocolId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    const items = await listFactorProtocols();
    setProtocols(items);
    setSelectedProtocolId((current) => (
      items.some((item) => item.protocol_id === current)
        ? current
        : items[0]?.protocol_id ?? ''
    ));
  };

  useEffect(() => {
    void refresh().catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : '研究协议加载失败');
    });
  }, []);

  const payload = useMemo<FactorProtocolPayload | null>(() => (
    base ? {
      ...base,
      data: datasetDigest
        ? {
          ...base.data,
          version_policy: 'pinned_dataset_digest',
          expected_dataset_digest: datasetDigest,
        }
        : {
          ...base.data,
          version_policy: 'latest_trusted_at_execution',
          expected_dataset_digest: null,
        },
      question: question.trim(),
      hypothesis: hypothesis.trim(),
      thresholds: {
        rank_ic_mean_min: rankIcMean,
        rank_ic_ir_min: rankIcIr,
        long_short_mean_min: longShortMean,
      },
      export_rules: {
        allow_strategy_export: allowExport,
        require_all_thresholds: requireThresholds,
        require_dataset_consistency: requireDataset,
        minimum_evidence_runs: minimumRuns,
      },
    } : null
  ), [
    allowExport,
    base,
    datasetDigest,
    hypothesis,
    longShortMean,
    minimumRuns,
    question,
    rankIcIr,
    rankIcMean,
    requireDataset,
    requireThresholds,
  ]);
  const selected = protocols.find((item) => item.protocol_id === selectedProtocolId);

  const create = async (nextVersion: boolean) => {
    if (!payload || question.trim().length < 8 || hypothesis.trim().length < 8) return;
    setBusy(true);
    setError(null);
    try {
      const result = nextVersion && selected
        ? await createFactorProtocolVersion({
          protocol_id: selected.protocol_id,
          expected_current_version: selected.current_version,
          payload,
        })
        : await createFactorProtocol({ name: name.trim(), payload });
      await refresh();
      setSelectedProtocolId(result.protocol_id);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '研究协议保存失败');
    } finally {
      setBusy(false);
    }
  };

  const lock = async (version: FactorProtocolVersion) => {
    setBusy(true);
    setError(null);
    try {
      await lockFactorProtocol(version);
      await refresh();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '研究协议锁定失败');
    } finally {
      setBusy(false);
    }
  };

  const apply = (version: FactorProtocolVersion) => {
    setDatasetDigest(version.payload.data.expected_dataset_digest ?? '');
    onApply(version.payload, {
      protocol_id: version.protocol_id,
      version: version.version,
      payload_digest: version.payload_digest,
    });
  };

  return (
    <Card
      id="factor-protocols"
      className="mt-4 scroll-mt-16"
      title="研究协议与预注册审查"
      description="问题、假设、数据策略、窗口、成本、阈值和导出规则按版本固化；任何修改都会创建新版本。"
    >
      {error && <Banner variant="danger" className="mb-3">{error}</Banner>}
      {!canManage && (
        <Banner variant="info" className="mb-3">
          当前账号可查看和应用已有协议；创建新版本或锁定协议需要实验创建权限。
        </Banner>
      )}
      {activeReference && (
        <Banner variant="ok" className="mb-3" title="本次研究已绑定锁定协议">
          {activeReference.protocol_id} v{activeReference.version} · 摘要 {activeReference.payload_digest.slice(0, 12)}…
        </Banner>
      )}
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="space-y-3">
          <Input label="协议名称" value={name} onChange={(event) => setName(event.target.value)} />
          <label className="block text-sm font-medium text-ink-700">
            研究问题
            <textarea
              className="mt-1 block min-h-20 w-full rounded border border-ink-300 bg-surface px-3 py-2 text-sm"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
            />
          </label>
          <label className="block text-sm font-medium text-ink-700">
            可证伪假设
            <textarea
              className="mt-1 block min-h-20 w-full rounded border border-ink-300 bg-surface px-3 py-2 text-sm"
              value={hypothesis}
              onChange={(event) => setHypothesis(event.target.value)}
            />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <Input label="RankIC 均值门槛" type="number" step={0.01} min={-1} max={1}
              value={rankIcMean} onChange={(event) => setRankIcMean(Number(event.target.value))} />
            <Input label="RankIC IR 门槛" type="number" step={0.1}
              value={rankIcIr} onChange={(event) => setRankIcIr(Number(event.target.value))} />
            <Input label="多空均值门槛" type="number" step={0.001} min={-1} max={1}
              value={longShortMean} onChange={(event) => setLongShortMean(Number(event.target.value))} />
            <Input label="最少证据运行" type="number" min={1} max={20}
              value={minimumRuns} onChange={(event) => setMinimumRuns(Number(event.target.value))} />
          </div>
          <Input
            label="固定数据摘要（可选）"
            value={datasetDigest}
            error={datasetDigest && !/^[0-9a-f]{64}$/.test(datasetDigest)
              ? '必须是 64 位 SHA-256'
              : undefined}
            hint={suggestedDatasetDigest
              ? `当前已打开运行：${suggestedDatasetDigest.slice(0, 12)}…`
              : '留空表示执行时选择最新可信数据，并在运行证据中固化实际摘要'}
            onChange={(event) => setDatasetDigest(event.target.value.trim().toLowerCase())}
          />
          {suggestedDatasetDigest && (
            <Button size="sm" variant="ghost" onClick={() => setDatasetDigest(suggestedDatasetDigest)}>
              使用当前运行的数据摘要
            </Button>
          )}
          <div className="flex flex-wrap gap-3 text-sm">
            <label><input type="checkbox" checked={requireThresholds}
              onChange={(event) => setRequireThresholds(event.target.checked)} /> 导出前要求全部阈值通过</label>
            <label><input type="checkbox" checked={requireDataset}
              onChange={(event) => setRequireDataset(event.target.checked)} /> 导出证据数据版本一致</label>
            <label><input type="checkbox" checked={allowExport}
              onChange={(event) => setAllowExport(event.target.checked)} /> 允许导出策略</label>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" loading={busy}
              disabled={!canManage || !payload
                || Boolean(datasetDigest && !/^[0-9a-f]{64}$/.test(datasetDigest))}
              onClick={() => void create(false)}>
              新建协议草稿
            </Button>
            <Button size="sm" variant="secondary" loading={busy}
              disabled={!canManage || !payload || !selected
                || Boolean(datasetDigest && !/^[0-9a-f]{64}$/.test(datasetDigest))}
              onClick={() => void create(true)}>
              按当前配置另存新版
            </Button>
          </div>
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-ink-700" htmlFor="factor-protocol-series">
            我的协议
          </label>
          <select
            id="factor-protocol-series"
            className="block w-full rounded border border-ink-300 bg-surface px-3 py-2 text-sm"
            value={selectedProtocolId}
            onChange={(event) => setSelectedProtocolId(event.target.value)}
          >
            {protocols.map((item) => (
              <option key={item.protocol_id} value={item.protocol_id}>
                {item.name} · v{item.current_version}
              </option>
            ))}
          </select>
          <div className="mt-3 max-h-[32rem] space-y-3 overflow-y-auto">
            {selected?.versions.map((version) => (
              <div key={version.version} className="rounded border border-ink-200 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">v{version.version}</span>
                  <StatusTag variant={version.status === 'locked' ? 'verified' : 'warning'}>
                    {version.status === 'locked' ? '已锁定' : '草稿'}
                  </StatusTag>
                </div>
                <p className="mt-1 text-xs text-ink-500">
                  {formatBackendDateTime(version.created_at)} · 已用于 {version.used_run_count} 次运行
                </p>
                <p className="mt-2 text-sm">{version.payload.question}</p>
                <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">
                  <div><dt className="text-ink-500">数据策略</dt><dd>{version.payload.data.version_policy}</dd></div>
                  <div><dt className="text-ink-500">窗口</dt><dd>{version.payload.window.start} ~ {version.payload.window.end}</dd></div>
                  <div className="col-span-2"><dt className="text-ink-500">摘要</dt><dd className="break-all font-mono">{version.payload_digest}</dd></div>
                </dl>
                <div className="mt-3 flex gap-2">
                  {version.status === 'draft' ? (
                    <Button size="sm" variant="secondary"
                      disabled={busy || !canManage}
                      onClick={() => void lock(version)}>
                      审查并锁定
                    </Button>
                  ) : (
                    <Button size="sm" variant="secondary" onClick={() => apply(version)}>
                      应用到本次研究
                    </Button>
                  )}
                </div>
              </div>
            ))}
            {!selected && <p className="text-sm text-ink-500">尚未创建研究协议。</p>}
          </div>
        </div>
      </div>
      <Banner variant="info" className="mt-4">
        锁定只改变生命周期状态，不重写协议内容；已使用版本由数据库触发器保护。协议审查与组合约束均为只读，不会自动发布策略或修改持仓。
      </Banner>
    </Card>
  );
}
