import type {
  FactorCacheCapability,
  NeutralizationMode,
} from '../../services/factorResearch';

const REASONS: Record<string, string> = {
  point_in_time_store_uninitialized: '点时行业主数据尚未初始化',
  effective_dated_history_missing: '缺少有效期行业历史',
  current_snapshot_not_valid_for_historical_research: '只有当前行业快照，不能回填历史',
  historical_source_evidence_insufficient: '行业历史来源证据等级不足',
  industry_effective_period_missing: '行业有效期覆盖存在缺口',
  point_in_time_size_field_missing: '缓存不含市值或流通市值字段',
  point_in_time_size_provenance_missing: '市值字段缺少点时 provenance',
  point_in_time_size_provenance_invalid: '市值字段 provenance 无效',
  point_in_time_size_field_identity_mismatch: '市值字段与 provenance 身份不一致',
  point_in_time_size_semantics_missing: '未证明市值按交易日点时可用',
  point_in_time_size_availability_invalid: '市值可用时点可能包含未来信息',
  point_in_time_size_evidence_insufficient: '市值来源证据等级不足',
};

export function neutralizationUnavailableReason(
  pool: FactorCacheCapability | undefined,
  mode: NeutralizationMode,
): string | null {
  if (mode === 'none') return null;
  const readiness = pool?.neutralization?.modes[mode];
  if (!readiness) return '服务尚未提供该中性化模式的就绪证明';
  if (readiness.ready) return null;
  return REASONS[readiness.reason ?? ''] ?? readiness.reason ?? '暴露数据不可用';
}
