import type { ReactNode } from 'react';
import Spinner from './Spinner';

export interface Column<T> {
  key: string;
  header: ReactNode;
  render?: (item: T) => ReactNode;
  className?: string;
  /** Numeric columns are right-aligned with tabular figures. */
  numeric?: boolean;
}

interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  keyField?: string;
  loading?: boolean;
  emptyMessage?: string;
  onRowClick?: (item: T) => void;
  caption?: string;
  dense?: boolean;
  minWidth?: string;
}

/**
 * Research data table: quiet header, visible row separation, right-aligned
 * tabular numerals for numeric columns, keyboard-activatable rows when
 * onRowClick is provided.
 */
export default function Table<T extends object>({
  columns,
  data,
  keyField = 'id',
  loading = false,
  emptyMessage = '暂无数据',
  onRowClick,
  caption,
  dense = false,
  minWidth = '640px',
}: TableProps<T>) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Spinner size="lg" label="表格加载中" />
      </div>
    );
  }

  if (data.length === 0) {
    return <div className="py-12 text-center text-sm text-ink-400">{emptyMessage}</div>;
  }

  const cellPadding = dense ? 'px-3 py-2' : 'px-4 py-2.5';

  return (
    <div className="overflow-x-auto scrollbar-thin">
      <table className="w-full text-sm" style={{ minWidth }}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-ink-200 bg-ink-50">
            {columns.map((col) => (
              <th
                key={col.key}
                scope="col"
                className={`${cellPadding} text-xs font-semibold uppercase tracking-wide text-ink-500 ${
                  col.numeric ? 'text-right' : 'text-left'
                } ${col.className ?? ''}`}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-ink-100">
          {data.map((item, index) => {
            const record = item as Record<string, unknown>;
            const rowKey = String(record[keyField] ?? index);
            const rowContent = columns.map((col) => (
              <td
                key={col.key}
                className={`${cellPadding} text-ink-700 ${
                  col.numeric ? 'tnum text-right' : ''
                } ${col.className ?? ''}`}
              >
                {col.render ? col.render(item) : String(record[col.key] ?? '')}
              </td>
            ));
            if (onRowClick) {
              return (
                <tr
                  key={rowKey}
                  tabIndex={0}
                  onClick={() => onRowClick(item)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      onRowClick(item);
                    }
                  }}
                  className="cursor-pointer transition-colors hover:bg-accent-50/60 focus-visible:bg-accent-50"
                >
                  {rowContent}
                </tr>
              );
            }
            return <tr key={rowKey}>{rowContent}</tr>;
          })}
        </tbody>
      </table>
    </div>
  );
}
