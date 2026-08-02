import type { ReactNode } from 'react';

interface CardProps {
  children: ReactNode;
  className?: string;
  padding?: 'none' | 'sm' | 'md' | 'lg';
  onClick?: () => void;
  title?: string;
  description?: string;
  actions?: ReactNode;
  id?: string;
  ariaLabelledby?: string;
}

const paddingClasses: Record<string, string> = {
  none: '',
  sm: 'p-3',
  md: 'p-4 sm:p-5',
  lg: 'p-5 sm:p-6',
};

/**
 * Flat bordered surface. Optional header row with title/description/actions.
 * No decorative gradients or heavy shadows — overlays only.
 */
export default function Card({
  children,
  className = '',
  padding = 'md',
  onClick,
  title,
  description,
  actions,
  id,
  ariaLabelledby,
}: CardProps) {
  const hasHeader = title !== undefined || description !== undefined || actions !== undefined;
  return (
    <section
      id={id}
      aria-labelledby={ariaLabelledby}
      className={`rounded-md border border-ink-200 bg-surface ${
        onClick ? 'cursor-pointer transition-colors hover:border-ink-300' : ''
      } ${className}`}
      onClick={onClick}
    >
      {hasHeader && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-200 px-4 py-3 sm:px-5">
          <div className="min-w-0">
            {title !== undefined && (
              <h2 className="text-base font-semibold leading-6">{title}</h2>
            )}
            {description !== undefined && (
              <p className="mt-0.5 text-sm leading-5 text-ink-500">{description}</p>
            )}
          </div>
          {actions !== undefined && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={paddingClasses[padding]}>{children}</div>
    </section>
  );
}
