export interface AllocationInput {
  deploymentId: number;
  locked: boolean;
  currentBps: number;
  minBps: number;
  maxBps: number;
  score: number;
}

/**
 * Allocate integer basis points while preserving locked rows and bounds.
 *
 * The result is deterministic and sums to totalBps whenever the configured
 * bounds leave enough capacity. An infeasible set of bounds is intentionally
 * returned as-is so the server-side validator can explain the conflict.
 */
export function allocateBasisPoints(
  inputs: AllocationInput[],
  totalBps = 10_000,
): Map<number, number> {
  const result = new Map<number, number>();
  const unlocked = inputs.filter((item) => !item.locked);
  let lockedTotal = 0;

  for (const item of inputs) {
    if (item.locked) {
      const value = Math.round(item.currentBps);
      result.set(item.deploymentId, value);
      lockedTotal += value;
    }
  }

  let assigned = lockedTotal;
  for (const item of unlocked) {
    const minimum = Math.max(0, Math.round(item.minBps));
    result.set(item.deploymentId, minimum);
    assigned += minimum;
  }

  let remaining = Math.max(totalBps - assigned, 0);
  while (remaining > 0) {
    const candidates = unlocked.filter((item) => (
      (result.get(item.deploymentId) ?? 0) < Math.max(0, Math.round(item.maxBps))
    ));
    if (candidates.length === 0) break;

    const positiveScoreTotal = candidates.reduce(
      (sum, item) => sum + Math.max(0, item.score),
      0,
    );
    const proposals = candidates.map((item) => {
      const score = positiveScoreTotal > 0 ? Math.max(0, item.score) : 1;
      const denominator = positiveScoreTotal > 0 ? positiveScoreTotal : candidates.length;
      const ideal = remaining * score / denominator;
      const current = result.get(item.deploymentId) ?? 0;
      const capacity = Math.max(0, Math.round(item.maxBps) - current);
      return {
        item,
        amount: Math.min(capacity, Math.floor(ideal)),
        fraction: ideal - Math.floor(ideal),
        capacity,
      };
    });

    let distributed = 0;
    for (const proposal of proposals) {
      if (proposal.amount <= 0) continue;
      result.set(
        proposal.item.deploymentId,
        (result.get(proposal.item.deploymentId) ?? 0) + proposal.amount,
      );
      proposal.capacity -= proposal.amount;
      distributed += proposal.amount;
    }
    remaining -= distributed;
    if (remaining <= 0) break;

    proposals.sort((a, b) => (
      b.fraction - a.fraction || a.item.deploymentId - b.item.deploymentId
    ));
    let remainderDistributed = 0;
    for (const proposal of proposals) {
      if (remaining <= 0) break;
      if (proposal.capacity <= 0) continue;
      result.set(
        proposal.item.deploymentId,
        (result.get(proposal.item.deploymentId) ?? 0) + 1,
      );
      proposal.capacity -= 1;
      remaining -= 1;
      remainderDistributed += 1;
    }

    if (distributed === 0 && remainderDistributed === 0) break;
  }

  return result;
}
