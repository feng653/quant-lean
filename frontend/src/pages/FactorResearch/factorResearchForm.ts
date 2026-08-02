import type {
  FactorCacheCapability,
  FactorDefinition,
  FactorResearchReadiness,
} from '../../services/factorResearch';

export function defaultResearchStart(
  dateStart: string | null,
  dateEnd: string | null,
): string {
  if (!dateEnd) return '';
  const end = new Date(`${dateEnd}T00:00:00Z`);
  end.setUTCFullYear(end.getUTCFullYear() - 2);
  const candidate = end.toISOString().slice(0, 10);
  return dateStart && candidate < dateStart ? dateStart : candidate;
}

export function parseResearchHorizons(value: string): number[] | null {
  const items = value.split(/[,，\s]+/).filter(Boolean).map(Number);
  if (
    items.length === 0
    || items.length > 12
    || new Set(items).size !== items.length
    || items.some((item) => !Number.isInteger(item) || item < 1 || item > 252)
  ) return null;
  return items.sort((left, right) => left - right);
}

function canonicalJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalJsonValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalJsonValue(item)]),
    );
  }
  return value;
}

export function researchConfigEquals(
  left: unknown,
  right: unknown,
): boolean {
  return JSON.stringify(canonicalJsonValue(left))
    === JSON.stringify(canonicalJsonValue(right));
}

export function firstReadyPool(
  readiness: FactorResearchReadiness,
): FactorCacheCapability | undefined {
  return readiness.pools.find((pool) => pool.ready);
}

export function firstAvailableFactor(
  factors: FactorDefinition[],
  pool: FactorCacheCapability | undefined,
): FactorDefinition | undefined {
  return factors.find((factor) =>
    pool?.available_factor_ids.includes(factor.factor_id)
  );
}
