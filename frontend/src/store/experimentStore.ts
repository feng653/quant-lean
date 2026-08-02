import { create } from 'zustand';
import type { Experiment, ExperimentMetrics, EquityPoint } from '../types/experiment';
import type { PaginatedResponse } from '../types/api';
import * as experimentsApi from '../services/experiments';
import type { ExperimentFilters, CreateExperimentData, SweepData } from '../services/experiments';

interface ExperimentState {
  experiments: Experiment[];
  currentExperiment: Experiment | null;
  currentMetrics: ExperimentMetrics | null;
  equityCurve: EquityPoint[];
  total: number;
  page: number;
  limit: number;
  loading: boolean;
  error: string | null;

  fetchExperiments: (filters?: ExperimentFilters) => Promise<void>;
  createExperiment: (data: CreateExperimentData) => Promise<{ experiment_id: number; job_id: string }>;
  fetchExperiment: (id: number) => Promise<void>;
  fetchMetrics: (id: number) => Promise<void>;
  fetchEquityCurve: (id: number, resolution?: string) => Promise<void>;
  toggleStar: (id: number, isStarred: boolean) => Promise<void>;
  setLabels: (id: number, labels: string[]) => Promise<void>;
  createSweep: (data: SweepData) => ReturnType<typeof experimentsApi.createSweep>;
  clearCurrent: () => void;
  clearError: () => void;
}

export const useExperimentStore = create<ExperimentState>((set, get) => ({
  experiments: [],
  currentExperiment: null,
  currentMetrics: null,
  equityCurve: [],
  total: 0,
  page: 1,
  limit: 20,
  loading: false,
  error: null,

  fetchExperiments: async (filters?: ExperimentFilters) => {
    set({ loading: true, error: null });
    try {
      const result: PaginatedResponse<Experiment> = await experimentsApi.listExperiments(filters);
      set({
        experiments: result.items,
        total: result.total,
        page: result.page,
        limit: result.limit,
        loading: false,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取实验列表失败';
      set({ error: message, loading: false });
    }
  },

  createExperiment: async (data: CreateExperimentData) => {
    set({ loading: true, error: null });
    try {
      const result = await experimentsApi.createExperiment(data);
      set({ loading: false });
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : '创建实验失败';
      set({ error: message, loading: false });
      throw err;
    }
  },

  fetchExperiment: async (id: number) => {
    set({ loading: true, error: null });
    try {
      const experiment = await experimentsApi.getExperiment(id);
      set({ currentExperiment: experiment, loading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取实验详情失败';
      set({ error: message, loading: false });
    }
  },

  fetchMetrics: async (id: number) => {
    try {
      const metrics = await experimentsApi.getExperimentMetrics(id);
      set({ currentMetrics: metrics });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取指标失败';
      set({ error: message });
    }
  },

  fetchEquityCurve: async (id: number, resolution: string = 'daily') => {
    try {
      const curve = await experimentsApi.getEquityCurve(id, resolution);
      set({ equityCurve: curve });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取权益曲线失败';
      set({ error: message });
    }
  },

  toggleStar: async (id: number, isStarred: boolean) => {
    try {
      await experimentsApi.toggleStar(id, isStarred);
      const experiments = get().experiments.map((exp) =>
        exp.id === id ? { ...exp, is_starred: isStarred } : exp
      );
      set({ experiments });
      if (get().currentExperiment?.id === id) {
        set({ currentExperiment: { ...get().currentExperiment!, is_starred: isStarred } });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : '操作失败';
      set({ error: message });
    }
  },

  setLabels: async (id: number, labels: string[]) => {
    try {
      await experimentsApi.setLabels(id, labels);
      const experiments = get().experiments.map((exp) =>
        exp.id === id ? { ...exp, labels } : exp
      );
      set({ experiments });
      if (get().currentExperiment?.id === id) {
        set({ currentExperiment: { ...get().currentExperiment!, labels } });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : '设置标签失败';
      set({ error: message });
    }
  },

  createSweep: async (data: SweepData) => {
    set({ loading: true, error: null });
    try {
      const result = await experimentsApi.createSweep(data);
      set({ loading: false });
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : '创建参数扫描失败';
      set({ error: message, loading: false });
      throw err;
    }
  },

  clearCurrent: () => {
    set({ currentExperiment: null, currentMetrics: null, equityCurve: [] });
  },

  clearError: () => {
    set({ error: null });
  },
}));
