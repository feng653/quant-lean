export const PLATFORM_DISPLAY_TIME_ZONE = 'Asia/Shanghai';

/**
 * Parse API timestamps without changing their instant.
 *
 * The API now emits RFC 3339 UTC. The naive branch remains intentionally
 * supported for old servers and saved payloads: SQLite's historical
 * `YYYY-MM-DD HH:mm:ss` values have always represented UTC.
 */
export function parseBackendTimestamp(value?: string | null): Date | null {
  if (!value) return null;
  const raw = value.trim();
  if (!raw) return null;

  const withIsoSeparator = /^\d{4}-\d{2}-\d{2}$/.test(raw)
    ? `${raw}T00:00:00`
    : raw.includes('T') ? raw : raw.replace(' ', 'T');
  const hasTimeZone = /(?:z|[+-]\d{2}:?\d{2})$/i.test(withIsoSeparator);
  const normalized = hasTimeZone ? withIsoSeparator : `${withIsoSeparator}Z`;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatBackendDateTime(
  value?: string | null,
  fallback = '-',
): string {
  const parsed = parseBackendTimestamp(value);
  if (!parsed) return fallback;
  return parsed.toLocaleString('zh-CN', {
    hour12: false,
    timeZone: PLATFORM_DISPLAY_TIME_ZONE,
  });
}
