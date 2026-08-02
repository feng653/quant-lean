import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { FactorCacheCapability } from '../../services/factorResearch';
import {
  PointInTimeReadinessDetails,
  PointInTimeReadinessSummary,
} from './PointInTimeReadiness';

const pool = {
  pool_id: 'csi300',
  neutralization_ready: false,
  point_in_time: {
    universe: { ready: true, reason: null },
    security_master: { ready: true, reason: null },
    industry: {
      ready: false,
      neutralization_ready: false,
      reason: 'current_snapshot_not_valid_for_historical_research',
    },
  },
} as FactorCacheCapability;

describe('point-in-time factor readiness', () => {
  it('explains why a current industry snapshot cannot neutralize history', () => {
    const html = renderToStaticMarkup(
      <>
        <PointInTimeReadinessSummary pool={pool} />
        <PointInTimeReadinessDetails pool={pool} />
      </>,
    );

    expect(html).toContain('系统不会把今日行业分类回写到历史日期');
    expect(html).toContain('仅有当前快照，不能回填历史');
    expect(html).toContain('历史成分：有效期覆盖完整');
  });
});
