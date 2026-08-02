import { useCallback, useEffect, useRef, useState } from 'react';
import { getIndustryCatalog } from '../../services/data';
import type { IndustryCatalogState } from '../../services/industryCatalog';

export interface IndustryCatalogController {
  catalog: IndustryCatalogState | null;
  loading: boolean;
  error: string | null;
  retry: () => void;
}

/**
 * Loads the validated industry catalog once per classification and pool, with an
 * explicit retry path. Race-safe: stale responses are discarded.
 */
export function useIndustryCatalog(
  classification?: string,
  poolId?: string,
  codes?: string[],
): IndustryCatalogController {
  const [catalog, setCatalog] = useState<IndustryCatalogState | null>(null);
  const [catalogScope, setCatalogScope] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorScope, setErrorScope] = useState<string | null>(null);
  const requestRef = useRef(0);
  const codesKey = [...new Set(codes ?? [])].sort().join(',');
  const scopeKey = `${classification ?? ''}\u0000${poolId ?? ''}\u0000${codesKey}`;

  const load = useCallback(() => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    setErrorScope(null);
    setCatalog(null);
    setCatalogScope(null);
    getIndustryCatalog(
      classification,
      poolId,
      codesKey ? codesKey.split(',') : undefined,
    )
      .then((state) => {
        if (requestRef.current !== requestId) return;
        setCatalog(state);
        setCatalogScope(scopeKey);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (requestRef.current !== requestId) return;
        setCatalog(null);
        setError(err instanceof Error ? err.message : '行业目录加载失败');
        setErrorScope(scopeKey);
        setLoading(false);
      });
  }, [classification, poolId, codesKey, scopeKey]);

  useEffect(() => {
    load();
  }, [load]);

  const isCurrentScope = catalogScope === scopeKey;
  const isCurrentError = errorScope === scopeKey;
  return {
    catalog: isCurrentScope ? catalog : null,
    loading: loading || (!isCurrentScope && !isCurrentError),
    error: isCurrentError ? error : null,
    retry: load,
  };
}
