import api from './api';
import type { ApiResponse } from '../types/api';

export interface AdminUser {
  id: number;
  username: string;
  display_name: string | null;
  email: string | null;
  is_admin: boolean;
  is_active: boolean;
  role: string;
  permission_count: number;
  permissions: string[];
  created_at: string;
  last_login: string | null;
}

export interface PermissionDefinition {
  key: string;
  name: string;
  group: string;
}

export async function listAdminUsers(): Promise<AdminUser[]> {
  const response = await api.get<ApiResponse<AdminUser[]>>('/api/admin/users');
  return response.data.data ?? [];
}

export async function listPermissions(): Promise<PermissionDefinition[]> {
  const response = await api.get<ApiResponse<PermissionDefinition[]>>('/api/admin/permissions');
  return response.data.data ?? [];
}

export async function createAdminUser(data: {
  username: string;
  password: string;
  display_name?: string;
  email?: string;
  is_admin?: boolean;
}): Promise<void> {
  await api.post('/api/admin/users', data);
}

export async function updateAdminUserPermissions(
  userId: number,
  permissions: string[],
): Promise<void> {
  await api.put(`/api/admin/users/${userId}/permissions`, { permissions });
}

export async function updateAdminUserStatus(
  userId: number,
  isActive: boolean,
): Promise<void> {
  await api.put(`/api/admin/users/${userId}/status`, { is_active: isActive });
}

export async function deactivateAdminUser(userId: number): Promise<void> {
  await api.delete(`/api/admin/users/${userId}`);
}
