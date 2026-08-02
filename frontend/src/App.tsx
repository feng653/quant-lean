import { BrowserRouter, Routes, Route, Navigate } from 'react-router';
import { lazy, Suspense, useEffect } from 'react';
import AppShell from './components/layout/AppShell';
import Banner from './components/shared/Banner';
import Spinner from './components/shared/Spinner';
import { useAuthStore } from './store/authStore';

const DashboardPage = lazy(() => import('./pages/Dashboard/DashboardPage'));
const LoginPage = lazy(() => import('./pages/Auth/LoginPage'));
const RegisterPage = lazy(() => import('./pages/Auth/RegisterPage'));
const ExperimentListPage = lazy(() => import('./pages/ExperimentCenter/ExperimentListPage'));
const ExperimentNewPage = lazy(() => import('./pages/ExperimentCenter/ExperimentNewPage'));
const ExperimentDetailPage = lazy(() => import('./pages/ExperimentCenter/ExperimentDetailPage'));
const ParamSweepPage = lazy(() => import('./pages/ExperimentCenter/ParamSweepPage'));
const ParameterPresetPage = lazy(() => import('./pages/ExperimentCenter/ParameterPresetPage'));
const ComparePage = lazy(() => import('./pages/ExperimentCenter/ComparePage'));
const StrategyCorrelationPage = lazy(() => import('./pages/ExperimentCenter/StrategyCorrelationPage'));
const PortfolioManagerPage = lazy(() => import('./pages/TradingWorkbench/PortfolioManagerPage'));
const BrokerReadinessPage = lazy(() => import('./pages/TradingWorkbench/BrokerReadinessPage'));
const PositionMonitorPage = lazy(() => import('./pages/TradingWorkbench/PositionMonitorPage'));
const SignalPanelPage = lazy(() => import('./pages/TradingWorkbench/SignalPanelPage'));
const OrderHistoryPage = lazy(() => import('./pages/TradingWorkbench/OrderHistoryPage'));
const ModelLifecyclePage = lazy(() => import('./pages/TradingWorkbench/ModelLifecyclePage'));
const DataCenterPage = lazy(() => import('./pages/DataCenter/DataCenterPage'));
const StrategyListPage = lazy(() => import('./pages/StrategyManager/StrategyListPage'));
const StrategyDetailPage = lazy(() => import('./pages/StrategyManager/StrategyDetailPage'));
const AdminPage = lazy(() => import('./pages/Admin/AdminPage'));
const JobCenterPage = lazy(() => import('./pages/JobCenter/JobCenterPage'));
const FactorResearchPage = lazy(() => import('./pages/FactorResearch/FactorResearchPage'));

function FullPageLoading() {
  return (
    <div className="flex h-screen items-center justify-center bg-paper">
      <Spinner size="lg" label="页面加载中" />
    </div>
  );
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const isLoading = useAuthStore((s) => s.isLoading);

  if (isLoading) {
    return <FullPageLoading />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <AppShell>{children}</AppShell>;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  if (!user?.is_admin) {
    return <Navigate to="/" replace />;
  }

  return <AppShell>{children}</AppShell>;
}

function PermissionRoute({
  permission,
  children,
}: {
  permission: string;
  children: React.ReactNode;
}) {
  const user = useAuthStore((s) => s.user);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const isLoading = useAuthStore((s) => s.isLoading);

  if (isLoading) {
    return <FullPageLoading />;
  }
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  if (!user?.is_admin && !user?.permissions.includes(permission)) {
    return (
      <AppShell>
        <div className="mx-auto max-w-xl">
          <Banner variant="warning" title="没有操作权限" icon="lock">
            此功能需要权限：<span className="font-mono">{permission}</span>。
            如需开通，请联系平台管理员。
          </Banner>
        </div>
      </AppShell>
    );
  }
  return <AppShell>{children}</AppShell>;
}

export default function App() {
  const checkAuth = useAuthStore((s) => s.checkAuth);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  return (
    <BrowserRouter>
      <Suspense fallback={<FullPageLoading />}>
        <Routes>
        {/* Public routes */}
        <Route path="/login" element={
          isAuthenticated ? <Navigate to="/" replace /> : <LoginPage />
        } />
        <Route path="/register" element={
          isAuthenticated ? <Navigate to="/" replace /> : <RegisterPage />
        } />

        {/* Protected routes */}
        <Route path="/" element={
          <ProtectedRoute><DashboardPage /></ProtectedRoute>
        } />
        <Route path="/experiment" element={
          <ProtectedRoute><ExperimentListPage /></ProtectedRoute>
        } />
        <Route path="/experiment/new" element={
          <PermissionRoute permission="experiments:create"><ExperimentNewPage /></PermissionRoute>
        } />
        <Route path="/experiment/:id" element={
          <ProtectedRoute><ExperimentDetailPage /></ProtectedRoute>
        } />
        <Route path="/experiment/sweep" element={
          <PermissionRoute permission="experiments:sweep"><ParamSweepPage /></PermissionRoute>
        } />
        <Route path="/experiment/parameters" element={
          <ProtectedRoute><ParameterPresetPage /></ProtectedRoute>
        } />
        <Route path="/experiment/compare" element={
          <ProtectedRoute><ComparePage /></ProtectedRoute>
        } />
        <Route path="/experiment/correlation" element={
          <ProtectedRoute><StrategyCorrelationPage /></ProtectedRoute>
        } />
        <Route path="/trading" element={
          <ProtectedRoute><PortfolioManagerPage /></ProtectedRoute>
        } />
        <Route path="/trading/portfolio" element={
          <ProtectedRoute><PortfolioManagerPage /></ProtectedRoute>
        } />
        <Route path="/trading/portfolio/:portfolioId/:section" element={
          <ProtectedRoute><PortfolioManagerPage /></ProtectedRoute>
        } />
        <Route path="/trading/brokers" element={
          <ProtectedRoute><BrokerReadinessPage /></ProtectedRoute>
        } />
        <Route path="/trading/positions" element={
          <ProtectedRoute><PositionMonitorPage /></ProtectedRoute>
        } />
        <Route path="/trading/signals" element={
          <ProtectedRoute><SignalPanelPage /></ProtectedRoute>
        } />
        <Route path="/trading/orders" element={
          <ProtectedRoute><OrderHistoryPage /></ProtectedRoute>
        } />
        <Route path="/trading/models" element={
          <ProtectedRoute><ModelLifecyclePage /></ProtectedRoute>
        } />
        <Route path="/data" element={
          <ProtectedRoute><DataCenterPage /></ProtectedRoute>
        } />
        <Route path="/factor-research" element={
          <ProtectedRoute><FactorResearchPage /></ProtectedRoute>
        } />
        <Route path="/strategies" element={
          <ProtectedRoute><StrategyListPage /></ProtectedRoute>
        } />
        <Route path="/strategies/:id" element={
          <ProtectedRoute><StrategyDetailPage /></ProtectedRoute>
        } />
        <Route path="/jobs" element={
          <ProtectedRoute><JobCenterPage /></ProtectedRoute>
        } />

        {/* Admin route */}
        <Route path="/admin" element={
          <AdminRoute><AdminPage /></AdminRoute>
        } />

        {/* Fallback */}
        <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
