import type { ReactNode } from 'react';

interface BadgeProps {
  children: ReactNode;
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' | 'accent';
  size?: 'sm' | 'md';
}

/**
 * Compact text label for categorical metadata (e.g. strategy category).
 * For evidence/safety states use StatusTag instead — it adds an icon so
 * meaning never depends on color alone.
 */
const variantClasses: Record<string, string> = {
  default: 'bg-ink-100 text-ink-600 border-ink-200',
  success: 'bg-ok-bg text-ok-fg border-ok-border',
  warning: 'bg-warn-bg text-warn-fg border-warn-border',
  danger: 'bg-danger-bg text-danger-fg border-danger-border',
  info: 'bg-info-bg text-info-fg border-info-border',
  accent: 'bg-accent-50 text-accent-800 border-accent-200',
};

const sizeClasses: Record<string, string> = {
  sm: 'px-1.5 py-px text-2xs',
  md: 'px-2 py-0.5 text-xs',
};

export default function Badge({ children, variant = 'default', size = 'md' }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center rounded-sm border font-medium leading-4 ${variantClasses[variant]} ${sizeClasses[size]}`}
    >
      {children}
    </span>
  );
}
