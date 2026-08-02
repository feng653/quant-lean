interface SpinnerProps {
  size?: 'sm' | 'md' | 'lg';
  className?: string;
  label?: string;
}

const sizeClasses: Record<string, string> = {
  sm: 'h-4 w-4',
  md: 'h-6 w-6',
  lg: 'h-10 w-10',
};

export default function Spinner({ size = 'md', className = '', label = '加载中' }: SpinnerProps) {
  return (
    <span role="status" className={`inline-flex items-center gap-2 ${className}`}>
      <svg
        className={`animate-spin text-accent-600 motion-reduce:animate-none ${sizeClasses[size]}`}
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden
      >
        <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path
          className="opacity-90"
          fill="currentColor"
          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
        />
      </svg>
      <span className="sr-only">{label}</span>
    </span>
  );
}
