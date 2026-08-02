import type { ReactNode } from 'react';

export interface DescriptionItem {
  label: ReactNode;
  value: ReactNode;
  mono?: boolean;
  span?: 1 | 2;
}

interface DescriptionListProps {
  items: DescriptionItem[];
  columns?: 1 | 2 | 3 | 4;
  className?: string;
}

const columnClasses: Record<number, string> = {
  1: 'sm:grid-cols-1',
  2: 'sm:grid-cols-2',
  3: 'sm:grid-cols-2 lg:grid-cols-3',
  4: 'sm:grid-cols-2 lg:grid-cols-4',
};

/**
 * Metadata grid built on semantic <dl> — used for configuration snapshots,
 * data provenance and evidence fields.
 */
export default function DescriptionList({ items, columns = 2, className = '' }: DescriptionListProps) {
  return (
    <dl className={`grid grid-cols-1 gap-x-6 gap-y-3 ${columnClasses[columns]} ${className}`}>
      {items.map((item, index) => (
        <div key={index} className={item.span === 2 ? 'sm:col-span-2' : ''}>
          <dt className="text-xs font-medium uppercase tracking-wide text-ink-400">{item.label}</dt>
          <dd className={`mt-0.5 break-words text-sm text-ink-800 ${item.mono ? 'font-mono text-[13px]' : ''}`}>
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
