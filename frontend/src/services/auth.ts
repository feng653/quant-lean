import api from './api';
import type { ApiResponse, User } from '../types/api';

interface AuthResponse {
  token: string;
  refresh_token: string;
  user: User;
}

interface BackendAuthResponse {
  user_id: number;
  username: string;
  display_name?: string | null;
  email?: string | null;
  is_admin: boolean;
  access_token: string;
  refresh_token: string;
}

// Backend returns flat: {user_id, username, display_name, email, is_admin, access_token, refresh_token}
function adaptLoginResponse(raw: BackendAuthResponse): AuthResponse {
  // Store refresh_token for later use by refreshToken()
  if (raw.refresh_token) {
    localStorage.setItem('auth_refresh_token', raw.refresh_token);
  }
  return {
    token: raw.access_token,
    refresh_token: raw.refresh_token,
    user: {
      id: raw.user_id,
      username: raw.username,
      display_name: raw.display_name || raw.username,
      is_admin: raw.is_admin ?? false,
      permissions: [],
    },
  };
}

export async function login(username: string, password: string): Promise<AuthResponse> {
  const response = await api.post<ApiResponse<BackendAuthResponse>>('/api/auth/login', {
    username,
    password,
  });
  if (!response.data.data) {
    throw new Error('登录失败');
  }
  return adaptLoginResponse(response.data.data);
}

export async function register(
  username: string,
  password: string,
  displayName: string
): Promise<AuthResponse> {
  const response = await api.post<ApiResponse<BackendAuthResponse>>('/api/auth/register', {
    username,
    password,
    display_name: displayName,
  });
  if (!response.data.data) {
    throw new Error('注册失败');
  }
  return adaptLoginResponse(response.data.data);
}

export async function getMe(): Promise<User> {
  const response = await api.get<ApiResponse<User>>('/api/auth/me');
  if (!response.data.data) {
    throw new Error('获取用户信息失败');
  }
  return response.data.data;
}

export async function refreshToken(): Promise<{ access_token: string; refresh_token: string }> {
  const storedRefreshToken = localStorage.getItem('auth_refresh_token') || '';
  const response = await api.post<ApiResponse<{ access_token: string; refresh_token: string }>>('/api/auth/refresh', {
    refresh_token: storedRefreshToken,
  });
  if (!response.data.data) {
    throw new Error('刷新 token 失败');
  }
  return response.data.data;
}
