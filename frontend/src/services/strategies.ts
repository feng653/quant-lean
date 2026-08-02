import api from './api';
import type { StrategyMetadata } from '../types/strategy';
import type { ApiResponse } from '../types/api';

export async function listStrategies(category?: string): Promise<StrategyMetadata[]> {
  const params = category ? { category } : {};
  const response = await api.get<ApiResponse<StrategyMetadata[]>>('/api/strategies', { params });
  return response.data.data ?? [];
}

export async function scanStrategies(): Promise<{
  before: number;
  after: number;
  added: number;
}> {
  const response = await api.post<ApiResponse<{
    before: number;
    after: number;
    added: number;
  }>>('/api/strategies/scan');
  return response.data.data ?? { before: 0, after: 0, added: 0 };
}

export async function getStrategy(id: string): Promise<StrategyMetadata> {
  const response = await api.get<ApiResponse<StrategyMetadata>>(`/api/strategies/${id}`);
  if (!response.data.data) {
    throw new Error('策略不存在');
  }
  return response.data.data;
}
