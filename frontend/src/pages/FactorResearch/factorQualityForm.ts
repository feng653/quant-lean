export function parseBoundedNumberList(
  value: string,
  {
    min,
    max,
    maxItems,
    includeMin = true,
  }: {
    min: number;
    max: number;
    maxItems: number;
    includeMin?: boolean;
  },
): number[] | null {
  const parts = value.split(',').map((item) => item.trim()).filter(Boolean);
  if (parts.length === 0 || parts.length > maxItems) return null;
  const numbers = parts.map(Number);
  if (
    numbers.some((item) => (
      !Number.isFinite(item)
      || (includeMin ? item < min : item <= min)
      || item > max
    ))
    || new Set(numbers).size !== numbers.length
  ) return null;
  return numbers;
}

export function equalFactorWeights(factorIds: string[]): Record<string, number> {
  const unique = [...new Set(factorIds)].sort();
  if (unique.length === 0) return {};
  return Object.fromEntries(unique.map((factorId) => [factorId, 1 / unique.length]));
}

export function capacityStatusText(
  status: 'available' | 'partial' | 'unavailable',
  reason: string | null,
): string {
  if (status === 'available') return '可用';
  if (reason === 'amount_field_missing') return '不可用：缓存缺少成交额字段';
  if (reason === 'amount_incomplete') return '部分可用：成交额覆盖不完整';
  return status === 'partial' ? '部分可用' : '不可用';
}

export function boundedChartSeries<T>(values: T[], maxPoints = 500): T[] {
  if (!Number.isInteger(maxPoints) || maxPoints < 2) return [];
  if (values.length <= maxPoints) return values;
  const result: T[] = [];
  for (let index = 0; index < maxPoints; index += 1) {
    const sourceIndex = Math.round(index * (values.length - 1) / (maxPoints - 1));
    result.push(values[sourceIndex]);
  }
  return result;
}
