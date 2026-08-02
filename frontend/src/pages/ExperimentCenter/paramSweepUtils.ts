export type SweepValue = number | string | boolean;
export type SweepValueMode = 'linear' | 'log' | 'custom';

export interface SweepParameterDraft {
  id: number;
  name: string;
  valueType: string;
  choices?: string[];
  mode: SweepValueMode;
  min: string;
  max: string;
  steps: string;
  custom: string;
}

export interface SweepGridResult {
  grid: Record<string, SweepValue[]>;
  rowErrors: Record<number, string>;
  total: number;
}

const NUMBER_PATTERN = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;

function roundGeneratedValue(value: number): number {
  return Number(value.toPrecision(12));
}

export function parseCustomValues(input: string): SweepValue[] {
  const normalized = input.trim();
  if (normalized.startsWith('[')) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(normalized);
    } catch {
      throw new Error('JSON 数组格式无效');
    }
    if (!Array.isArray(parsed) || parsed.length === 0) {
      throw new Error('JSON 自定义取值必须是非空一维数组');
    }
    if (
      parsed.some(
        (value) =>
          !['number', 'string', 'boolean'].includes(typeof value)
          || (typeof value === 'number' && !Number.isFinite(value)),
      )
    ) {
      throw new Error('JSON 数组只允许有限数字、字符串或布尔值');
    }
    return parsed as SweepValue[];
  }

  const tokens = normalized.split(/[,，\n]/).map((item) => item.trim());
  if (tokens.length === 0 || tokens.some((item) => item.length === 0)) {
    throw new Error('请用逗号分隔值，且不要留空项');
  }

  return tokens.map((token) => {
    if (/^true$/i.test(token)) return true;
    if (/^false$/i.test(token)) return false;
    if (NUMBER_PATTERN.test(token)) return Number(token);
    if (
      token.length >= 2
      && ((token.startsWith('"') && token.endsWith('"'))
        || (token.startsWith("'") && token.endsWith("'")))
    ) {
      return token.slice(1, -1);
    }
    return token;
  });
}

export function generateRangeValues(
  min: number,
  max: number,
  steps: number,
  mode: Exclude<SweepValueMode, 'custom'>,
  valueType = 'float',
): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    throw new Error('最小值和最大值必须是有效数字');
  }
  if (!Number.isInteger(steps) || steps < 2 || steps > 100) {
    throw new Error('取值数量必须是 2 到 100 的整数');
  }
  if (min >= max) {
    throw new Error('最大值必须大于最小值');
  }
  if (mode === 'log' && (min <= 0 || max <= 0)) {
    throw new Error('对数扫描的最小值和最大值必须大于 0');
  }
  const isInteger = valueType === 'int' || valueType === 'integer';
  if (isInteger && (!Number.isInteger(min) || !Number.isInteger(max))) {
    throw new Error('整数参数的最小值和最大值必须为整数');
  }

  let values: number[];
  if (mode === 'linear') {
    const interval = (max - min) / (steps - 1);
    values = Array.from(
      { length: steps },
      (_, index) => roundGeneratedValue(index === steps - 1 ? max : min + interval * index),
    );
  } else {
    const logMin = Math.log(min);
    const interval = (Math.log(max) - logMin) / (steps - 1);
    values = Array.from(
      { length: steps },
      (_, index) => roundGeneratedValue(index === steps - 1 ? max : Math.exp(logMin + interval * index)),
    );
  }

  if (!isInteger) return values;
  const integerValues = [...new Set(values.map((value) => Math.round(value)))];
  if (integerValues.length < 2) {
    throw new Error('整数范围太窄，无法生成至少 2 个不同取值');
  }
  return integerValues;
}

export function countCartesianProduct(valueGroups: readonly (readonly unknown[])[]): number {
  if (valueGroups.length === 0) return 0;
  return valueGroups.reduce((total, values) => total * values.length, 1);
}

export function buildSweepGrid(rows: SweepParameterDraft[]): SweepGridResult {
  const grid: Record<string, SweepValue[]> = {};
  const rowErrors: Record<number, string> = {};
  const seenNames = new Set<string>();

  for (const row of rows) {
    const name = row.name.trim();
    if (!name) {
      rowErrors[row.id] = '请选择参数';
      continue;
    }
    if (seenNames.has(name)) {
      rowErrors[row.id] = '参数不能重复';
      continue;
    }
    seenNames.add(name);

    try {
      const forceCustom = ['bool', 'boolean', 'choice'].includes(row.valueType);
      if (forceCustom && row.mode !== 'custom') {
        throw new Error('布尔和选项参数只能使用自定义取值');
      }
      const values = row.mode === 'custom'
        ? parseCustomValues(row.custom)
        : generateRangeValues(
          Number(row.min),
          Number(row.max),
          Number(row.steps),
          row.mode,
          row.valueType,
        );
      if (row.valueType === 'int' || row.valueType === 'integer') {
        if (values.some((value) => typeof value !== 'number' || !Number.isInteger(value))) {
          throw new Error('整数参数的所有取值必须为整数');
        }
      } else if (row.valueType === 'float' || row.valueType === 'number') {
        if (values.some((value) => typeof value !== 'number' || !Number.isFinite(value))) {
          throw new Error('数值参数的所有取值必须为数字');
        }
      } else if (row.valueType === 'bool' || row.valueType === 'boolean') {
        if (values.some((value) => typeof value !== 'boolean')) {
          throw new Error('布尔参数的取值只能是 true 或 false');
        }
      } else if (row.valueType === 'choice') {
        if (values.some((value) => typeof value !== 'string')) {
          throw new Error('选项参数的取值必须为字符串');
        }
        if (row.choices?.length && values.some((value) => !row.choices!.includes(String(value)))) {
          throw new Error(`选项必须是：${row.choices.join(', ')}`);
        }
      } else if (['str', 'string'].includes(row.valueType)) {
        if (values.some((value) => typeof value !== 'string')) {
          throw new Error('字符串参数的取值必须为字符串；数字样式请使用引号');
        }
      }
      grid[name] = values;
    } catch (error) {
      rowErrors[row.id] = error instanceof Error ? error.message : '参数配置无效';
    }
  }

  const total = Object.keys(rowErrors).length === 0
    ? countCartesianProduct(Object.values(grid))
    : 0;
  return { grid, rowErrors, total };
}

export function cartesianCombinations(
  grid: Record<string, SweepValue[]>,
): Array<Record<string, SweepValue>> {
  const entries = Object.entries(grid);
  if (entries.length === 0) return [];

  return entries.reduce<Array<Record<string, SweepValue>>>(
    (combinations, [name, values]) => combinations.flatMap(
      (combination) => values.map((value) => ({ ...combination, [name]: value })),
    ),
    [{}],
  );
}
