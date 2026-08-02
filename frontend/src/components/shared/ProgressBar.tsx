interface ProgressBarProps {
  /** 0-100 */
  value: number;
  label: string;
  variant?: 'accent' | 'danger';
  className?: string;
  showValue?: boolean;
}

export default function ProgressBar({
  value,
  label,
  variant = 'accent',
  className = '',
  showValue = true,
}: ProgressBarProps) {
  const clamped = Math.min(100, Math.max(0, Number.isFinite(value) ? value : 0));
  const barColor = variant === 'danger' ? 'bg-danger-fg' : 'bg-accent-600';
  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(clamped)}
        className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink-200"
      >
        <div
          className={`h-full rounded-full transition-[width] motion-reduce:transition-none ${barColor}`}
          style={{ width: `${clamped}%` }}
        />
      </div>
      {showValue && <span className="tnum w-10 text-right text-xs text-ink-500">{Math.round(clamped)}%</span>}
    </div>
  );
}
