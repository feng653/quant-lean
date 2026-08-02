import { useEffect, useRef, useState } from 'react';

export type AiScopeKey = string | number | null | undefined;

export interface AiRequestSequence {
  next: () => number;
  invalidate: () => void;
  isCurrent: (requestId: number) => boolean;
}

export function createAiRequestSequence(): AiRequestSequence {
  let currentRequestId = 0;
  return {
    next: () => {
      currentRequestId += 1;
      return currentRequestId;
    },
    invalidate: () => {
      currentRequestId += 1;
    },
    isCurrent: (requestId) => requestId === currentRequestId,
  };
}

export interface AiActionState<T> {
  result?: T;
  loading: boolean;
  error: string;
  run: (request: () => Promise<T>) => Promise<void>;
}

export function aiErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (/api[\s_-]*key|deepseek.*(?:not configured|未配置)|(?:not configured|未配置).*deepseek|密钥未配置/i.test(message)) {
    return 'AI 服务尚未配置 API Key，请联系管理员配置后重试。';
  }
  if (/503|service unavailable|temporarily unavailable|服务暂不可用/i.test(message)) {
    return 'AI 服务暂时不可用，请稍后重试。';
  }
  return message || 'AI 请求失败，请稍后重试。';
}

export function useAiAction<T>(
  initialResult?: T,
  scopeKey?: AiScopeKey
): AiActionState<T> {
  const [result, setResult] = useState<T | undefined>(initialResult);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [resolvedScope, setResolvedScope] = useState<AiScopeKey>(scopeKey);
  const initialResultRef = useRef(initialResult);
  const sequenceRef = useRef<AiRequestSequence | null>(null);
  initialResultRef.current = initialResult;
  sequenceRef.current ??= createAiRequestSequence();

  useEffect(() => {
    const sequence = sequenceRef.current;
    sequence?.invalidate();
    setResult(initialResultRef.current);
    setLoading(false);
    setError('');
    setResolvedScope(scopeKey);
  }, [scopeKey]);

  useEffect(() => () => {
    sequenceRef.current?.invalidate();
  }, []);

  const run = async (request: () => Promise<T>) => {
    const sequence = sequenceRef.current;
    if (!sequence) return;
    const requestId = sequence.next();
    setResolvedScope(scopeKey);
    setLoading(true);
    setError('');
    try {
      const nextResult = await request();
      if (sequence.isCurrent(requestId)) setResult(nextResult);
    } catch (requestError) {
      if (sequence.isCurrent(requestId)) setError(aiErrorMessage(requestError));
    } finally {
      if (sequence.isCurrent(requestId)) setLoading(false);
    }
  };

  const scopeMatches = Object.is(resolvedScope, scopeKey);
  return {
    result: scopeMatches ? result : initialResult,
    loading: scopeMatches ? loading : false,
    error: scopeMatches ? error : '',
    run,
  };
}
