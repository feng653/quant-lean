import Badge from '../shared/Badge';
import type { AiResponseMeta } from '../../types/ai';

export function AiResultMeta({ result }: { result: AiResponseMeta }) {
  const totalTokens = result.usage?.total_tokens;
  const latencyMs = result.usage?.latency_ms;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
      {result.cached && <Badge variant="info" size="sm">缓存结果</Badge>}
      {result.model && <span>模型：{result.model}</span>}
      {typeof totalTokens === 'number' && <span>Token：{totalTokens}</span>}
      {typeof latencyMs === 'number' && <span>延迟：{latencyMs} ms</span>}
    </div>
  );
}

export function AiErrorNotice({ message }: { message: string }) {
  if (!message) return null;
  return (
    <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
      {message}
    </div>
  );
}
