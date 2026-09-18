import { ChevronDown } from "lucide-react";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

const field =
  "w-full rounded-md bg-surface px-2.5 text-sm text-ink ring-1 ring-border-strong placeholder:text-muted focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-60 aria-[invalid=true]:ring-critical";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(field, "h-8", className)} {...props} />;
}

export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return <textarea className={cn(field, "min-h-20 py-2", className)} {...props} />;
}

/**
 * A native select, styled. Native on purpose: it is keyboard- and
 * screen-reader-correct for free, and every filter here is a short list.
 */
export function Select({ className, children, ...props }: ComponentProps<"select">) {
  return (
    <div className={cn("relative", className)}>
      <select className={cn(field, "h-8 appearance-none pr-7")} {...props}>
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
    </div>
  );
}

export function Label({ className, ...props }: ComponentProps<"label">) {
  return <label className={cn("text-xs font-medium text-ink-2", className)} {...props} />;
}

export function FieldError({ children, id }: { children?: string | null; id?: string }) {
  if (!children) return null;
  return (
    <p id={id} className="text-xs text-critical-text" role="alert">
      {children}
    </p>
  );
}
