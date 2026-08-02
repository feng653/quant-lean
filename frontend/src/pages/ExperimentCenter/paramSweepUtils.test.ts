import { describe, expect, it } from 'vitest';
import {
  buildSweepGrid,
  countCartesianProduct,
  generateRangeValues,
  parseCustomValues,
} from './paramSweepUtils';

describe('parameter sweep utilities', () => {
  it('generates logarithmically spaced values including both endpoints', () => {
    expect(generateRangeValues(1, 1000, 4, 'log')).toEqual([1, 10, 100, 1000]);
  });

  it('parses custom numbers, strings, quoted strings, and booleans', () => {
    expect(parseCustomValues('10, growth, "value", true, FALSE, 1e-3')).toEqual([
      10,
      'growth',
      'value',
      true,
      false,
      0.001,
    ]);
  });

  it('accepts a JSON array so string values can safely contain commas', () => {
    expect(
      parseCustomValues(
        '["ma_cross_v1,rsi_reversal_v1", "ma_cross_v1,macd_signal_v1"]',
      ),
    ).toEqual([
      'ma_cross_v1,rsi_reversal_v1',
      'ma_cross_v1,macd_signal_v1',
    ]);
  });

  it.each([
    '[]',
    '[null]',
    '[{"unsafe": true}]',
    '[[1, 2]]',
    '[1,]',
  ])('rejects unsafe or malformed JSON custom values: %s', (input) => {
    expect(() => parseCustomValues(input)).toThrow();
  });

  it('counts a cartesian product across all parameter value groups', () => {
    expect(countCartesianProduct([[1, 2], ['a', 'b', 'c'], [true, false]])).toBe(12);
    expect(countCartesianProduct([])).toBe(0);
  });

  it('rounds and deduplicates integer linear and logarithmic ranges', () => {
    expect(generateRangeValues(1, 3, 10, 'linear', 'int')).toEqual([1, 2, 3]);
    expect(generateRangeValues(1, 100, 5, 'log', 'integer')).toEqual([1, 3, 10, 32, 100]);
  });

  it('enforces custom typed values for boolean and choice parameters', () => {
    const result = buildSweepGrid([
      {
        id: 1,
        name: 'enabled',
        valueType: 'bool',
        mode: 'custom',
        min: '',
        max: '',
        steps: '2',
        custom: 'true, false',
      },
      {
        id: 2,
        name: 'window_mode',
        valueType: 'choice',
        choices: ['fixed', 'rolling'],
        mode: 'custom',
        min: '',
        max: '',
        steps: '2',
        custom: 'fixed, rolling',
      },
    ]);

    expect(result.rowErrors).toEqual({});
    expect(result.grid).toEqual({
      enabled: [true, false],
      window_mode: ['fixed', 'rolling'],
    });
    expect(result.total).toBe(4);
  });

  it('rejects decimal custom values for integer parameters', () => {
    const result = buildSweepGrid([{
      id: 3,
      name: 'window',
      valueType: 'int',
      mode: 'custom',
      min: '',
      max: '',
      steps: '2',
      custom: '10, 12.5',
    }]);

    expect(result.total).toBe(0);
    expect(result.rowErrors[3]).toContain('必须为整数');
  });
});
