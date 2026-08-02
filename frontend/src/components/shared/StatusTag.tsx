import type { ReactNode } from 'react';
import Icon from './Icon';
import type { IconName } from './Icon';

/**
 * StatusTag — the platform's canonical evidence/status indicator.
 *
 * Every state is communicated with an icon AND a text label; color is never
 * the sole carrier of meaning. Rectangular geometry (no pills) keeps the
 * research-console tone and avoids "decorative dashboard" semantics.
 */
export type StatusVariant =
  | 'verified'   // evidence verified / gate passed
  | 'ok'         // ordinary positive state (always labelled)
  | 'unverified' // legacy / untrusted / not yet validated evidence
  | 'legacy'     // historical-only, non-promotable
  | 'warning'    // attention required, non-blocking
  | 'error'      // failed
  | 'blocked'    // fail-closed block (gate denied)
  | 'running'    // in progress
  | 'queued'     // pending
  | 'info'       // neutral information
  | 'paper'      // paper trading / simulation environment
  | 'live'       // live trading (always rendered locked on this platform)
  | 'neutral';

interface VariantSpec {
  icon: IconName;
  classes: string;
}

const VARIANTS: Record<StatusVariant, VariantSpec> = {
  verified: { icon: 'checkCircle', classes: 'border-ok-border bg-ok-bg text-ok-strong' },
  ok: { icon: 'checkCircle', classes: 'border-ok-border bg-ok-bg text-ok-fg' },
  unverified: { icon: 'alertCircle', classes: 'border-warn-border bg-warn-bg text-warn-strong' },
  legacy: { icon: 'history', classes: 'border-warn-border bg-warn-bg text-warn-strong' },
  warning: { icon: 'warning', classes: 'border-warn-border bg-warn-bg text-warn-strong' },
  error: { icon: 'xCircle', classes: 'border-danger-border bg-danger-bg text-danger-strong' },
  blocked: { icon: 'lock', classes: 'border-danger-border bg-danger-bg text-danger-strong' },
  running: { icon: 'clock', classes: 'border-info-border bg-info-bg text-info-strong' },
  queued: { icon: 'clock', classes: 'border-ink-300 bg-ink-100 text-ink-600' },
  info: { icon: 'info', classes: 'border-info-border bg-info-bg text-info-strong' },
  paper: { icon: 'flask', classes: 'border-accent-200 bg-accent-50 text-accent-800' },
  live: { icon: 'lock', classes: 'border-danger-border bg-danger-bg text-danger-strong' },
  neutral: { icon: 'info', classes: 'border-ink-300 bg-ink-100 text-ink-600' },
};

interface StatusTagProps {
  variant: StatusVariant;
  children: ReactNode;
  className?: string;
  title?: string;
}

export default function StatusTag({ variant, children, className = '', title }: StatusTagProps) {
  const spec = VARIANTS[variant];
  return (
    <span
      data-variant={variant}
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 text-xs font-medium leading-5 ${spec.classes} ${className}`}
    >
      <Icon name={spec.icon} className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span>{children}</span>
    </span>
  );
}
