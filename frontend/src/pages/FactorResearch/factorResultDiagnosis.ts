import type { FactorResearchResult } from '../../services/factorResearch';

export type FactorResultCheckStatus = 'passed' | 'attention' | 'blocked';

export interface FactorResultCheck {
  id: 'integrity' | 'protocol' | 'stability' | 'implementation' | 'capacity';
  label: string;
  status: FactorResultCheckStatus;
  summary: string;
  target: string;
}

export interface FactorResultDiagnosis {
  decision: 'candidate' | 'incomplete' | 'blocked';
  title: string;
  summary: string;
  checks: FactorResultCheck[];
  nextSteps: Array<{
    text: string;
    target: string;
  }>;
}

function numberMetric(value: number | null | undefined, digits = 4): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(digits)
    : '-';
}

function integrityCheck(result: FactorResearchResult): FactorResultCheck {
  const run = result.run;
  const complete = Boolean(
    run?.run_id
    && run.request_digest
    && run.dataset_digest
    && run.result_digest
    && run.run_digest
    && result.dataset.content_sha256,
  );
  if (!complete) {
    return {
      id: 'integrity',
      label: '不可变证据',
      status: 'attention',
      summary: '运行或数据摘要不完整；旧版结果只能作为线索，不能作为晋级证据。',
      target: 'factor-result-evidence',
    };
  }
  if (run?.archived_at) {
    return {
      id: 'integrity',
      label: '不可变证据',
      status: 'attention',
      summary: '摘要完整，但该运行已归档；如需继续研究，应创建新的不可变运行。',
      target: 'factor-result-evidence',
    };
  }
  return {
    id: 'integrity',
    label: '不可变证据',
    status: 'passed',
    summary: '请求、数据、结果与运行摘要齐全。',
    target: 'factor-result-evidence',
  };
}

function protocolCheck(result: FactorResearchResult): FactorResultCheck {
  if (!result.protocol_review) {
    return {
      id: 'protocol',
      label: '预注册协议',
      status: 'attention',
      summary: '未绑定可审查的锁定协议；本次发现不应被当作预注册结论。',
      target: 'factor-protocols',
    };
  }
  if (!result.protocol_review.passed) {
    const failed = result.protocol_review.checks.filter((check) => !check.passed).length;
    return {
      id: 'protocol',
      label: '预注册协议',
      status: 'blocked',
      summary: `${failed} 项预注册门槛未通过；不得事后放宽门槛包装结果。`,
      target: 'factor-protocol-review',
    };
  }
  return {
    id: 'protocol',
    label: '预注册协议',
    status: 'passed',
    summary: '已按只读协议审查，全部预先声明的门槛通过。',
    target: 'factor-protocol-review',
  };
}

function stabilityCheck(result: FactorResearchResult): FactorResultCheck {
  const stability = result.stability;
  if (!stability) {
    return {
      id: 'stability',
      label: '样本外稳定性',
      status: 'attention',
      summary: '缺少训练、验证、锁定三段证据；当前结果不能回答样本外是否稳定。',
      target: 'factor-stability-results',
    };
  }
  const summary = stability.stability_summary;
  const locked = stability.windows.find((window) => window.role === 'locked');
  const lockedHorizon = locked?.horizons[String(summary.primary_horizon)];
  const lockedRankIc = lockedHorizon?.ic.summary.rank_ic.mean;
  const lockedSignPositive = typeof lockedRankIc === 'number' && lockedRankIc > 0;
  const lockedMultiplicityPassed = Boolean(
    lockedHorizon?.multiple_testing.passes_adjusted_alpha,
  );
  if (
    summary.windows_with_evaluable_primary_ic < 3
    || !summary.rank_ic_sign_consistent
    || !lockedSignPositive
    || !lockedMultiplicityPassed
  ) {
    const reasons = [
      summary.windows_with_evaluable_primary_ic < 3 ? '三段窗口未全部可评估' : null,
      !summary.rank_ic_sign_consistent ? '分窗 RankIC 不同号' : null,
      !lockedSignPositive ? `锁定窗 RankIC 为 ${numberMetric(lockedRankIc)}` : null,
      !lockedMultiplicityPassed ? '锁定窗未通过多重检验校正' : null,
    ].filter(Boolean);
    return {
      id: 'stability',
      label: '样本外稳定性',
      status: 'blocked',
      summary: `${reasons.join('；')}。不要用锁定窗继续调参。`,
      target: 'factor-stability-results',
    };
  }
  return {
    id: 'stability',
    label: '样本外稳定性',
    status: 'passed',
    summary: `三段窗口可评估且同号；锁定窗 RankIC ${numberMetric(lockedRankIc)}，通过校正检验。`,
    target: 'factor-stability-results',
  };
}

function implementationCheck(result: FactorResearchResult): FactorResultCheck {
  const implementation = result.implementation;
  if (!implementation) {
    return {
      id: 'implementation',
      label: '成本后表现',
      status: 'attention',
      summary: '缺少换手和成本后收益证据；毛收益不能替代可实施性。',
      target: 'factor-implementation-results',
    };
  }
  const netMean = implementation.net_default.long_short.mean;
  if (implementation.status !== 'available' || netMean == null) {
    return {
      id: 'implementation',
      label: '成本后表现',
      status: 'blocked',
      summary: '有效样本不足，无法形成成本后结论。',
      target: 'factor-implementation-results',
    };
  }
  if (netMean <= 0) {
    return {
      id: 'implementation',
      label: '成本后表现',
      status: 'blocked',
      summary: `默认 ${implementation.assumptions.default_cost_bps} bps 成本后的多空均值为 ${numberMetric(netMean)}，未保留正向收益。`,
      target: 'factor-implementation-results',
    };
  }
  return {
    id: 'implementation',
    label: '成本后表现',
    status: 'passed',
    summary: `默认 ${implementation.assumptions.default_cost_bps} bps 成本后的多空均值为 ${numberMetric(netMean)}。`,
    target: 'factor-implementation-results',
  };
}

function capacityCheck(result: FactorResearchResult): FactorResultCheck {
  const capacity = result.implementation?.capacity;
  if (!capacity || capacity.status === 'unavailable') {
    return {
      id: 'capacity',
      label: '容量证据',
      status: 'attention',
      summary: '缺少可信成交额容量证据；不得外推可承载资金规模。',
      target: 'factor-implementation-results',
    };
  }
  if (capacity.status === 'partial') {
    return {
      id: 'capacity',
      label: '容量证据',
      status: 'attention',
      summary: `仅 ${capacity.available_rebalance_dates} / ${capacity.total_rebalance_dates} 个调仓日可评估容量。`,
      target: 'factor-implementation-results',
    };
  }
  return {
    id: 'capacity',
    label: '容量证据',
    status: 'passed',
    summary: `${capacity.available_rebalance_dates} 个调仓日具有容量场景证据。`,
    target: 'factor-implementation-results',
  };
}

export function diagnoseFactorResult(result: FactorResearchResult): FactorResultDiagnosis {
  const checks = [
    integrityCheck(result),
    protocolCheck(result),
    stabilityCheck(result),
    implementationCheck(result),
    capacityCheck(result),
  ];
  const blocked = checks.filter((check) => check.status === 'blocked');
  const attention = checks.filter((check) => check.status === 'attention');

  const decision = blocked.length > 0
    ? 'blocked'
    : attention.length > 0
      ? 'incomplete'
      : 'candidate';
  const titles = {
    blocked: '当前证据不支持进入策略池',
    incomplete: '证据不完整，暂缓导出',
    candidate: '可进入下一轮人工复核',
  } as const;
  const summaries = {
    blocked: `发现 ${blocked.length} 个阻断项。保留本次负结果，不要围绕锁定样本事后优化。`,
    incomplete: `仍有 ${attention.length} 项证据缺口。补齐前不能把本次结果解释为可交易因子。`,
    candidate: '全部基础证据门槛已通过；这只代表可继续复核，不代表已证明因果关系、未来收益或实盘适用性。',
  } as const;

  const nextSteps = [...blocked, ...attention].map((check) => ({
    text: check.status === 'blocked'
      ? `处理阻断项：${check.label}`
      : `补齐证据：${check.label}`,
    target: check.target,
  }));
  if (nextSteps.length === 0) {
    nextSteps.push(
      { text: '与同数据版本的其他因子横向比较', target: 'factor-history' },
      { text: '人工审查全部限制与证据摘要', target: 'factor-result-evidence' },
    );
  }

  return {
    decision,
    title: titles[decision],
    summary: summaries[decision],
    checks,
    nextSteps,
  };
}
