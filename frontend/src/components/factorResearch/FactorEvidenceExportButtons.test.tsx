import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import FactorEvidenceExportButtons from './FactorEvidenceExportButtons';

vi.mock('../../services/factorEvidenceExport', () => ({
  downloadFactorEvidence: vi.fn(),
}));

describe('FactorEvidenceExportButtons', () => {
  it('offers both evidence formats without an initial false status', () => {
    const html = renderToStaticMarkup(
      <FactorEvidenceExportButtons runId={`frun_${'a'.repeat(32)}`} />,
    );

    expect(html).toContain('导出 JSON');
    expect(html).toContain('导出 CSV ZIP');
    expect(html).not.toContain('role="alert"');
    expect(html).not.toContain('role="status"');
  });
});
