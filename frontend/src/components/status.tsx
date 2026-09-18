import { GitCompare } from "lucide-react";

import { Tip } from "@/components/ui/overlay";
import { formatPct } from "@/lib/format";
import { routeSpec, statusSpec, TONE_CLASS } from "@/lib/status";
import { cn } from "@/lib/utils";

/** The one way a status is drawn: tinted chip, coloured icon, ink label. */
export function StatusBadge({ status, className, iconOnly }: { status: string | null | undefined; className?: string; iconOnly?: boolean }) {
  const spec = statusSpec(status);
  const tone = TONE_CLASS[spec.tone];
  const Icon = spec.icon;
  const chip = (
    <span
      className={cn(
        "inline-flex h-5.5 items-center gap-1 rounded-full px-2 text-xs font-medium whitespace-nowrap text-ink ring-1 ring-inset",
        tone.chip,
        iconOnly && "px-1",
        className,
      )}
    >
      <Icon className={cn("size-3.5 shrink-0", tone.icon, status === "RUNNING" && "animate-spin")} aria-hidden />
      <span className={iconOnly ? "sr-only" : undefined}>{spec.label}</span>
    </span>
  );
  return <Tip content={spec.hint}>{chip}</Tip>;
}

/** A case a newer run disagrees with (Q5). Same look wherever a conflict is flagged. */
export function ConflictTag() {
  return (
    <Tip content="A newer reconciliation run disagrees with the decision this case was opened on.">
      <span className="inline-flex h-5.5 items-center gap-1 rounded-full bg-serious/15 px-2 text-xs font-medium text-ink ring-1 ring-serious/40 ring-inset">
        <GitCompare className="size-3.5 text-serious-text" aria-hidden />
        Conflict
      </span>
    </Tip>
  );
}

export function RouteBadge({ route }: { route: string | null | undefined }) {
  const spec = routeSpec(route);
  const Icon = spec.icon;
  return (
    <Tip content={spec.hint}>
      <span className="inline-flex items-center gap-1 text-xs text-ink-2">
        <Icon className="size-3.5 text-muted" aria-hidden />
        {spec.label}
      </span>
    </Tip>
  );
}

/**
 * Calibrated confidence, drawn the same way on every screen: a fixed-width
 * track whose fill is the probability, beside the number. One hue - it is a
 * magnitude, not a status; the band a value falls in is shown separately,
 * on the Investigation page, against the thresholds that decided it.
 */
export function ConfidenceBadge({ value, className }: { value: number | null | undefined; className?: string }) {
  if (value == null) return <span className="text-xs text-muted">—</span>;
  const pct = Math.max(0, Math.min(1, value));
  return (
    <span className={cn("inline-flex items-center gap-2", className)} aria-label={`Confidence ${formatPct(value)}`}>
      <span className="relative h-1.5 w-12 overflow-hidden rounded-full bg-surface-3" aria-hidden>
        <span className="absolute inset-y-0 left-0 rounded-full bg-series-1" style={{ width: `${pct * 100}%` }} />
      </span>
      <span className="tabular w-11 text-right text-xs text-ink">{formatPct(value)}</span>
    </span>
  );
}
