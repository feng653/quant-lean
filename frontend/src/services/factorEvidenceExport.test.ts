import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  downloadFactorEvidence,
  factorEvidenceFilename,
} from './factorEvidenceExport';

const { get } = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('./api', () => ({
  default: { get },
}));

afterEach(() => {
  get.mockReset();
  vi.unstubAllGlobals();
});

describe('factor evidence export service', () => {
  it('accepts only a safe server filename and extension', () => {
    const runId = `frun_${'a'.repeat(32)}`;
    expect(
      factorEvidenceFilename(
        runId,
        'json',
        'attachment; filename="../../unsafe.json"',
      ),
    ).toBe('unsafe.json');
    expect(
      factorEvidenceFilename(
        '../unsafe',
        'csv',
        'attachment; filename="wrong.exe"',
      ),
    ).toBe('factor-research-evidence-factor-run.zip');
  });

  it('downloads a CSV ZIP with encoded run id and finite timeout', async () => {
    const click = vi.fn();
    const createObjectURL = vi.fn(() => 'blob:factor-evidence');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL });
    vi.stubGlobal('document', {
      createElement: vi.fn(() => ({
        href: '',
        download: '',
        rel: '',
        click,
      })),
    });
    const evidence = new Blob(['evidence']);
    get.mockResolvedValueOnce({
      data: evidence,
      headers: {
        'content-disposition':
          'attachment; filename="factor-research-evidence-safe.zip"',
      },
    });

    await expect(
      downloadFactorEvidence('frun_/unsafe', 'csv'),
    ).resolves.toBe('factor-research-evidence-safe.zip');
    expect(get).toHaveBeenCalledWith(
      '/api/factor-research/runs/frun_%2Funsafe/export',
      {
        params: { format: 'csv' },
        responseType: 'blob',
        timeout: 120_000,
      },
    );
    expect(createObjectURL).toHaveBeenCalledWith(evidence);
    expect(click).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:factor-evidence');
  });
});
