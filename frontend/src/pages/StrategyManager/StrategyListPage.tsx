import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { listStrategies, scanStrategies } from '../../services/strategies';
import type { StrategyMetadata } from '../../types/strategy';
import { strategyCategoryLabel, trainingModeLabel, strategyTrainingMode } from '../../utils/strategy';
import Badge from '../../components/shared/Badge';
import Banner from '../../components/shared/Banner';
import Button from '../../components/shared/Button';
import EmptyState from '../../components/shared/EmptyState';
import Icon from '../../components/shared/Icon';
import Modal from '../../components/shared/Modal';
import PageHeader from '../../components/shared/PageHeader';
import Skeleton from '../../components/shared/Skeleton';
import Tabs from '../../components/shared/Tabs';

const CATEGORY_TABS = [
  { key: '', label: '全部' },
  { key: 'technical', label: '技术指标' },
  { key: 'ml', label: '机器学习' },
  { key: 'factor', label: '因子' },
  { key: 'composite', label: '组合策略' },
];

interface ScanResult {
  before: number;
  after: number;
  added: number;
}

export default function StrategyListPage() {
  const [strategies, setStrategies] = useState<StrategyMetadata[]>([]);
  const [category, setCategory] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const navigate = useNavigate();

  const load = useCallback(async (categoryFilter: string) => {
    setLoading(true);
    setError(null);
    try {
      const result = await listStrategies(categoryFilter || undefined);
      setStrategies(result);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '获取策略列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(category);
  }, [category, load]);

  const handleScan = async () => {
    setScanning(true);
    setError(null);
    try {
      const result = await scanStrategies();
      setScanResult(result);
      await load(category);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '';
      if (message.includes('403') || message.includes('权限')) {
        setError('需要管理员权限才能扫描策略');
      } else {
        setError(message || '扫描策略失败');
      }
    } finally {
      setScanning(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="策略管理"
        description="浏览已注册策略的元数据、参数契约与训练模式。"
        breadcrumb={[{ label: '研究' }, { label: '策略管理' }]}
        actions={
          <Button variant="secondary" onClick={() => void handleScan()} loading={scanning}>
            <Icon name="refresh" className="h-4 w-4" />
            扫描策略
          </Button>
        }
      />

      {error && (
        <Banner
          variant="danger"
          className="mb-4"
          action={
            <Button variant="secondary" size="sm" onClick={() => void load(category)}>
              重试
            </Button>
          }
        >
          {error}
        </Banner>
      )}

      <Tabs
        tabs={CATEGORY_TABS}
        active={category}
        onChange={setCategory}
        ariaLabel="策略分类筛选"
        className="mb-4"
      />

      {loading ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, index) => (
            <Skeleton key={index} className="h-44 w-full" />
          ))}
        </div>
      ) : strategies.length === 0 ? (
        <div className="rounded-md border border-ink-200 bg-surface">
          <EmptyState
            icon="strategies"
            title="暂无策略"
            description="扫描策略目录后，可在这里查看和管理已注册策略。"
            action={
              <Button variant="secondary" size="sm" onClick={() => void handleScan()} loading={scanning}>
                扫描策略
              </Button>
            }
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {strategies.map((strategy) => {
            const trainingMode = strategyTrainingMode(strategy);
            return (
              <button
                type="button"
                key={strategy.strategy_id}
                onClick={() => navigate(`/strategies/${strategy.strategy_id}`)}
                className="flex flex-col rounded-md border border-ink-200 bg-surface p-4 text-left transition-colors hover:border-accent-400 hover:bg-accent-50/40"
              >
                <div className="flex items-start justify-between gap-2">
                  <h2 className="text-base font-semibold leading-6 text-ink-900">
                    {strategy.display_name}
                  </h2>
                  <Badge variant="accent" size="sm">
                    {strategyCategoryLabel(strategy.category)}
                  </Badge>
                </div>
                <p className="mt-1.5 line-clamp-2 min-h-[2.5rem] text-sm leading-5 text-ink-500">
                  {strategy.description}
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-1.5">
                  {trainingMode !== 'none' && (
                    <Badge variant={trainingMode === 'periodic' ? 'warning' : 'info'} size="sm">
                      {trainingModeLabel(strategy)}
                    </Badge>
                  )}
                  {strategy.supported_modes.map((mode) => (
                    <Badge key={mode} variant="default" size="sm">
                      {mode}
                    </Badge>
                  ))}
                  <span className="ml-auto font-mono text-2xs text-ink-400">v{strategy.version}</span>
                </div>
                {strategy.tags.length > 0 && (
                  <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    {strategy.tags.slice(0, 4).map((tag) => (
                      <span key={tag} className="rounded-sm bg-ink-100 px-1.5 py-px text-2xs text-ink-500">
                        {tag}
                      </span>
                    ))}
                    {strategy.tags.length > 4 && (
                      <span className="text-2xs text-ink-400">+{strategy.tags.length - 4}</span>
                    )}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}

      <Modal
        isOpen={scanResult !== null}
        onClose={() => setScanResult(null)}
        title="扫描结果"
        footer={
          <Button variant="secondary" onClick={() => setScanResult(null)}>
            关闭
          </Button>
        }
      >
        {scanResult && (
          <div>
            <div className="grid grid-cols-3 gap-3 text-center">
              <div className="rounded border border-ink-200 p-3">
                <p className="tnum text-2xl font-semibold">{scanResult.before}</p>
                <p className="mt-1 text-xs text-ink-500">扫描前</p>
              </div>
              <div className="rounded border border-ok-border bg-ok-bg p-3">
                <p className="tnum text-2xl font-semibold text-ok-strong">+{scanResult.added}</p>
                <p className="mt-1 text-xs text-ok-fg">新发现</p>
              </div>
              <div className="rounded border border-ink-200 p-3">
                <p className="tnum text-2xl font-semibold">{scanResult.after}</p>
                <p className="mt-1 text-xs text-ink-500">扫描后</p>
              </div>
            </div>
            {scanResult.added === 0 && (
              <p className="mt-3 text-center text-sm text-ink-500">没有新策略</p>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
