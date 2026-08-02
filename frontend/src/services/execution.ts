import api from './api';
import type { ApiResponse } from '../types/api';

export interface ExecutionAdapterReadiness {
  adapter_id: string;
  display_name: string;
  capabilities: {
    supported_order_types: Array<'market' | 'limit'>;
    supports_account_query: boolean;
    supports_position_query: boolean;
    supports_order_validation: boolean;
    supports_order_cancel: boolean;
    live_order_submission: boolean;
  };
  health: {
    status: 'unavailable' | 'configured' | 'healthy' | 'degraded';
    ready: boolean;
    message: string;
    checked_at: string;
    details: {
      sdk_module?: string;
      sdk_available?: boolean;
      missing_config?: string[];
      live_order_submission_enabled?: boolean;
    };
  };
}

export type LiveCapabilityStatus = 'available' | 'partial' | 'missing' | 'locked';

export interface LiveCapability {
  capability_id: string;
  label: string;
  status: LiveCapabilityStatus;
  required: boolean;
  evidence: string;
  source: string;
  limitation?: string | null;
}

export interface LiveReadinessDomain {
  domain_id: string;
  title: string;
  status: 'available' | 'partial' | 'blocked';
  capabilities: LiveCapability[];
}

export interface LiveReadinessBlocker {
  blocker_id: string;
  domain_id: string;
  capability_id: string;
  title: string;
  evidence: string;
  remediation: string;
}

export interface LiveAdapterEvidence {
  adapter_id: string;
  display_name: string;
  recognized_scaffold: boolean;
  certified: boolean;
  health_status: string;
  health_ready: boolean;
  health_message: string;
  sdk_module?: string | null;
  sdk_available: boolean;
  missing_config: string[];
  declared_capabilities: Partial<ExecutionAdapterReadiness['capabilities']>;
  fail_closed: boolean;
  blockers: string[];
}

export interface LiveReadinessReport {
  schema_version: string;
  capability_version: string;
  ready: false;
  certification: 'not_certified';
  platform_scope: 'research_and_paper_trading_only';
  summary: string;
  blocker_count: number;
  domains: LiveReadinessDomain[];
  blockers: LiveReadinessBlocker[];
  adapters: LiveAdapterEvidence[];
  limitations: string[];
}

export interface OrderValidation {
  adapter_id: string;
  valid: boolean;
  capability_supported: boolean;
  adapter_ready: boolean;
  submission_enabled: boolean;
  can_submit: boolean;
  errors: string[];
  warnings: string[];
  health: ExecutionAdapterReadiness['health'];
}

export interface ValidateOrderRequest {
  adapter_id: string;
  order: {
    symbol: string;
    side: 'buy' | 'sell';
    order_type: 'market' | 'limit';
    quantity: number;
    limit_price?: number;
    account_id?: string;
    client_order_id?: string;
  };
}

export async function getExecutionReadiness(): Promise<ExecutionAdapterReadiness[]> {
  const response = await api.get<ApiResponse<{ adapters: ExecutionAdapterReadiness[] }>>(
    '/api/execution/adapters/readiness',
  );
  return response.data.data?.adapters ?? [];
}

export async function getLiveReadiness(): Promise<LiveReadinessReport> {
  const response = await api.get<ApiResponse<LiveReadinessReport>>(
    '/api/execution/live-readiness',
  );
  if (!response.data.data) throw new Error('读取实盘安全门禁失败');
  return response.data.data;
}

export async function validateExecutionOrder(
  request: ValidateOrderRequest,
): Promise<OrderValidation> {
  const response = await api.post<ApiResponse<OrderValidation>>(
    '/api/execution/orders/validate',
    request,
  );
  if (!response.data.data) throw new Error('订单预检失败');
  return response.data.data;
}
