import api from './api';
import type { ApiResponse } from '../types/api';
import type { Job, JobList, JobObservability, JobStatus, JobSummary } from '../types/job';

export interface JobFilters {
  status?: JobStatus | '';
  job_type?: string;
  page?: number;
  page_size?: number;
  mine?: boolean;
}

export async function listJobs(filters: JobFilters = {}): Promise<JobList> {
  const response = await api.get<ApiResponse<JobList>>('/api/jobs/', { params: filters });
  return response.data.data ?? { items: [], total: 0, page: 1, page_size: 20 };
}

export async function getJobSummary(): Promise<JobSummary> {
  const response = await api.get<ApiResponse<JobSummary>>('/api/jobs/summary');
  if (!response.data.data) throw new Error('无法获取任务状态');
  return response.data.data;
}

export async function getJobObservability(windowHours = 24): Promise<JobObservability> {
  const response = await api.get<ApiResponse<JobObservability>>('/api/jobs/observability', {
    params: { window_hours: windowHours },
  });
  if (!response.data.data) throw new Error('无法获取服务可观测性');
  return response.data.data;
}

export async function getJob(jobId: string): Promise<Job> {
  const response = await api.get<ApiResponse<Job>>(`/api/jobs/${jobId}`);
  if (!response.data.data) throw new Error('任务不存在');
  return response.data.data;
}

export async function cancelJob(jobId: string): Promise<JobStatus> {
  const response = await api.delete<ApiResponse<{ status: JobStatus }>>(`/api/jobs/${jobId}`);
  if (!response.data.data) throw new Error('取消任务失败');
  return response.data.data.status;
}

export async function retryJob(jobId: string): Promise<string> {
  const response = await api.post<ApiResponse<{ job_id: string }>>(`/api/jobs/${jobId}/retry`);
  if (!response.data.data) throw new Error('重试任务失败');
  return response.data.data.job_id;
}
