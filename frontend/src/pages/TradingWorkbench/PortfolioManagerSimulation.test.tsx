import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { SimulationCalendar } from '../../services/trading';
import { SimulationSection } from './PortfolioManagerPage';

vi.mock('../../store/authStore', () => ({
  useAuthStore: vi.fn(),
}));

const CALENDAR: SimulationCalendar = {
  pool_id: 'csi500',
  min_date: '2026-07-01',
  max_date: '2026-07-31',
  suggested_start: '2026-07-04',
  trading_days: 23,
};

function renderSimulation(
  calendar: SimulationCalendar | null,
  calendarError: string | null,
): string {
  return renderToStaticMarkup(
    <SimulationSection
      simulationStatus={null}
      simulationRuns={[]}
      calendar={calendar}
      calendarError={calendarError}
      simulationDate=""
      setSimulationDate={() => undefined}
      replayStart={calendar?.suggested_start ?? ''}
      setReplayStart={() => undefined}
      replayEnd={calendar?.max_date ?? ''}
      setReplayEnd={() => undefined}
      restartReplay={false}
      setRestartReplay={() => undefined}
      simulationBusy={false}
      canExecute
      hasStrategies
      onRun={() => undefined}
      onReplay={() => undefined}
    />,
  );
}

describe('SimulationSection calendar readiness', () => {
  it('shows the cache readiness error and disables replay', () => {
    const html = renderSimulation(
      null,
      '中证500行情缓存版本过旧或来源证据无效，请先在数据中心受控刷新',
    );
    const replayButton = html
      .match(/<button[^>]*>/g)
      ?.find((tag) => tag.includes('aria-label="提交历史模拟"'));

    expect(html).toContain('行情缓存版本过旧或来源证据无效');
    expect(replayButton).toMatch(/\sdisabled(?:=""|(?=[ >]))/);
    expect(html).not.toContain('可用数据：');
  });

  it('enables replay only when verified calendar dates are available', () => {
    const html = renderSimulation(CALENDAR, null);
    const replayButton = html
      .match(/<button[^>]*>/g)
      ?.find((tag) => tag.includes('aria-label="提交历史模拟"'));

    expect(html).toContain('可用数据：2026-07-01 ~ 2026-07-31');
    expect(replayButton).toBeDefined();
    expect(replayButton).not.toMatch(/\sdisabled(?:=""|(?=[ >]))/);
  });
});
