import axios from 'axios';
import api from './api';

export type FactorEvidenceFormat = 'json' | 'csv';

export function factorEvidenceFilename(
  runId: string,
  format: FactorEvidenceFormat,
  contentDisposition?: string,
): string {
  const safeRunId = /^frun_[0-9a-f]{32}$/.test(runId) ? runId : 'factor-run';
  const extension = format === 'csv' ? '.zip' : '.json';
  const fallback = `factor-research-evidence-${safeRunId}${extension}`;
  const match = contentDisposition?.match(/filename="([^"]+)"/i);
  if (!match?.[1]) return fallback;
  const basename = match[1].split(/[\\/]/).pop() ?? '';
  const safe = basename
    .replace(/[^A-Za-z0-9._-]/g, '_')
    .replace(/^\.+/, '');
  return safe && safe.endsWith(extension) ? safe : fallback;
}

async function factorDownloadError(error: unknown): Promise<Error> {
  if (!axios.isAxiosError(error)) {
    return error instanceof Error ? error : new Error('因子研究证据导出失败');
  }
  const payload = error.response?.data;
  if (payload instanceof Blob) {
    try {
      const parsed = JSON.parse(await payload.text()) as { detail?: unknown };
      if (typeof parsed.detail === 'string' && parsed.detail.trim()) {
        return new Error(parsed.detail);
      }
    } catch {
      // Preserve the safe generic message when an intermediary returned HTML.
    }
  }
  return new Error(
    error.response?.status === 404
      ? '研究运行不存在'
      : '因子研究证据导出失败',
  );
}

export async function downloadFactorEvidence(
  runId: string,
  format: FactorEvidenceFormat,
): Promise<string> {
  try {
    const response = await api.get<Blob>(
      `/api/factor-research/runs/${encodeURIComponent(runId)}/export`,
      {
        params: { format },
        responseType: 'blob',
        timeout: 120_000,
      },
    );
    const filename = factorEvidenceFilename(
      runId,
      format,
      response.headers['content-disposition'],
    );
    const href = URL.createObjectURL(response.data);
    try {
      const anchor = document.createElement('a');
      anchor.href = href;
      anchor.download = filename;
      anchor.rel = 'noopener';
      anchor.click();
    } finally {
      URL.revokeObjectURL(href);
    }
    return filename;
  } catch (error: unknown) {
    throw await factorDownloadError(error);
  }
}
