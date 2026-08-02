import { describe, expect, it } from 'vitest';
import {
  normalizeApiError,
  normalizeDeploymentSubmissionError,
} from './apiError';

function axiosFailure(data: unknown, requestId?: string) {
  return {
    isAxiosError: true,
    response: {
      status: 409,
      data,
      headers: requestId ? { 'x-request-id': requestId } : {},
    },
  };
}

describe('normalizeApiError', () => {
  it('reads a structured FastAPI detail instead of rendering [object Object]', () => {
    const error = normalizeApiError(axiosFailure({
      detail: {
        code: 'promotion_not_approved',
        message: 'Research promotion was revoked',
      },
    }, '9c1f3f307f0d42e8b0d479318b3961cd'));

    expect(error.message).toBe('Research promotion was revoked');
    expect(error.code).toBe('promotion_not_approved');
    expect(error.requestId).toBe('9c1f3f307f0d42e8b0d479318b3961cd');
    expect(error.message).not.toContain('[object Object]');
  });

  it('does not expose raw HTML or a suspected secret in an error response', () => {
    const html = normalizeApiError(axiosFailure({ detail: '<html>gateway failed</html>' }));
    const secret = normalizeApiError(axiosFailure({
      detail: { message: 'authorization: Bearer definitely-not-for-display' },
    }));

    expect(html.message).toBe('请求未能完成，请稍后重试');
    expect(secret.message).toBe('请求未能完成，请稍后重试');
  });

  it('keeps a safe legacy string detail without treating an object as text', () => {
    const error = normalizeApiError(axiosFailure({ detail: '目标模拟盘未启用' }));

    expect(error.message).toBe('目标模拟盘未启用');
  });
});

describe('normalizeDeploymentSubmissionError', () => {
  it('makes failed atomic deployment explicit and keeps the request ID', () => {
    const error = normalizeDeploymentSubmissionError(axiosFailure({
      detail: {
        code: 'legacy_experiment_deployment_forbidden',
        message: 'untrusted raw backend detail',
      },
    }, '15724f9da8a44251b5bbd9a3374ea5e3'));

    expect(error.message).toContain('部署未提交');
    expect(error.message).toContain('PIT');
    expect(error.message).toContain('部署记录和模拟盘版本均未变更');
    expect(error.message).toContain('15724f9da8a44251b5bbd9a3374ea5e3');
    expect(error.code).toBe('legacy_experiment_deployment_forbidden');
  });

  it('does not stringify a malformed object payload', () => {
    const error = normalizeDeploymentSubmissionError(axiosFailure({
      detail: { allocation_errors: ['not enough cash'] },
    }));

    expect(error.message).toContain('部署未提交');
    expect(error.message).not.toContain('[object Object]');
  });
});
