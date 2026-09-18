import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Card({ className, ...props }: ComponentProps<"section">) {
  return <section className={cn("rounded-lg bg-surface ring-1 ring-border", className)} {...props} />;
}

export function CardHeader({
  title,
  description,
  actions,
  className,
  id,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <header className={cn("flex items-start justify-between gap-3 px-4 pt-3.5 pb-2", className)}>
      <div className="min-w-0">
        <h2 id={id} className="text-sm font-semibold text-ink">
          {title}
        </h2>
        {description ? <p className="mt-0.5 text-xs text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function CardBody({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("px-4 pb-4", className)} {...props} />;
}

/** Loading placeholder. Skeletons, not spinners: the layout does not jump when data lands. */
export function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("animate-pulse rounded-md bg-surface-3", className)} aria-hidden {...props} />;
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded-sm bg-surface-2 px-1 py-px font-mono text-[11px] text-ink-2 ring-1 ring-border-strong">
      {children}
    </kbd>
  );
}
