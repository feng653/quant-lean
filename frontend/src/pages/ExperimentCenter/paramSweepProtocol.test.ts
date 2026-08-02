import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createSweep,
  getExperimentPicker,
  promoteSweepExperiment,
  type CreateSweepResponse,
  type SweepData,
} from '../../services/experiments';
import {
  canPromoteSweepTrial,
  compareSweepResults,
  hasSweepWindowErrors,
  mapSelectionResult,
  parsePositiveQueryId,
  restoredSweepPromotion,
  validateStrictSweepWindows,
} from './paramSweepProtocol';

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  default: { get, post },
}));

afterEach(() => {
  get.mockReset();
  post.mockReset();
});

describe('strict sweep window protocol', () => {
  it('requires a strictly separated locked final-test window', () => {
    const touching = validateStrictSweepWindows({
      selectionStart: '2025-01-01',
      selectionEnd: '2025-06-30',
      lockedTestStart: '2025-06-30',
      lockedTestEnd: '2025-12-31',
    });
    const overlapping = validateStrictSweepWindows({
      selectionStart: '2025-01-01',
      selectionEnd: '2025-06-30',
      lockedTestStart: '2025-06-01',
      lockedTestEnd: '2025-12-31',
    });

    expect(touching.lockedTestStart).toContain('严格晚于');
    expect(overlapping.lockedTestStart).toContain('不能重叠');
  });

  it('rejects an incomplete window and training that reaches selection', () => {
    const errors = validateStrictSweepWindows({
      selectionStart: '2025-01-01',
      selectionEnd: '2025-06-30',
      lockedTestStart: '',
      lockedTestEnd: '',
      trainStart: '2024-01-01',
      trainEnd: '2025-01-01',
    });

    expect(hasSweepWindowErrors(errors)).toBe(true);
    expect(errors.lockedTestStart).toContain('请选择');
    expect(errors.lockedTestEnd).toContain('请选择');
    expect(errors.trainWindow).toContain('选模窗口开始前');
  });

  it('accepts non-overlapping training, selection, and locked windows', () => {
    const errors = validateStrictSweepWindows({
      trainStart: '2023-01-01',
      trainEnd: '2024-12-31',
      selectionStart: '2025-01-01',
      selectionEnd: '2025-06-30',
      lockedTestStart: '2025-07-01',
      lockedTestEnd: '2025-12-31',
    });

    expect(errors).toEqual({});
    expect(hasSweepWindowErrors(errors)).toBe(false);
  });
});

describe('selection result and promotion rules', () => {
  it('maps only selection_metrics into values rendered by the page', () => {
    const result = mapSelectionResult({
      id: 91,
      name: 'trial',
      params: { lookback: 20 },
      status: 'completed',
      selection_metrics: {
        sharpe_ratio: 1.2,
        annual_return: 0.18,
        max_drawdown: -0.09,
        win_rate: 0.57,
      },
    });

    expect(result).toEqual({
      experiment_id: 91,
      params: { lookback: 20 },
      status: 'completed',
      sharpe: 1.2,
      return: 0.18,
      max_drawdown: -0.09,
      win_rate: 0.57,
    });
  });

  it('allows only completed trials to be selected for promotion', () => {
    expect(canPromoteSweepTrial('completed')).toBe(true);
    expect(canPromoteSweepTrial('pending')).toBe(false);
    expect(canPromoteSweepTrial('running')).toBe(false);
    expect(canPromoteSweepTrial('failed')).toBe(false);
    expect(canPromoteSweepTrial('cancelled')).toBe(false);
  });

  it('ranks negative drawdowns closest to zero first and null last', () => {
    const item = (experimentId: number, maxDrawdown: number | null) => ({
      experiment_id: experimentId,
      params: {},
      status: 'completed',
      sharpe: null,
      return: null,
      max_drawdown: maxDrawdown,
      win_rate: null,
    });
    const results = [
      item(1, -0.4),
      item(2, -0.1),
      item(3, 0),
      item(4, null),
    ];

    expect(
      results
        .sort((a, b) => compareSweepResults(a, b, 'max_drawdown'))
        .map((result) => result.experiment_id),
    ).toEqual([3, 2, 1, 4]);
  });

  it('ranks Sharpe descending, moves unavailable values last, and breaks ties by id', () => {
    const item = (experimentId: number, sharpe: number | null) => ({
      experiment_id: experimentId,
      params: {},
      status: 'completed',
      sharpe,
      return: null,
      max_drawdown: null,
      win_rate: null,
    });
    const results = [
      item(9, null),
      item(7, 1.4),
      item(5, 1.4),
      item(3, -0.2),
      item(11, Number.NaN),
    ];

    expect(
      results
        .sort((a, b) => compareSweepResults(a, b, 'sharpe'))
        .map((result) => result.experiment_id),
    ).toEqual([5, 7, 3, 9, 11]);
  });
});

describe('sweep URL restoration', () => {
  it.each([
    ['42', 42],
    ['1', 1],
    [null, null],
    ['', null],
    ['0', null],
    ['-1', null],
    ['1.5', null],
    [' 4', null],
    ['9007199254740992', null],
  ])('parses query id %s as %s', (value, expected) => {
    expect(parsePositiveQueryId(value)).toBe(expected);
  });

  it('restores an existing promotion from the persisted sweep response', () => {
    expect(restoredSweepPromotion({
      sweep: {
        id: 8,
        status: 'completed',
        sweep_config: { short_window: [10, 20] },
        total_experiments: 2,
        completed_experiments: 2,
        selection_start: '2025-01-01',
        selection_end: '2025-06-30',
        locked_test_start: '2025-07-01',
        locked_test_end: '2025-12-31',
        research_trust: 'locked_test',
        data_access_policy: 'cache_only',
        promoted_experiment_id: 101,
        promotion_source_experiment_id: 82,
      },
      experiments: [],
    })).toMatchObject({
      sweep_id: 8,
      source_experiment_id: 82,
      experiment_id: 101,
      created: false,
    });
  });

  it('does not invent a promotion from incomplete persisted fields', () => {
    expect(restoredSweepPromotion({
      sweep: {
        id: 8,
        status: 'completed',
        sweep_config: {},
        total_experiments: 2,
        completed_experiments: 2,
        selection_start: null,
        selection_end: null,
        locked_test_start: null,
        locked_test_end: null,
        research_trust: 'legacy_unlocked',
        data_access_policy: 'allow_fetch',
        promoted_experiment_id: 101,
        promotion_source_experiment_id: null,
      },
      experiments: [],
    })).toBeNull();
  });
});

describe('strict sweep service contract', () => {
  it('requests a 100-item baseline picker so sweep children do not hide recent baselines', async () => {
    get.mockResolvedValueOnce({ data: { data: [] } });

    await expect(getExperimentPicker({ limit: 100 })).resolves.toEqual([]);
    expect(get).toHaveBeenCalledWith(
      '/api/experiments/picker',
      { params: { limit: 100 } },
    );
  });

  it('submits strict window fields without legacy trial test fields', async () => {
    const response: CreateSweepResponse = {
      sweep_id: 8,
      total_experiments: 2,
      experiment_ids: [81, 82],
      job_ids: ['a', 'b'],
      selection_window: { start: '2025-01-01', end: '2025-06-30' },
      locked_test_window: { start: '2025-07-01', end: '2025-12-31' },
      research_trust: 'locked_test',
      data_access_policy: 'cache_only',
    };
    post.mockResolvedValueOnce({ data: { data: response } });
    const request: SweepData = {
      strategy_id: 'ma_cross',
      param_grid: { short_window: [10, 20] },
      selection_start: '2025-01-01',
      selection_end: '2025-06-30',
      locked_test_start: '2025-07-01',
      locked_test_end: '2025-12-31',
      data_access_policy: 'cache_only',
      source_experiment_id: 42,
    };

    await expect(createSweep(request)).resolves.toEqual(response);
    expect(post).toHaveBeenCalledWith('/api/experiments/sweep', request);
    const sent = post.mock.calls[0][1] as Record<string, unknown>;
    expect(sent).not.toHaveProperty('test_start');
    expect(sent).not.toHaveProperty('test_end');
    expect(sent.selection_start).toBe('2025-01-01');
    expect(sent.locked_test_start).toBe('2025-07-01');
  });

  it('promotes the explicitly chosen member through the sweep endpoint', async () => {
    const response = {
      sweep_id: 8,
      source_experiment_id: 82,
      experiment_id: 101,
      job_id: 'locked-job',
      created: true,
      research_trust: 'locked_test' as const,
    };
    post.mockResolvedValueOnce({ data: { data: response } });

    await expect(promoteSweepExperiment(8, 82)).resolves.toEqual(response);
    expect(post).toHaveBeenCalledWith(
      '/api/experiments/sweep/8/promote',
      { experiment_id: 82 },
    );
  });
});
