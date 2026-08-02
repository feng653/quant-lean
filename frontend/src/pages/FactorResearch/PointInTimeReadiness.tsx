import type { FactorCacheCapability } from '../../services/factorResearch';

const reasonLabels: Record<string, string> = {
  price_cache_unavailable: '行情缓存不可用',
  point_in_time_store_uninitialized: '点时主数据尚未初始化',
  effective_dated_history_missing: '缺少有效期历史',
  current_snapshot_not_valid_for_historical_research: '仅有当前快照，不能回填历史',
  historical_source_evidence_insufficient: '历史来源证据等级不足',
  historical_membership_empty: '历史成分为空',
  membership_price_coverage_missing: '历史成分缺少对应行情',
  security_effective_period_missing: '证券主数据有效期有缺口',
  industry_effective_period_missing: '行业有效期有缺口',
  point_in_time_integrity_invalid: '点时主数据完整性失败',
  point_in_time_identity_invalid: '证券代码身份不符合点时主数据契约',
  point_in_time_size_field_missing: '缺少点时市值字段',
  point_in_time_size_provenance_missing: '市值字段缺少点时来源证据',
  point_in_time_size_provenance_invalid: '市值字段来源证据无效',
  point_in_time_size_evidence_insufficient: '市值字段来源证据等级不足',
};

function reasonLabel(reason: string | null | undefined): string {
  return reason ? (reasonLabels[reason] ?? reason) : '已覆盖';
}

export function PointInTimeReadinessSummary({
  pool,
}: {
  pool: FactorCacheCapability;
}) {
  return (
    <p>
      点时证券池：{reasonLabel(pool.point_in_time.universe.reason)} ·
      点时行业：{reasonLabel(pool.point_in_time.industry.reason)}。
      {!pool.neutralization_ready && (
        <> 行业中性化不可用，系统不会把今日行业分类回写到历史日期。</>
      )}
      {!pool.neutralization?.size.ready && (
        <> 规模中性化不可用，系统不会用收盘价或今日市值替代点时市值。</>
      )}
    </p>
  );
}

export function PointInTimeReadinessDetails({
  pool,
}: {
  pool: FactorCacheCapability;
}) {
  return (
    <div className="mt-2 grid gap-1 text-xs text-ink-500">
      <p>
        历史成分：
        {pool.point_in_time.universe.ready
          ? '有效期覆盖完整'
          : reasonLabel(pool.point_in_time.universe.reason)}
      </p>
      <p>
        证券主数据：
        {pool.point_in_time.security_master.ready
          ? '有效期覆盖完整'
          : reasonLabel(pool.point_in_time.security_master.reason)}
      </p>
      <p>
        行业中性化：
        {pool.neutralization_ready
          ? '可用'
          : reasonLabel(pool.point_in_time.industry.reason)}
      </p>
      <p>
        规模中性化：
        {pool.neutralization?.size.ready
          ? `可用（${pool.neutralization.size.selected_field}）`
          : reasonLabel(pool.neutralization?.size.reason)}
      </p>
    </div>
  );
}
