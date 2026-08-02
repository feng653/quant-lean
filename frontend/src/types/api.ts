export interface ApiResponse<T> {
  data?: T;
  error?: string;
  detail?: string;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  limit: number;
}

export interface User {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  permissions: string[];
}
