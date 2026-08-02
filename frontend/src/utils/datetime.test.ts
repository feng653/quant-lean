import { describe, expect, it } from 'vitest';
import {
  formatBackendDateTime,
  parseBackendTimestamp,
  PLATFORM_DISPLAY_TIME_ZONE,
} from './datetime';

describe('backend timestamp presentation', () => {
  it('treats legacy SQLite timestamps as UTC', () => {
    expect(parseBackendTimestamp('2026-07-30 00:00:00')?.toISOString())
      .toBe('2026-07-30T00:00:00.000Z');
    expect(parseBackendTimestamp('2026-07-30')?.toISOString())
      .toBe('2026-07-30T00:00:00.000Z');
  });

  it('preserves the instant carried by an explicit offset', () => {
    expect(parseBackendTimestamp('2026-07-30T08:00:00+08:00')?.toISOString())
      .toBe('2026-07-30T00:00:00.000Z');
  });

  it('formats API UTC timestamps in the platform display timezone', () => {
    expect(PLATFORM_DISPLAY_TIME_ZONE).toBe('Asia/Shanghai');
    expect(formatBackendDateTime('2026-07-30T00:00:00Z'))
      .toBe('2026/7/30 08:00:00');
  });

  it('uses a stable fallback for absent or invalid values', () => {
    expect(formatBackendDateTime(null)).toBe('-');
    expect(formatBackendDateTime('not-a-time')).toBe('-');
  });
});
