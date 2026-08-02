import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { getLiveReadiness, validateExecutionOrder } from '../../services/execution';
import type { LiveCapabilityStatus, LiveReadinessReport, OrderValidation } from '../../services/execution';
import { useAuthStore } from '../../store/authStore';
import Badge from '../../components/shared/Badge';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import Card from '../../components/shared/Card';
import Icon from '../../components/shared/Icon';
import Input from '../../components/shared/Input';
import PageHeader from '../../components/shared/PageHeader';
import Select from '../../components/shared/Select';
import Spinner from '../../components/shared/Spinner';

const CAPABILITY_LABEL: Record<LiveCapabilityStatus, string> = {
  available: '已具备',
  partial: '部分具备',
  missing: '缺失',
  locked: '已锁定',
};

const CAPABILITY_VARIANT: Record<LiveCapabilityStatus, 'success' | 'warning' | 'danger' | 'default'> = {
  available: 'success',
  partial: 'warning',
  missing: 'danger',
  locked: 'default',
};

const DOMAIN_LABEL: Record<string, string> = {
  available: '已具备',
  partial: '待验收',
  blocked: '阻断',
};

const DOMAIN_VARIANT: Record<string, 'success' | 'warning' | 'danger'> = {
  available: 'success',
  partial: 'warning',
  blocked: 'danger',
};

/**
 * Pure presentational render of the machine-readable fail-closed report.
 * Kept free of hooks and browser APIs so it can be server-rendered and
 * independently tested.
 */
export function ReadinessReportContent({ report }: { report: LiveReadinessReport }) {
  return (
    <div className="space-y-5">
      {/* Live gate hero */}
      <section
        aria-labelledby="live-gate-title"
        className="rounded-md border-2 border-danger-fg bg-danger-bg p-5 sm:p-6"
      >
        <div className="flex items-start gap-4">
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-md border border-danger-border bg-surface text-danger-fg">
            <Icon name="lock" className="h-6 w-6" title="实盘锁定" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-2xs font-semibold uppercase tracking-widest text-danger-fg">
              Live trading safety gate
            </p>
            <h1 id="live-gate-title" className="mt-1 text-2xl font-bold text-danger-strong">
              实盘锁定
            </h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-danger-strong">
              {report.summary}
            </p>
            <div className="mt-4 flex flex-wrap items-center gap-6">
              <div>
                <p className="tnum text-2xl font-bold text-danger-strong">{report.blocker_count}</p>
                <p className="text-xs text-danger-fg">必选阻断项</p>
              </div>
              <div>
                <p className="text-2xl font-bold text-danger-strong">未认证</p>
                <p className="text-xs text-danger-fg">认证状态</p>
              </div>
            </div>
            <p className="mt-4 border-t border-danger-border pt-3 font-mono text-2xs leading-5 text-danger-fg">
              能力版本：{report.capability_version}
              {' · '}接口版本：{report.schema_version}
              {' · '}平台范围：研究与模拟盘
            </p>
          </div>
        </div>
      </section>

      {/* Domains */}
      <section aria-labelledby="readiness-domains-title">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 id="readiness-domains-title" className="text-lg font-semibold text-ink-900">
              安全域评估
            </h2>
            <p className="mt-0.5 text-sm text-ink-500">
              任一必选能力不是“已具备”，整个平台都不能获得实盘认证。
            </p>
          </div>
          <Badge variant="danger">门禁拒绝</Badge>
        </div>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {report.domains.map((domain) => (
            <Card key={domain.domain_id} padding="md">
              <div className="mb-3 flex items-center justify-between gap-2">
                <h3 className="text-base font-semibold text-ink-900">{domain.title}</h3>
                <Badge variant={DOMAIN_VARIANT[domain.status] ?? 'default'}>
                  {DOMAIN_LABEL[domain.status] ?? domain.status}
                </Badge>
              </div>
              <ul className="space-y-3">
                {domain.capabilities.map((capability) => (
                  <li key={capability.capability_id} className="rounded border border-ink-100 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-sm font-medium text-ink-800">{capability.label}</p>
                      <Badge variant={CAPABILITY_VARIANT[capability.status]}>
                        {CAPABILITY_LABEL[capability.status]}
                      </Badge>
                    </div>
                    <p className="mt-1 text-xs leading-5 text-ink-500">{capability.evidence}</p>
                    <p className="mt-1 text-2xs text-ink-400">证据来源：{capability.source}</p>
                    {capability.limitation && (
                      <p className="mt-1 text-2xs text-warn-strong">{capability.limitation}</p>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          ))}
        </div>
      </section>

      {/* Adapter evidence */}
      <section aria-labelledby="readiness-adapters-title">
        <h2 id="readiness-adapters-title" className="mb-1 text-lg font-semibold text-ink-900">
          券商适配器证据
        </h2>
        <p className="mb-3 text-sm text-ink-500">
          仅进行本地 SDK、配置与 capability 声明检查，不连接券商或读取账户。
        </p>
        {report.adapters.length === 0 ? (
          <p className="text-sm text-ink-400">没有已声明的券商适配器。</p>
        ) : (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {report.adapters.map((adapter) => (
              <Card key={adapter.adapter_id} padding="md">
                <div className="mb-3 flex items-center justify-between gap-2">
                  <h3 className="text-base font-semibold text-ink-900">{adapter.display_name}</h3>
                  <Badge variant="danger">未认证</Badge>
                </div>
                <p className="mb-3 text-xs leading-5 text-ink-500">{adapter.health_message}</p>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 text-sm">
                  <div>
                    <dt className="text-2xs uppercase tracking-wide text-ink-400">SDK 模块</dt>
                    <dd className="mt-0.5 font-mono text-xs text-ink-700">{adapter.sdk_module ?? '未声明'}</dd>
                  </div>
                  <div>
                    <dt className="text-2xs uppercase tracking-wide text-ink-400">SDK 检测</dt>
                    <dd className="mt-0.5 text-xs text-ink-700">{adapter.sdk_available ? '已发现' : '未发现'}</dd>
                  </div>
                  <div>
                    <dt className="text-2xs uppercase tracking-wide text-ink-400">健康声明</dt>
                    <dd className="mt-0.5 text-xs text-ink-700">{adapter.health_status}</dd>
                  </div>
                  <div>
                    <dt className="text-2xs uppercase tracking-wide text-ink-400">真实下单</dt>
                    <dd className="mt-0.5 text-xs font-medium text-danger-strong">
                      {adapter.declared_capabilities.live_order_submission === true
                        ? '异常声明，仍阻断'
                        : adapter.declared_capabilities.live_order_submission === false
                          ? '已锁定'
                          : '能力未知，仍阻断'}
                    </dd>
                  </div>
                </dl>
                {adapter.missing_config.length > 0 && (
                  <p className="mt-3 text-xs text-ink-500">
                    缺少配置名称：{adapter.missing_config.join('、')}
                  </p>
                )}
              </Card>
            ))}
          </div>
        )}
      </section>

      {/* Acceptance checklist */}
      <section aria-labelledby="readiness-blockers-title">
        <h2 id="readiness-blockers-title" className="mb-1 text-lg font-semibold text-ink-900">
          后续验收清单
        </h2>
        <p className="mb-3 text-sm text-ink-500">
          下列事项必须提供可复核证据并发布新的能力版本，不能只修改配置或安装 SDK。
        </p>
        <ol className="space-y-2.5">
          {report.blockers.map((blocker, index) => (
            <li key={blocker.blocker_id} className="flex gap-3 rounded border border-ink-200 bg-surface p-3.5">
              <span className="tnum flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-danger-bg text-xs font-semibold text-danger-strong">
                {index + 1}
              </span>
              <div>
                <p className="text-sm font-medium text-ink-800">{blocker.title}</p>
                <p className="mt-0.5 text-xs leading-5 text-ink-500">{blocker.remediation}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      {/* Report boundaries */}
      {report.limitations.length > 0 && (
        <Banner variant="warning" title="报告边界">
          <ul className="list-disc space-y-1 pl-4">
            {report.limitations.map((limitation, index) => (
              <li key={index}>{limitation}</li>
            ))}
          </ul>
        </Banner>
      )}
    </div>
  );
}

export default function BrokerReadinessPage() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const canExecute = user?.is_admin || user?.permissions.includes('trading:execute');

  const [report, setReport] = useState<LiveReadinessReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [adapterId, setAdapterId] = useState('');
  const [symbol, setSymbol] = useState('600000.SH');
  const [accountId, setAccountId] = useState('');
  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [orderType, setOrderType] = useState<'market' | 'limit'>('limit');
  const [quantity, setQuantity] = useState('100');
  const [limitPrice, setLimitPrice] = useState('10.00');
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState<OrderValidation | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await getLiveReadiness();
      setReport(result);
      setAdapterId((current) => current || result.adapters[0]?.adapter_id || '');
    } catch (err: unknown) {
      setReport(null);
      setError(err instanceof Error ? err.message : '读取实盘安全门禁失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const validateOrder = async () => {
    if (!adapterId) return;
    setValidating(true);
    setValidation(null);
    setValidationError(null);
    try {
      const result = await validateExecutionOrder({
        adapter_id: adapterId,
        order: {
          symbol: symbol.trim(),
          side,
          order_type: orderType,
          quantity: Number(quantity),
          ...(orderType === 'limit' ? { limit_price: Number(limitPrice) } : {}),
          ...(accountId.trim() ? { account_id: accountId.trim() } : {}),
        },
      });
      setValidation(result);
    } catch (err: unknown) {
      setValidationError(err instanceof Error ? err.message : '订单预检失败');
    } finally {
      setValidating(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="券商与实盘准备"
        description="交易工作台 / 安全治理。此页面不展示或生成虚构账户、持仓和行情。"
        breadcrumb={[{ label: '执行' }, { label: '交易工作台', to: '/trading' }, { label: '安全治理' }]}
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={() => void load()} loading={loading}>
              <Icon name="refresh" className="h-4 w-4" />
              重新检查
            </Button>
            <Button variant="ghost" size="sm" onClick={() => navigate('/trading')}>
              <Icon name="arrowLeft" className="h-4 w-4" />
              返回交易工作台
            </Button>
          </>
        }
      />

      {loading && (
        <div role="status" aria-live="polite" className="flex items-center justify-center py-16">
          <Spinner size="lg" />
          <span className="sr-only">正在读取实盘安全门禁</span>
        </div>
      )}

      {!loading && error && (
        <div className="space-y-5">
          <Banner variant="danger">
            门禁报告读取失败，状态按实盘锁定处理：{error}
          </Banner>
          <section
            aria-labelledby="fail-closed-title"
            className="rounded-md border-2 border-danger-fg bg-danger-bg p-6 text-center"
          >
            <Icon name="lock" className="mx-auto h-8 w-8 text-danger-fg" aria-hidden />
            <h1 id="fail-closed-title" className="mt-2 text-2xl font-bold text-danger-strong">
              实盘锁定
            </h1>
            <p className="mt-2 text-sm text-danger-strong">
              无法取得机器可读的安全证据，平台不会推断或降级为可用状态。
            </p>
          </section>
        </div>
      )}

      {!loading && report && (
        <>
          <ReadinessReportContent report={report} />

          {/* Local order format precheck — explicitly non-submitting */}
          <Card
            className="mt-5"
            title="订单意图本地预检"
            description="只验证统一订单格式，不连接券商，不查询账户，也不构成实盘能力。"
            actions={<Badge variant="default">非提交接口</Badge>}
          >
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <Select
                label="适配器"
                value={adapterId}
                onChange={(event) => setAdapterId(event.target.value)}
                options={report.adapters.map((adapter) => ({
                  value: adapter.adapter_id,
                  label: adapter.display_name,
                }))}
              />
              <Input
                label="证券代码"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
              />
              <Input
                label="账户 ID（可选）"
                value={accountId}
                onChange={(event) => setAccountId(event.target.value)}
              />
              <Select
                label="方向"
                value={side}
                onChange={(event) => setSide(event.target.value as 'buy' | 'sell')}
                options={[
                  { value: 'buy', label: '买入' },
                  { value: 'sell', label: '卖出' },
                ]}
              />
              <Select
                label="订单类型"
                value={orderType}
                onChange={(event) => setOrderType(event.target.value as 'market' | 'limit')}
                options={[
                  { value: 'limit', label: '限价' },
                  { value: 'market', label: '市价' },
                ]}
              />
              <Input
                label="数量"
                type="number"
                min={1}
                step={100}
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
              />
              {orderType === 'limit' && (
                <Input
                  label="限价"
                  type="number"
                  step={0.01}
                  value={limitPrice}
                  onChange={(event) => setLimitPrice(event.target.value)}
                />
              )}
            </div>
            <div className="mt-4 flex items-center gap-3">
              <Button
                onClick={() => void validateOrder()}
                loading={validating}
                disabled={!adapterId || !canExecute}
              >
                执行本地格式预检
              </Button>
              {!canExecute && (
                <span className="text-xs text-ink-400">需要 trading:execute 权限</span>
              )}
              <span className="text-xs text-ink-400">此按钮没有真实订单提交路径。</span>
            </div>

            <div aria-live="polite" className="mt-4">
              {validationError && (
                <Banner variant="danger">{validationError}</Banner>
              )}
              {validation && (
                <div className="rounded border border-ink-200 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={validation.valid ? 'success' : 'danger'}>
                      {validation.valid ? '订单格式有效' : '格式预检未通过'}
                    </Badge>
                    <Badge variant="danger">实盘能力：未认证，门禁仍锁定</Badge>
                  </div>
                  {validation.errors.length > 0 && (
                    <ul className="mt-3 list-disc space-y-1 pl-4 text-sm text-danger-strong">
                      {validation.errors.map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  )}
                  {validation.warnings.length > 0 && (
                    <ul className="mt-2 list-disc space-y-1 pl-4 text-sm text-warn-strong">
                      {validation.warnings.map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
