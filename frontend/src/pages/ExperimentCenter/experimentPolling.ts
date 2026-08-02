import type { Experiment } from '../../types/experiment';

export const EXPERIMENT_POLL_INTERVAL_MS = 2_500;
export const MAX_EXPERIMENT_POLL_ATTEMPTS = 240;

const ACTIVE_EXPERIMENT_STATUSES: ReadonlySet<Experiment['status']> = new Set([
  'pending',
  'running',
]);

export function shouldPollExperiment(
  status: Experiment['status'],
  attempts: number,
  maxAttempts: number = MAX_EXPERIMENT_POLL_ATTEMPTS,
): boolean {
  return ACTIVE_EXPERIMENT_STATUSES.has(status) && attempts < maxAttempts;
}
