import type { ButtonHTMLAttributes, ReactNode } from 'react';
import Spinner from './Spinner';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost' | 'subtle';
  size?: 'sm' | 'md' | 'lg';
  loading?: boolean;
  children: ReactNode;
}

const variantClasses: Record<string, string> = {
  primary:
    'bg-accent-700 text-white hover:bg-accent-800 active:bg-accent-900 border border-accent-700',
  secondary:
    'bg-surface text-ink-700 border border-ink-300 hover:bg-ink-100 hover:text-ink-900',
  danger:
    'bg-danger-fg text-white hover:bg-danger-strong border border-danger-fg',
  ghost:
    'bg-transparent text-ink-600 border border-transparent hover:bg-ink-100 hover:text-ink-900',
  subtle:
    'bg-ink-100 text-ink-700 border border-ink-200 hover:bg-ink-200',
};

const sizeClasses: Record<string, string> = {
  sm: 'px-2.5 py-1.5 text-xs min-h-[32px]',
  md: 'px-3.5 py-2 text-sm min-h-[38px]',
  lg: 'px-5 py-2.5 text-base min-h-[44px]',
};

export default function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  children,
  className = '',
  disabled,
  type = 'button',
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center gap-2 rounded font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${variantClasses[variant]} ${sizeClasses[size]} ${className}`}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading && <Spinner size="sm" label="处理中" className="text-current" />}
      {children}
    </button>
  );
}
