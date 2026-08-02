import axios from 'axios';

/**
 * A deliberately small, safe subset of an API failure which may be shown to a
 * user.  Do not stringify arbitrary response bodies: FastAPI's `detail` may
 * legitimately be an object and intermediary error pages may contain HTML.
 */
export class ApiRequestError extends Error {
  readonly code?: string;
  readonly requestId?: string;
  readonly status?: number;

  constructor(
    message: string,
    options: { code?: string; requestId?: string; status?: number } = {},
  ) {
    super(message);
    this.name = 'ApiRequestError';
    this.code = options.code;
    this.requestId = options.requestId;
    this.status = options.status;
  }
}

type ErrorRecord = Record<string, unknown>;

const SAFE_CODE = /^[a-z][a-z0-9_:-]{1,80}$/;
const SAFE_REQUEST_ID = /^[A-Za-z0-9_-]{8,128}$/;
const UNSAFE_MESSAGE = /(?:<\/?[a-z][^>]*>|\b(?:traceback|stack trace)\b|\b(?:authorization|password|api[_ -]?key|secret|bearer)\s*(?:=|:))/i;

function record(value: unknown): ErrorRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as ErrorRecord
    : null;
}

function safeCode(value: unknown): string | undefined {
  return typeof value === 'string' && SAFE_CODE.test(value) ? value : undefined;
}

function safeRequestId(value: unknown): string | undefined {
  return typeof value === 'string' && SAFE_REQUEST_ID.test(value) ? value : undefined;
}

function safeMessage(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  if ([...value].some((character) => {
    const point = character.codePointAt(0) ?? 0;
    return point < 0x20 || point === 0x7f;
  })) return undefined;
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (!normalized || normalized.length > 360 || UNSAFE_MESSAGE.test(normalized)) return undefined;
  return normalized;
}

function headerValue(headers: unknown, name: string): unknown {
  const normalized = name.toLowerCase();
  if (headers && typeof (headers as { get?: unknown }).get === 'function') {
    return (headers as { get: (key: string) => unknown }).get(name);
  }
  const values = record(headers);
  if (!values) return undefined;
  return values[normalized] ?? values[name];
}

function payloadFields(payload: unknown): {
  code?: string;
  message?: string;
  requestId?: string;
} {
  const root = record(payload);
  if (!root) {
    return { message: safeMessage(payload) };
  }
  const detail = record(root.detail) ?? record(root.error);
  if (!detail) {
    return {
      code: safeCode(root.code),
      message: safeMessage(root.detail) ?? safeMessage(root.error) ?? safeMessage(root.message),
      requestId: safeRequestId(root.request_id) ?? safeRequestId(root.correlation_id),
    };
  }
  return {
    code: safeCode(detail.code) ?? safeCode(root.code),
    message: safeMessage(detail.message) ?? safeMessage(detail.detail) ?? safeMessage(root.message),
    requestId: safeRequestId(detail.request_id)
      ?? safeRequestId(detail.correlation_id)
      ?? safeRequestId(root.request_id)
      ?? safeRequestId(root.correlation_id),
  };
}

/** Normalize API failures without leaking a raw JSON object or HTML response. */
export function normalizeApiError(error: unknown, fallback = '请求未能完成，请稍后重试'): ApiRequestError {
  if (error instanceof ApiRequestError) return error;

  if (axios.isAxiosError(error)) {
    const fields = payloadFields(error.response?.data);
    const requestId = fields.requestId
      ?? safeRequestId(headerValue(error.response?.headers, 'x-request-id'))
      ?? safeRequestId(headerValue(error.response?.headers, 'x-correlation-id'));
    return new ApiRequestError(fields.message ?? fallback, {
      code: fields.code,
      requestId,
      status: error.response?.status,
    });
  }

  return new ApiRequestError(
    error instanceof Error ? safeMessage(error.message) ?? fallback : fallback,
  );
}

const DEPLOYMENT_CODE_MESSAGES: Record<string, string> = {
  approved_promotion_required: '需要绑定已批准且未撤销的研究晋级记录',
  promotion_experiment_required: '研究晋级必须绑定来源实验',
  legacy_experiment_deployment_forbidden: '该实验缺少可验证的 PIT 研究证据，不能进入模拟部署',
  promotion_binding_incomplete: '研究晋级证据不完整',
  promotion_not_approved: '研究晋级尚未获批或已撤销',
  promotion_binding_changed: '研究晋级证据与原始绑定不一致',
};

/**
 * Deployment creation is transactional.  Make that important fact explicit
 * whenever its request failed, while retaining a generated request ID for
 * support and server-log correlation.
 */
export function normalizeDeploymentSubmissionError(error: unknown): ApiRequestError {
  const normalized = normalizeApiError(error, '服务未能确认本次部署');
  const explanation = (normalized.code && DEPLOYMENT_CODE_MESSAGES[normalized.code])
    ?? normalized.message;
  const requestSuffix = normalized.requestId
    ? `（请求编号：${normalized.requestId}）`
    : '';
  return new ApiRequestError(`部署未提交：${explanation}。部署记录和模拟盘版本均未变更${requestSuffix}`, {
    code: normalized.code,
    requestId: normalized.requestId,
    status: normalized.status,
  });
}
