import type {
  StrategyCorrelationPair,
  StrategyCorrelationReport,
} from '../../services/experiments';

export const CORRELATION_CLASS_LABEL: Record<StrategyCorrelationPair['classification'], string> = {
  near_duplicate: '近似重复',
  high_positive: '高度同向',
  high_negative: '高度反向',
  negative: '负相关',
  low: '低相关',
  moderate: '中等相关',
  unavailable: '不可计算',
};

export function pairKey(leftId: number, rightId: number): string {
  return leftId < rightId ? `${leftId}:${rightId}` : `${rightId}:${leftId}`;
}

export function correlationColor(value: number | null): string {
  if (value === null) return '#d1d5db';
  if (value >= 0.8) return '#b91c1c';
  if (value >= 0.4) return '#f97316';
  if (value > -0.25) return '#f8fafc';
  if (value > -0.8) return '#38bdf8';
  return '#1d4ed8';
}

export function buildCorrelationHeatmap(report: StrategyCorrelationReport) {
  const names = new Map(
    report.experiments.map((experiment) => [
      experiment.id,
      `#${experiment.id} ${experiment.name}`,
    ]),
  );
  const labels = report.matrix.experiment_ids.map(
    (id) => names.get(id) ?? `#${id}`,
  );
  const cells = report.matrix.values.flatMap((row, y) =>
    row.map((value, x) => ({
      value: [x, y, value ?? 0, report.matrix.overlap_counts[y][x]],
      itemStyle: { color: correlationColor(value) },
      unavailable: value === null,
      leftId: report.matrix.experiment_ids[y],
      rightId: report.matrix.experiment_ids[x],
    })),
  );
  return {
    animation: false,
    grid: { left: 150, right: 35, top: 25, bottom: 105 },
    tooltip: {
      formatter: (raw: unknown) => {
        const params = raw as { data?: (typeof cells)[number] };
        const data = params.data;
        if (!data) return '';
        const [, , value, overlap] = data.value;
        const valueText = data.unavailable ? '不可计算' : Number(value).toFixed(3);
        return `${names.get(data.leftId)}<br/>${names.get(data.rightId)}<br/>相关系数：${valueText}<br/>共同观测：${overlap}`;
      },
    },
    xAxis: {
      type: 'category' as const,
      data: labels,
      axisLabel: { rotate: 35, width: 125, overflow: 'truncate' as const },
      splitArea: { show: true },
    },
    yAxis: {
      type: 'category' as const,
      data: labels,
      axisLabel: { width: 135, overflow: 'truncate' as const },
      splitArea: { show: true },
    },
    series: [{
      name: '收益相关性',
      type: 'heatmap' as const,
      data: cells,
      label: {
        show: true,
        formatter: (raw: unknown) => {
          const params = raw as { data?: (typeof cells)[number] };
          if (!params.data || params.data.unavailable) return '-';
          return Number(params.data.value[2]).toFixed(2);
        },
      },
      emphasis: { itemStyle: { borderColor: '#111827', borderWidth: 2 } },
    }],
  };
}
