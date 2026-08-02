import { useState } from 'react';
import type { FactorEvidenceFormat } from '../../services/factorEvidenceExport';
import { downloadFactorEvidence } from '../../services/factorEvidenceExport';
import Button from '../shared/Button';

interface FactorEvidenceExportButtonsProps {
  runId: string;
}

export default function FactorEvidenceExportButtons({
  runId,
}: FactorEvidenceExportButtonsProps) {
  const [loading, setLoading] = useState<FactorEvidenceFormat | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const download = async (format: FactorEvidenceFormat) => {
    setLoading(format);
    setError(null);
    setNotice(null);
    try {
      const filename = await downloadFactorEvidence(runId, format);
      setNotice(`已下载 ${filename}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '因子研究证据导出失败');
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        size="sm"
        variant="ghost"
        loading={loading === 'json'}
        disabled={loading !== null}
        onClick={() => void download('json')}
      >
        导出 JSON
      </Button>
      <Button
        size="sm"
        variant="ghost"
        loading={loading === 'csv'}
        disabled={loading !== null}
        onClick={() => void download('csv')}
      >
        导出 CSV ZIP
      </Button>
      {error && (
        <span role="alert" className="text-xs text-danger-fg">
          {error}
        </span>
      )}
      {notice && (
        <span role="status" className="text-xs text-success-fg">
          {notice}
        </span>
      )}
    </div>
  );
}
