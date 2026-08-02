import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getLiveReadiness } from './execution';

const getMock = vi.hoisted(() => vi.fn());

vi.mock('./api', () => ({
  default: {
    get: getMock,
  },
}));

describe('execution live readiness service', () => {
  beforeEach(() => {
    getMock.mockReset();
  });

  it('reads the machine-readable fail-closed report', async () => {
    getMock.mockResolvedValueOnce({
      data: {
        data: {
          schema_version: 'live-readiness/v1',
          capability_version: '2026-07-28.1',
          ready: false,
          certification: 'not_certified',
          platform_scope: 'research_and_paper_trading_only',
          summary: '实盘锁定',
          blocker_count: 1,
          domains: [],
          blockers: [],
          adapters: [],
          limitations: [],
        },
      },
    });

    const report = await getLiveReadiness();

    expect(getMock).toHaveBeenCalledWith('/api/execution/live-readiness');
    expect(report.ready).toBe(false);
    expect(report.certification).toBe('not_certified');
    expect(report.platform_scope).toBe('research_and_paper_trading_only');
  });

  it('fails closed when the response has no report envelope', async () => {
    getMock.mockResolvedValueOnce({ data: {} });

    await expect(getLiveReadiness()).rejects.toThrow('读取实盘安全门禁失败');
  });
});
