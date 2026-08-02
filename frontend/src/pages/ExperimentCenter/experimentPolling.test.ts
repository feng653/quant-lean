import { describe, expect, it } from 'vitest';
import {
  MAX_EXPERIMENT_POLL_ATTEMPTS,
  shouldPollExperiment,
} from './experimentPolling';

describe('experiment detail polling policy', () => {
  it.each(['pending', 'running'] as const)(
    'continues polling an active %s experiment below the limit',
    (status) => {
      expect(shouldPollExperiment(status, 1)).toBe(true);
    },
  );

  it.each(['completed', 'failed', 'cancelled'] as const)(
    'stops immediately for terminal status %s',
    (status) => {
      expect(shouldPollExperiment(status, 1)).toBe(false);
    },
  );

  it('stops an active experiment at the finite attempt limit', () => {
    expect(
      shouldPollExperiment('running', MAX_EXPERIMENT_POLL_ATTEMPTS),
    ).toBe(false);
  });
});
