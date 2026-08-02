import { describe, expect, it } from 'vitest';
import {
  capacityStatusText,
  boundedChartSeries,
  equalFactorWeights,
  parseBoundedNumberList,
} from './factorQualityForm';

describe('factor quality form helpers', () => {
  it('parses bounded unique cost and participation scenarios', () => {
    expect(parseBoundedNumberList('0, 5, 10, 20', {
      min: 0, max: 100, maxItems: 8,
    })).toEqual([0, 5, 10, 20]);
    expect(parseBoundedNumberList('0.01, 0.05, 0.1', {
      min: 0, max: 0.25, maxItems: 5, includeMin: false,
    })).toEqual([0.01, 0.05, 0.1]);
    expect(parseBoundedNumberList('0, 0', {
      min: 0, max: 100, maxItems: 8,
    })).toBeNull();
    expect(parseBoundedNumberList('0, 101', {
      min: 0, max: 100, maxItems: 8,
    })).toBeNull();
  });

  it('builds deterministic bounded equal weights', () => {
    expect(equalFactorWeights(['z', 'a', 'z'])).toEqual({ a: 0.5, z: 0.5 });
    expect(equalFactorWeights([])).toEqual({});
  });

  it('explains fail-closed capacity states', () => {
    expect(capacityStatusText('unavailable', 'amount_field_missing')).toContain('缺少成交额');
    expect(capacityStatusText('partial', 'amount_incomplete')).toContain('覆盖不完整');
    expect(capacityStatusText('available', null)).toBe('可用');
  });

  it('bounds large chart series while preserving endpoints', () => {
    const input = Array.from({ length: 2_000 }, (_, index) => index);
    const output = boundedChartSeries(input, 500);
    expect(output).toHaveLength(500);
    expect(output[0]).toBe(0);
    expect(output.at(-1)).toBe(1_999);
    expect(boundedChartSeries([1, 2], 500)).toEqual([1, 2]);
  });
});
