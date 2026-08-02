import type { ReactNode } from 'react';
import Icon from './Icon';
import type { IconName } from './Icon';

interface EmptyStateProps {
  icon: IconName;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}

export default function EmptyState({
  icon,
  title,
  description,
  action,
  className = '',
}: EmptyStateProps) {
  return (
    <div className={`flex flex-col items-center px-6 py-14 text-center ${className}`} role="status">
      <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-md border border-ink-200 bg-ink-100 text-ink-500">
        <Icon name={icon} className="h-6 w-6" />
      </div>
      <p className="font-medium text-ink-800">{title}</p>
      {description && (
        <p className="mt-1 max-w-md text-sm leading-6 text-ink-500">{description}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}
