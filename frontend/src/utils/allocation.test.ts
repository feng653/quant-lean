import { describe, expect, it } from 'vitest';
import { allocateBasisPoints } from './allocation';

describe('allocateBasisPoints', () => {
  it('keeps locked rows and distributes an exact remainder', () => {
    const result = allocateBasisPoints([
      { deploymentId: 1, locked: true, currentBps: 2_500, minBps: 0, maxBps: 10_000, score: 0 },
      { deploymentId: 2, locked: false, currentBps: 0, minBps: 0, maxBps: 10_000, score: 1 },
      { deploymentId: 3, locked: false, currentBps: 0, minBps: 0, maxBps: 10_000, score: 1 },
    ]);

    expect(result.get(1)).toBe(2_500);
    expect([...result.values()].reduce((sum, value) => sum + value, 0)).toBe(10_000);
    expect(Math.abs((result.get(2) ?? 0) - (result.get(3) ?? 0))).toBeLessThanOrEqual(1);
  });

  it('honours minimum and maximum weights', () => {
    const result = allocateBasisPoints([
      { deploymentId: 1, locked: false, currentBps: 0, minBps: 1_000, maxBps: 2_000, score: 100 },
      { deploymentId: 2, locked: false, currentBps: 0, minBps: 500, maxBps: 10_000, score: 1 },
    ]);

    expect(result.get(1)).toBe(2_000);
    expect(result.get(2)).toBe(8_000);
  });

  it('leaves cash when maximum capacities are below one hundred percent', () => {
    const result = allocateBasisPoints([
      { deploymentId: 1, locked: false, currentBps: 0, minBps: 0, maxBps: 2_000, score: 1 },
      { deploymentId: 2, locked: false, currentBps: 0, minBps: 0, maxBps: 3_000, score: 1 },
    ]);

    expect([...result.values()].reduce((sum, value) => sum + value, 0)).toBe(5_000);
  });
});
