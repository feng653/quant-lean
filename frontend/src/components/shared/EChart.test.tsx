import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import EChart from './EChart';

describe('EChart', () => {
  it('loads the chart adapter as a renderable React component', () => {
    expect(() =>
      renderToStaticMarkup(
        <EChart option={{ xAxis: {}, yAxis: {}, series: [] }} />,
      ),
    ).not.toThrow();
  });
});
