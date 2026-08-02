import axios from 'axios';
import type { AxiosInstance, InternalAxiosRequestConfig, AxiosResponse, AxiosError } from 'axios';
import { apiBaseUrl } from '../config/runtime';
import { normalizeApiError } from './apiError';

export const API_BASE_URL = import.meta.env.VITE_API_URL || apiBaseUrl();
const api: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 请求拦截器：自动添加 JWT token
api.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = localStorage.getItem('auth_token');
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error: AxiosError) => {
    return Promise.reject(error);
  }
);

// 响应拦截器：401 时清除 token 跳转登录页
let refreshPromise: Promise<string> | null = null;

api.interceptors.response.use(
  (response: AxiosResponse) => {
    return response;
  },
  async (error: AxiosError) => {
    const original = error.config as (InternalAxiosRequestConfig & { _retry?: boolean }) | undefined;
    const refreshToken = localStorage.getItem('auth_refresh_token');
    const canRefresh = (
      error.response?.status === 401
      && original
      && !original._retry
      && refreshToken
      && !original.url?.includes('/api/auth/')
    );
    if (canRefresh) {
      original._retry = true;
      refreshPromise ??= axios
        .post(`${API_BASE_URL}/api/auth/refresh`, { refresh_token: refreshToken })
        .then((response) => {
          const accessToken = response.data?.data?.access_token as string | undefined;
          const nextRefreshToken = response.data?.data?.refresh_token as string | undefined;
          if (!accessToken) throw new Error('刷新 token 失败');
          localStorage.setItem('auth_token', accessToken);
          if (nextRefreshToken) localStorage.setItem('auth_refresh_token', nextRefreshToken);
          return accessToken;
        })
        .finally(() => { refreshPromise = null; });
      try {
        const accessToken = await refreshPromise;
        original.headers.Authorization = `Bearer ${accessToken}`;
        return api.request(original);
      } catch {
        // Continue to the common logout path below.
      }
    }
    if (error.response?.status === 401) {
      localStorage.removeItem('auth_token');
      localStorage.removeItem('auth_user');
      localStorage.removeItem('auth_refresh_token');
      // 避免在登录页重复跳转
      if (window.location.pathname !== '/login') {
        window.location.href = '/login';
      }
    }
    return Promise.reject(normalizeApiError(error));
  }
);

export default api;
