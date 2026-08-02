interface SkeletonProps {
  className?: string;
  lines?: number;
}

/** Placeholder shimmer for loading content. Decorative only. */
export default function Skeleton({ className = 'h-4 w-full', lines = 1 }: SkeletonProps) {
  if (lines > 1) {
    return (
      <div className="space-y-2" aria-hidden>
        {Array.from({ length: lines }, (_, index) => (
          <div
            key={index}
            className={`animate-pulse rounded bg-ink-200 motion-reduce:animate-none ${className} ${
              index === lines - 1 ? 'w-2/3' : ''
            }`}
          />
        ))}
      </div>
    );
  }
  return (
    <div
      aria-hidden
      className={`animate-pulse rounded bg-ink-200 motion-reduce:animate-none ${className}`}
    />
  );
}
