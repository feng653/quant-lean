// Vite 8 can expose the CommonJS subpath as a module object at runtime.
import ReactEChartsCore from 'echarts-for-react/esm/core';
import type { EChartsReactProps } from 'echarts-for-react/esm/types';
import * as echarts from 'echarts/core';
import { BarChart, HeatmapChart, LineChart, PieChart } from 'echarts/charts';
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  BarChart,
  HeatmapChart,
  LineChart,
  PieChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

export default function EChart(props: Omit<EChartsReactProps, 'echarts'>) {
  return <ReactEChartsCore echarts={echarts} {...props} />;
}
