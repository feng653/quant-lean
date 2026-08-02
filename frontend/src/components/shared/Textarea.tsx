import { type TextareaHTMLAttributes, forwardRef, useId } from 'react';

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: string;
  error?: string;
  hint?: string;
  requiredMark?: boolean;
}

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ label, error, hint, requiredMark = false, className = '', id, rows = 3, ...props }, ref) => {
    const autoId = useId();
    const textareaId = id ?? autoId;
    const describedBy = error ? `${textareaId}-error` : hint ? `${textareaId}-hint` : undefined;
    return (
      <div className="w-full">
        {label && (
          <label htmlFor={textareaId} className="mb-1 block text-sm font-medium text-ink-700">
            {label}
            {requiredMark && <span className="ml-0.5 text-danger-fg" aria-hidden>*</span>}
          </label>
        )}
        <textarea
          ref={ref}
          id={textareaId}
          rows={rows}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          className={`block w-full rounded border bg-surface px-3 py-2 text-sm text-ink-900 placeholder-ink-400 transition-colors focus:border-accent-600 focus:outline-none focus:ring-2 focus:ring-accent-600/30 disabled:cursor-not-allowed disabled:bg-ink-100 ${
            error ? 'border-danger-fg' : 'border-ink-300'
          } ${className}`}
          {...props}
        />
        {error && (
          <p id={`${textareaId}-error`} role="alert" className="mt-1 text-xs text-danger-fg">
            {error}
          </p>
        )}
        {!error && hint && (
          <p id={`${textareaId}-hint`} className="mt-1 text-xs text-ink-500">
            {hint}
          </p>
        )}
      </div>
    );
  }
);

Textarea.displayName = 'Textarea';
export default Textarea;
