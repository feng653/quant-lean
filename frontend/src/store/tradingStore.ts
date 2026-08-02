import { create } from 'zustand';
import type { Deployment, Portfolio, Position, Signal, Order } from '../types/trading';
import type { PaginatedResponse } from '../types/api';
import * as tradingApi from '../services/trading';
import type { DeploymentFilters, CreateDeploymentData, CreatePortfolioData } from '../services/trading';

interface TradingState {
  deployments: Deployment[];
  positions: Position[];
  signals: Signal[];
  orders: Order[];
  portfolios: Portfolio[];
  ordersTotal: number;
  ordersPage: number;
  loading: boolean;
  error: string | null;

  fetchDeployments: (filters?: DeploymentFilters) => Promise<void>;
  createDeployment: (data: CreateDeploymentData) => Promise<{ deployment_id: number }>;
  fetchPositions: (portfolioId?: number) => Promise<void>;
  fetchSignals: (deploymentId?: number, date?: string) => Promise<void>;
  fetchOrders: (deploymentId?: number, page?: number, limit?: number) => Promise<void>;
  fetchPortfolios: () => Promise<void>;
  createPortfolio: (data: CreatePortfolioData) => Promise<{ portfolio_id: number }>;
  triggerSimulation: (date: string) => Promise<{ job_id: string }>;
  clearError: () => void;
}

export const useTradingStore = create<TradingState>((set) => ({
  deployments: [],
  positions: [],
  signals: [],
  orders: [],
  portfolios: [],
  ordersTotal: 0,
  ordersPage: 1,
  loading: false,
  error: null,

  fetchDeployments: async (filters?: DeploymentFilters) => {
    set({ loading: true, error: null });
    try {
      const deployments = await tradingApi.listDeployments(filters);
      set({ deployments, loading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取部署列表失败';
      set({ error: message, loading: false });
    }
  },

  createDeployment: async (data: CreateDeploymentData) => {
    set({ loading: true, error: null });
    try {
      const result = await tradingApi.createDeployment(data);
      set({ loading: false });
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : '创建部署失败';
      set({ error: message, loading: false });
      throw err;
    }
  },

  fetchPositions: async (portfolioId?: number) => {
    set({ loading: true, error: null });
    try {
      const positions = await tradingApi.getPositions(portfolioId);
      set({ positions, loading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取持仓失败';
      set({ error: message, loading: false });
    }
  },

  fetchSignals: async (deploymentId?: number, date?: string) => {
    set({ loading: true, error: null });
    try {
      const signals = await tradingApi.getSignals(deploymentId, date);
      set({ signals, loading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取信号失败';
      set({ error: message, loading: false });
    }
  },

  fetchOrders: async (deploymentId?: number, page: number = 1, limit: number = 50) => {
    set({ loading: true, error: null });
    try {
      const result: PaginatedResponse<Order> = await tradingApi.getOrders(deploymentId, page, limit);
      set({
        orders: result.items,
        ordersTotal: result.total,
        ordersPage: result.page,
        loading: false,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取订单失败';
      set({ error: message, loading: false });
    }
  },

  fetchPortfolios: async () => {
    set({ loading: true, error: null });
    try {
      const portfolios = await tradingApi.listPortfolios();
      set({ portfolios, loading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取组合列表失败';
      set({ error: message, loading: false });
    }
  },

  createPortfolio: async (data: CreatePortfolioData) => {
    set({ loading: true, error: null });
    try {
      const result = await tradingApi.createPortfolio(data);
      set({ loading: false });
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : '创建组合失败';
      set({ error: message, loading: false });
      throw err;
    }
  },

  triggerSimulation: async (date: string) => {
    set({ loading: true, error: null });
    try {
      const result = await tradingApi.triggerSimulation(date);
      set({ loading: false });
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : '触发模拟失败';
      set({ error: message, loading: false });
      throw err;
    }
  },

  clearError: () => {
    set({ error: null });
  },
}));
