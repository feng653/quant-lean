import type { ReactNode } from 'react';
import { Link } from 'react-router';
import Icon from './Icon';

export interface BreadcrumbItem {
  label: string;
  to?: string;
}

interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  breadcrumb?: BreadcrumbItem[];
  actions?: ReactNode;
  tags?: ReactNode;
}

/**
 * Standard page heading: breadcrumb, title, provenance/status tags and the
 * primary action row. Keeps information architecture consistent across pages.
 */
export default function PageHeader({ title, description, breadcrumb, actions, tags }: PageHeaderProps) {
  return (
    <div className="mb-5">
      {breadcrumb && breadcrumb.length > 0 && (
        <nav aria-label="面包屑" className="mb-2 flex items-center gap-1.5 text-xs text-ink-400">
          {breadcrumb.map((item, index) => (
            <span key={index} className="flex items-center gap-1.5">
              {index > 0 && <Icon name="chevronRight" className="h-3 w-3" aria-hidden />}
              {item.to ? (
                <Link to={item.to} className="transition-colors hover:text-accent-700 hover:underline">
                  {item.label}
                </Link>
              ) : (
                <span className="text-ink-500">{item.label}</span>
              )}
            </span>
          ))}
        </nav>
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold leading-8">{title}</h1>
          {description && <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-500">{description}</p>}
          {tags && <div className="mt-2 flex flex-wrap items-center gap-2">{tags}</div>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}
