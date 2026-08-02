import { useRef, type ReactNode, type KeyboardEvent } from 'react';

export interface TabItem {
  key: string;
  label: ReactNode;
  disabled?: boolean;
}

interface TabsProps {
  tabs: TabItem[];
  active: string;
  onChange: (key: string) => void;
  ariaLabel: string;
  className?: string;
}

/**
 * Accessible tab strip (manual activation pattern: arrow keys move focus,
 * Enter/Space activates). Styled as an underlined segmented control.
 */
export default function Tabs({ tabs, active, onChange, ariaLabel, className = '' }: TabsProps) {
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
    event.preventDefault();
    const enabled = tabs.filter((tab) => !tab.disabled);
    const currentIndex = enabled.findIndex((tab) => tab.key === active);
    const delta = event.key === 'ArrowRight' ? 1 : -1;
    const next = enabled[(currentIndex + delta + enabled.length) % enabled.length];
    if (next) {
      onChange(next.key);
      const focusIndex = tabs.findIndex((tab) => tab.key === next.key);
      tabRefs.current[focusIndex]?.focus();
    }
  };

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      onKeyDown={onKeyDown}
      className={`flex flex-wrap items-center gap-1 border-b border-ink-200 ${className}`}
    >
      {tabs.map((tab, index) => {
        const isActive = tab.key === active;
        return (
          <button
            key={tab.key}
            ref={(element) => {
              tabRefs.current[index] = element;
            }}
            type="button"
            role="tab"
            aria-selected={isActive}
            aria-controls={`tabpanel-${tab.key}`}
            id={`tab-${tab.key}`}
            tabIndex={isActive ? 0 : -1}
            disabled={tab.disabled}
            onClick={() => onChange(tab.key)}
            className={`-mb-px border-b-2 px-3.5 py-2.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              isActive
                ? 'border-accent-700 text-accent-800'
                : 'border-transparent text-ink-500 hover:border-ink-300 hover:text-ink-800'
            }`}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  tabKey,
  active,
  children,
  className = '',
}: {
  tabKey: string;
  active: string;
  children: ReactNode;
  className?: string;
}) {
  if (tabKey !== active) return null;
  return (
    <div
      role="tabpanel"
      id={`tabpanel-${tabKey}`}
      aria-labelledby={`tab-${tabKey}`}
      className={className}
    >
      {children}
    </div>
  );
}
