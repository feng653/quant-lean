import { useLocation, useNavigate } from 'react-router';
import { useAuthStore } from '../../store/authStore';
import Icon from '../shared/Icon';
import type { IconName } from '../shared/Icon';
import StatusTag from '../shared/StatusTag';

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
  onNavigate?: () => void;
}

interface NavItem {
  label: string;
  icon: IconName;
  path: string;
  adminOnly?: boolean;
}

interface NavSection {
  label: string;
  items: NavItem[];
}

/**
 * Information architecture: research workflows first, execution second,
 * platform administration last.
 */
const NAV_SECTIONS: NavSection[] = [
  {
    label: '研究',
    items: [
      { label: '总览', icon: 'dashboard', path: '/' },
      { label: '实验中心', icon: 'experiment', path: '/experiment' },
      { label: '策略管理', icon: 'strategies', path: '/strategies' },
      { label: '数据中心', icon: 'data', path: '/data' },
      { label: '因子研究', icon: 'chart', path: '/factor-research' },
    ],
  },
  {
    label: '执行',
    items: [
      { label: '交易工作台', icon: 'trading', path: '/trading' },
      { label: '模型生命周期', icon: 'history', path: '/trading/models' },
      { label: '任务中心', icon: 'jobs', path: '/jobs' },
    ],
  },
  {
    label: '系统',
    items: [{ label: '用户管理', icon: 'admin', path: '/admin', adminOnly: true }],
  },
];

function isNavActive(pathname: string, path: string): boolean {
  if (path === '/') return pathname === '/';
  if (pathname.startsWith('/trading/models')) {
    return path === '/trading/models';
  }
  return pathname.startsWith(path);
}

export default function Sidebar({ collapsed, onToggle, onNavigate }: SidebarProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);

  const visibleSections = NAV_SECTIONS.map((section) => ({
    ...section,
    items: section.items.filter((item) => !item.adminOnly || user?.is_admin),
  })).filter((section) => section.items.length > 0);

  return (
    <aside
      aria-label="主导航"
      className={`flex h-full min-h-0 shrink-0 flex-col border-r border-ink-200 bg-surface transition-[width] duration-200 motion-reduce:transition-none ${
        collapsed ? 'w-16' : 'w-60'
      }`}
    >
      {/* Brand */}
      <div
        className={`flex h-14 items-center border-b border-ink-200 ${
          collapsed ? 'justify-center px-2' : 'justify-between px-4'
        }`}
      >
        {!collapsed && (
          <div className="min-w-0">
            <p className="truncate text-sm font-bold tracking-wide text-ink-900">量化验证平台</p>
            <p className="text-2xs text-ink-400">研究与模拟环境</p>
          </div>
        )}
        <button
          type="button"
          onClick={onToggle}
          className="rounded p-1.5 text-ink-500 transition-colors hover:bg-ink-100 hover:text-ink-800"
          title={collapsed ? '展开菜单' : '收起菜单'}
          aria-label={collapsed ? '展开菜单' : '收起菜单'}
          aria-expanded={!collapsed}
        >
          <Icon name={collapsed ? 'chevronRight' : 'chevronLeft'} className="h-5 w-5" />
        </button>
      </div>

      {/* Navigation sections */}
      <nav className="flex-1 overflow-y-auto px-2 py-3 scrollbar-thin" aria-label="平台功能">
        {visibleSections.map((section) => (
          <div key={section.label} className="mb-4">
            {!collapsed && (
              <p className="mb-1 px-3 text-2xs font-semibold uppercase tracking-wider text-ink-400">
                {section.label}
              </p>
            )}
            <div className="space-y-0.5">
              {section.items.map((item) => {
                const active = isNavActive(location.pathname, item.path);
                return (
                  <button
                    type="button"
                    key={item.path}
                    onClick={() => {
                      navigate(item.path);
                      onNavigate?.();
                    }}
                    title={collapsed ? item.label : undefined}
                    aria-label={item.label}
                    aria-current={active ? 'page' : undefined}
                    className={`group flex w-full items-center rounded py-2 text-sm transition-colors ${
                      collapsed ? 'justify-center px-2' : 'gap-3 px-3'
                    } ${
                      active
                        ? 'bg-accent-50 font-medium text-accent-800 shadow-[inset_2px_0_0_theme(colors.accent.700)]'
                        : 'text-ink-600 hover:bg-ink-100 hover:text-ink-900'
                    }`}
                  >
                    <Icon
                      name={item.icon}
                      className={`h-5 w-5 shrink-0 ${active ? 'text-accent-700' : 'text-ink-400 group-hover:text-ink-600'}`}
                    />
                    {!collapsed && <span className="truncate">{item.label}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* Environment marker */}
      <div className={`border-t border-ink-200 p-3 ${collapsed ? 'flex justify-center' : ''}`}>
        {collapsed ? (
          <span title="模拟环境：平台未通过实盘认证">
            <Icon name="flask" className="h-5 w-5 text-accent-700" />
          </span>
        ) : (
          <div className="space-y-1.5">
            <StatusTag variant="paper">模拟环境</StatusTag>
            <p className="text-2xs leading-4 text-ink-400">平台未通过实盘认证，仅限研究与模拟</p>
          </div>
        )}
      </div>
    </aside>
  );
}
