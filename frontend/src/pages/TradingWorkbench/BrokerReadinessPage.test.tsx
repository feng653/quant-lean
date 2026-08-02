import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { LiveReadinessReport } from '../../services/execution';
import { ReadinessReportContent } from './BrokerReadinessPage';

vi.mock('../../store/authStore', () => ({
  useAuthStore: vi.fn(),
}));

const REPORT: LiveReadinessReport = {
  schema_version: 'live-readiness/v1',
  capability_version: '2026-07-28.1',
  ready: false,
  certification: 'not_certified',
  platform_scope: 'research_and_paper_trading_only',
  summary: '实盘交易未认证且保持锁定。',
  blocker_count: 2,
  domains: [
    {
      domain_id: 'broker_lifecycle',
      title: '券商订单全生命周期',
      status: 'blocked',
      capabilities: [
        {
          capability_id: 'live_order_submission',
          label: '真实订单提交',
          status: 'locked',
          required: true,
          evidence: '所有适配器均保持关闭。',
          source: 'adapter.capabilities.live_order_submission',
        },
        {
          capability_id: 'fill_stream_reconciliation',
          label: '成交回报与日终对账',
          status: 'missing',
          required: true,
          evidence: '没有成交推送和对账能力。',
          source: 'execution adapter contract',
        },
      ],
    },
  ],
  blockers: [
    {
      blocker_id: 'broker_lifecycle:live_order_submission',
      domain_id: 'broker_lifecycle',
      capability_id: 'live_order_submission',
      title: '真实订单提交',
      evidence: '所有适配器均保持关闭。',
      remediation: '在测试账户完成全生命周期验收。',
    },
    {
      blocker_id: 'broker_lifecycle:fill_stream_reconciliation',
      domain_id: 'broker_lifecycle',
      capability_id: 'fill_stream_reconciliation',
      title: '成交回报与日终对账',
      evidence: '没有成交推送和对账能力。',
      remediation: '建立逐笔回报和日终对账。',
    },
  ],
  adapters: [
    {
      adapter_id: 'qmt',
      display_name: '国金证券 QMT',
      recognized_scaffold: true,
      certified: false,
      health_status: 'unavailable',
      health_ready: false,
      health_message: '未安装可选 SDK。',
      sdk_module: 'xtquant',
      sdk_available: false,
      missing_config: ['QMT_ACCOUNT_ID'],
      declared_capabilities: {
        supported_order_types: ['market', 'limit'],
        supports_account_query: true,
        supports_position_query: true,
        supports_order_validation: true,
        supports_order_cancel: false,
        live_order_submission: false,
      },
      fail_closed: true,
      blockers: ['adapter_not_certified', 'live_submission_locked'],
    },
  ],
  limitations: [
    '本报告不连接券商、不读取账户。',
    '模拟盘不属于实盘能力。',
  ],
};

describe('BrokerReadinessPage report', () => {
  it('renders a serious fail-closed gate and acceptance evidence', () => {
    const html = renderToStaticMarkup(
      <ReadinessReportContent report={REPORT} />,
    );

    expect(html).toContain('实盘锁定');
    expect(html).toContain('未认证');
    expect(html).toContain('券商订单全生命周期');
    expect(html).toContain('成交回报与日终对账');
    expect(html).toContain('后续验收清单');
    expect(html).toContain('本报告不连接券商、不读取账户');
    expect(html).toContain('aria-labelledby="live-gate-title"');
    expect(html).not.toContain('可真实提交');
  });

  it('labels configured adapter declarations as evidence, not certification', () => {
    const html = renderToStaticMarkup(
      <ReadinessReportContent report={REPORT} />,
    );

    expect(html).toContain('国金证券 QMT');
    expect(html).toContain('未发现');
    expect(html).toContain('已锁定');
    expect(html).toContain('仅进行本地 SDK、配置与 capability 声明检查');
  });
});
