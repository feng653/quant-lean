import Button from './Button';
import Icon from './Icon';

interface PaginationProps {
  page: number;
  total: number;
  limit: number;
  onChange: (page: number) => void;
  className?: string;
}

export default function Pagination({ page, total, limit, onChange, className = '' }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  return (
    <nav
      aria-label="分页"
      className={`flex flex-wrap items-center justify-between gap-3 text-sm text-ink-500 ${className}`}
    >
      <p className="tnum">
        共 {total} 条，第 {page}/{totalPages} 页
      </p>
      <div className="flex items-center gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={page <= 1}
          onClick={() => onChange(page - 1)}
          aria-label="上一页"
        >
          <Icon name="chevronLeft" className="h-4 w-4" />
          上一页
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={page >= totalPages}
          onClick={() => onChange(page + 1)}
          aria-label="下一页"
        >
          下一页
          <Icon name="chevronRight" className="h-4 w-4" />
        </Button>
      </div>
    </nav>
  );
}
