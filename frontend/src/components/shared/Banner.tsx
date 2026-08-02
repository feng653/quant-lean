import type { ReactNode } from 'react';
import Icon from './Icon';
import type { IconName } from './Icon';

type BannerVariant = 'info' | 'warning' | 'danger' | 'ok' | 'neutral';

interface BannerProps {
  variant?: BannerVariant;
  title?: string;
  children: ReactNode;
  className?: string;
  action?: ReactNode;
  icon?: IconName;
}

const SPECS: Record<BannerVariant, { icon: IconName; classes: string; role: 'alert' | 'status' }> = {
  info: { icon: 'info', classes: 'border-info-border bg-info-bg text-info-strong', role: 'status' },
  warning: { icon: 'warning', classes: 'border-warn-border bg-warn-bg text-warn-strong', role: 'alert' },
  danger: { icon: 'xCircle', classes: 'border-danger-border bg-danger-bg text-danger-strong', role: 'alert' },
  ok: { icon: 'checkCircle', classes: 'border-ok-border bg-ok-bg text-ok-strong', role: 'status' },
  neutral: { icon: 'info', classes: 'border-ink-300 bg-ink-100 text-ink-700', role: 'status' },
};

/**
 * Inline alert for warnings, errors and safety statements. Warning/danger
 * variants use role="alert" so assistive tech announces them; content is
 * never truncated and never hidden to improve appearance.
 */
export default function Banner({
  variant = 'info',
  title,
  children,
  className = '',
  action,
  icon,
}: BannerProps) {
  const spec = SPECS[variant];
  return (
    <div role={spec.role} className={`rounded border px-3.5 py-3 ${spec.classes} ${className}`}>
      <div className="flex items-start gap-2.5">
        <Icon name={icon ?? spec.icon} className="mt-0.5 h-5 w-5 shrink-0" aria-hidden />
        <div className="min-w-0 flex-1 text-sm leading-6">
          {title && <p className="font-semibold">{title}</p>}
          <div className={title ? 'mt-0.5' : ''}>{children}</div>
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
    </div>
  );
}
